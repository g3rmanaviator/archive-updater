"""
resolver.py — URL ↔ Filesystem path mapping logic.

This module handles the critical task of converting URLs found in HTML files
into local filesystem paths, and vice versa.

URL-to-Filesystem Mapping Logic:
---------------------------------
Given:
  --input-dir  /htdocs/alumni/mirror/archives/archive-1998-06-01
  --base-url   https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/

A link in an HTML file can be:

1. Relative link: href="images/logo.gif"
   - Resolved relative to the HTML file's directory
   - e.g. if source is /htdocs/.../archive-1998-06-01/about/index.html
     then resolved path = /htdocs/.../archive-1998-06-01/about/images/logo.gif

2. Root-relative link: href="/mirror/archives/archive-1998-06-01/images/logo.gif"
   - Strip the base URL path prefix to get the relative path within the archive
   - e.g. strip "/mirror/archives/archive-1998-06-01/" → "images/logo.gif"
   - Then join with input-dir

3. Absolute URL (same host): href="https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/images/logo.gif"
   - Only treated as internal if --base-url is provided and the URL starts with it
   - Strip the base URL to get the relative path, then join with input-dir

Design decisions implemented here:
  - DD-006: Query strings are stripped before filesystem resolution
  - DD-007: URL fragments are stripped before filesystem resolution
  - DD-004: Directory links are resolved to index files
  - DD-005: Internal vs external classification
"""

import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote, urlunparse


# Index filenames to check when a link points to a directory (DD-004)
INDEX_FILES = ["index.html", "index.htm", "default.html", "default.htm"]

# URL schemes that should always be ignored
IGNORED_SCHEMES = {"mailto", "tel", "javascript", "data", "ftp", "news", "irc"}


class URLResolver:
    """
    Resolves URLs found in HTML files to local filesystem paths.

    Parameters
    ----------
    input_dir : Path
        The root directory of the archive being validated.
    base_url : str or None
        The public URL corresponding to input_dir. If provided, enables:
        - Recognition of absolute URLs as internal links
        - Construction of public URLs for source pages in the report
    case_insensitive : bool
        If True, filesystem lookups are case-insensitive.
    """

    def __init__(self, input_dir: Path, base_url: str = None, case_insensitive: bool = False):
        self.input_dir = input_dir.resolve()
        self.case_insensitive = case_insensitive

        # Normalize base_url: ensure it ends with /
        if base_url:
            self.base_url = base_url.rstrip("/") + "/"
            parsed = urlparse(self.base_url)
            self.base_scheme = parsed.scheme
            self.base_netloc = parsed.netloc
            self.base_path = parsed.path  # e.g. "/mirror/archives/archive-1998-06-01/"
        else:
            self.base_url = None
            self.base_scheme = None
            self.base_netloc = None
            self.base_path = None

    def is_ignored_link(self, href: str) -> bool:
        """
        Returns True if the link should be completely ignored (not checked at all).
        Covers: mailto:, tel:, javascript:, data:, fragment-only links.
        """
        if not href or not href.strip():
            return True

        href = href.strip()

        # Fragment-only links like "#top" or "#"
        if href.startswith("#"):
            return True

        # Check for ignored schemes
        parsed = urlparse(href)
        if parsed.scheme.lower() in IGNORED_SCHEMES:
            return True

        return False

    def is_external_link(self, href: str) -> bool:
        """
        Returns True if the link points to an external resource.

        A link is internal if:
        - It has no scheme (relative URL), OR
        - Its scheme+netloc matches the base_url (when base_url is provided)

        (DD-005)
        """
        if self.is_ignored_link(href):
            return False  # Not external — it's ignored entirely

        parsed = urlparse(href)

        # No scheme = relative URL = internal
        if not parsed.scheme:
            return False

        # Has a scheme but matches our base URL host = internal
        if self.base_url and self.base_netloc:
            if (parsed.scheme == self.base_scheme and
                    parsed.netloc == self.base_netloc and
                    parsed.path.startswith(self.base_path)):
                return False

        # Everything else with a scheme is external
        if parsed.scheme:
            return True

        return False

    def resolve_to_path(self, href: str, source_file: Path) -> Path | None:
        """
        Resolve a link href to an absolute local filesystem path.

        Returns None if the link cannot be resolved to a local path
        (e.g. it's external, ignored, or points outside the archive).

        Steps:
        1. Strip fragment and query string (DD-006, DD-007)
        2. URL-decode the path component
        3. Determine if relative, root-relative, or absolute
        4. Map to filesystem path
        5. Handle directory links (DD-004)

        Parameters
        ----------
        href : str
            The raw href/src value from the HTML attribute.
        source_file : Path
            The absolute path of the HTML file containing this link.

        Returns
        -------
        Path or None
            Resolved absolute filesystem path, or None if not resolvable.
        """
        if self.is_ignored_link(href) or self.is_external_link(href):
            return None

        parsed = urlparse(href)

        # Strip fragment and query string, keep only scheme+netloc+path
        clean_parsed = parsed._replace(fragment="", query="")
        clean_href = urlunparse(clean_parsed)

        # URL-decode the path (handles %20, %2B, etc.)
        decoded_path = unquote(clean_parsed.path)

        if not decoded_path:
            return None

        # Case 1: Absolute URL matching our base_url
        # e.g. https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/page.html
        if parsed.scheme and self.base_url and self.base_path:
            if (parsed.netloc == self.base_netloc and
                    decoded_path.startswith(self.base_path)):
                # Strip the base path prefix to get archive-relative path
                relative = decoded_path[len(self.base_path):]
                return self._resolve_relative_to_input(relative)

        # Case 2: Root-relative URL (starts with /)
        # e.g. /mirror/archives/archive-1998-06-01/images/logo.gif
        if decoded_path.startswith("/"):
            if self.base_path:
                # Try to strip the base path prefix
                if decoded_path.startswith(self.base_path):
                    relative = decoded_path[len(self.base_path):]
                    return self._resolve_relative_to_input(relative)
                else:
                    # Root-relative but outside our archive — treat as external
                    return None
            else:
                # No base_url provided; root-relative links can't be resolved
                # without knowing the server root. Skip them.
                return None

        # Case 3: Relative URL
        # e.g. images/logo.gif or ../other/page.html
        source_dir = source_file.parent
        resolved = (source_dir / decoded_path).resolve()

        # Security check: ensure resolved path is within input_dir
        try:
            resolved.relative_to(self.input_dir)
            return resolved
        except ValueError:
            # Path escapes the archive directory — treat as external
            return None

    def _resolve_relative_to_input(self, relative_path: str) -> Path | None:
        """
        Join a relative path (relative to input_dir root) with input_dir.

        Parameters
        ----------
        relative_path : str
            Path relative to the archive root, e.g. "images/logo.gif"

        Returns
        -------
        Path or None
        """
        # Strip leading slashes
        relative_path = relative_path.lstrip("/")

        if not relative_path:
            return self.input_dir

        resolved = (self.input_dir / relative_path).resolve()

        # Security check: ensure it stays within input_dir
        try:
            resolved.relative_to(self.input_dir)
            return resolved
        except ValueError:
            return None

    def resolve_directory_index(self, path: Path) -> Path | None:
        """
        If path is a directory, look for an index file within it (DD-004).

        Checks in order: index.html, index.htm, default.html, default.htm

        Returns the index file path if found, None if no index file exists.
        """
        if not path.is_dir():
            return None

        for index_name in INDEX_FILES:
            candidate = path / index_name
            if self._file_exists(candidate):
                return candidate

        return None

    def _file_exists(self, path: Path) -> bool:
        """
        Check if a file exists, respecting case sensitivity setting (DD-009).

        On case-sensitive filesystems (Linux), this is a direct check.
        With --case-insensitive, scans the parent directory for a match.
        """
        if self.case_insensitive:
            return self._case_insensitive_exists(path)
        return path.exists()

    def _case_insensitive_exists(self, path: Path) -> bool:
        """
        Check file existence case-insensitively by scanning the parent directory.
        """
        if not path.parent.exists():
            return False
        target_name = path.name.lower()
        try:
            for entry in path.parent.iterdir():
                if entry.name.lower() == target_name:
                    return True
        except PermissionError:
            pass
        return False

    def path_to_url(self, file_path: Path) -> str | None:
        """
        Convert a local filesystem path back to a public URL.

        Returns None if base_url is not set or path is outside input_dir.

        Parameters
        ----------
        file_path : Path
            Absolute filesystem path within the archive.

        Returns
        -------
        str or None
            Public URL for the file, or None if not constructable.
        """
        if not self.base_url:
            return None

        try:
            relative = file_path.resolve().relative_to(self.input_dir)
            # Use forward slashes for URLs regardless of OS
            relative_str = "/".join(relative.parts)
            return self.base_url + relative_str
        except ValueError:
            return None

    def get_link_type(self, tag_name: str, attr_name: str, href: str) -> str:
        """
        Determine the type of a link based on its tag, attribute, and URL.

        Returns one of: 'page', 'image', 'css', 'js', 'frame', 'asset'
        """
        tag = tag_name.lower()
        attr = attr_name.lower()

        if tag == "img" or attr == "background":
            return "image"
        if tag == "link":
            return "css"
        if tag == "script":
            return "js"
        if tag in ("frame", "iframe"):
            return "frame"
        if tag == "a":
            # Determine by file extension
            path = unquote(urlparse(href).path).lower()
            if any(path.endswith(ext) for ext in (".css",)):
                return "css"
            if any(path.endswith(ext) for ext in (".js",)):
                return "js"
            if any(path.endswith(ext) for ext in (
                ".jpg", ".jpeg", ".gif", ".png", ".bmp", ".svg",
                ".ico", ".webp", ".tiff", ".tif"
            )):
                return "image"
            return "page"

        return "asset"
