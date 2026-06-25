"""
detector.py — Dead link detection.

For each internal link extracted from an HTML file, this module:
1. Resolves the link to a local filesystem path (via URLResolver)
2. Checks whether the target file exists
3. Handles directory links by looking for index files (DD-004)
4. Records broken links with full metadata for reporting

External link checking (HTTP HEAD/GET) is also handled here when
the --external flag is set.
"""

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

from .extractor import LinkRecord
from .resolver import URLResolver


@dataclass
class BrokenLink:
    """
    Represents a confirmed broken (dead) link.

    Attributes
    ----------
    source_file : Path
        The HTML file containing the broken link.
    source_url : str or None
        Public URL of the source HTML file.
    raw_href : str
        The link exactly as written in the HTML.
    resolved_url : str or None
        The fully resolved public URL of the target (if constructable).
    expected_path : Path or None
        The local filesystem path where the target was expected.
    link_type : str
        Type: 'page', 'image', 'css', 'js', 'frame', 'asset'.
    tag_name : str
        HTML tag containing the link.
    attr_name : str
        HTML attribute containing the link.
    is_external : bool
        True if this was an external link that returned an error.
    http_status : int or None
        HTTP status code (for external links only).
    candidates : list
        Replacement candidates found by the searcher (populated later).
    """
    source_file: Path
    source_url: Optional[str]
    raw_href: str
    resolved_url: Optional[str]
    expected_path: Optional[Path]
    link_type: str
    tag_name: str
    attr_name: str
    is_external: bool = False
    http_status: Optional[int] = None
    candidates: list = field(default_factory=list)

    @property
    def missing_filename(self) -> str:
        """The filename portion of the expected path."""
        if self.expected_path:
            return self.expected_path.name
        # Fall back to parsing the raw href
        path = unquote(urlparse(self.raw_href).path)
        return Path(path).name if path else self.raw_href

    @property
    def source_display(self) -> str:
        """Human-readable source identifier for the report."""
        return self.source_url or str(self.source_file)

    @property
    def expected_path_display(self) -> str:
        """Human-readable expected path for the report."""
        return str(self.expected_path) if self.expected_path else "(unresolvable)"


class DeadLinkDetector:
    """
    Checks internal links for existence and optionally checks external links.

    Parameters
    ----------
    resolver : URLResolver
        Used to resolve hrefs to filesystem paths.
    check_external : bool
        If True, also check external links via HTTP.
    external_timeout : float
        Timeout in seconds for external HTTP requests.
    verbose : bool
        If True, print progress to stderr.
    """

    def __init__(
        self,
        resolver: URLResolver,
        check_external: bool = False,
        external_timeout: float = 10.0,
        verbose: bool = True,
    ):
        self.resolver = resolver
        self.check_external = check_external
        self.external_timeout = external_timeout
        self.verbose = verbose
        self._session = None  # Lazy-initialized requests session

    def detect(self, links: list[LinkRecord]) -> tuple[list[LinkRecord], list[BrokenLink]]:
        """
        Check all links and return broken ones.

        Parameters
        ----------
        links : list of LinkRecord
            All links extracted from the crawl.

        Returns
        -------
        tuple of (checked_links, broken_links)
            checked_links : list of LinkRecord with resolved_path/url populated
            broken_links  : list of BrokenLink for missing targets
        """
        self._print("[3/4] Checking links for broken targets...")

        internal_links = [l for l in links if not l.is_external and not l.is_ignored]
        external_links = [l for l in links if l.is_external] if self.check_external else []

        total_to_check = len(internal_links) + len(external_links)
        self._print(f"      Checking {len(internal_links)} internal link(s)"
                    + (f" and {len(external_links)} external link(s)" if external_links else ""))

        broken: list[BrokenLink] = []
        checked = 0

        # --- Check internal links ---
        for link in internal_links:
            resolved_path = self.resolver.resolve_to_path(link.raw_href, link.source_file)

            if resolved_path is None:
                # Could not resolve (e.g. root-relative without base_url) — skip
                continue

            # Handle directory links: look for index files (DD-004)
            actual_path = resolved_path
            if resolved_path.is_dir():
                index_path = self.resolver.resolve_directory_index(resolved_path)
                if index_path:
                    # Directory resolves to an index file — not broken
                    link.resolved_path = index_path
                    link.resolved_url = self.resolver.path_to_url(index_path)
                    checked += 1
                    continue
                else:
                    # Directory exists but has no index file — broken
                    actual_path = resolved_path

            # Populate resolved info on the link record
            link.resolved_path = resolved_path
            link.resolved_url = self.resolver.path_to_url(resolved_path)

            # Check existence (respects case sensitivity setting)
            exists = self._check_local_exists(resolved_path)

            if not exists:
                broken.append(BrokenLink(
                    source_file=link.source_file,
                    source_url=link.source_url,
                    raw_href=link.raw_href,
                    resolved_url=link.resolved_url,
                    expected_path=resolved_path,
                    link_type=link.link_type,
                    tag_name=link.tag_name,
                    attr_name=link.attr_name,
                    is_external=False,
                ))

            checked += 1

        # --- Check external links (optional) ---
        if external_links:
            self._print(f"      Checking {len(external_links)} external link(s) via HTTP...")
            for link in external_links:
                status = self._check_external_url(link.raw_href)
                if status is not None and status >= 400:
                    broken.append(BrokenLink(
                        source_file=link.source_file,
                        source_url=link.source_url,
                        raw_href=link.raw_href,
                        resolved_url=link.raw_href,
                        expected_path=None,
                        link_type=link.link_type,
                        tag_name=link.tag_name,
                        attr_name=link.attr_name,
                        is_external=True,
                        http_status=status,
                    ))

        self._print(f"      Found {len(broken)} broken link(s) out of "
                    f"{len(internal_links)} internal link(s) checked.")

        return links, broken

    def _check_local_exists(self, path: Path) -> bool:
        """
        Check if a local file exists, using the resolver's case sensitivity setting.
        """
        return self.resolver._file_exists(path)

    def _check_external_url(self, url: str) -> Optional[int]:
        """
        Check an external URL via HTTP HEAD (falls back to GET).

        Returns the HTTP status code, or None if the request failed entirely.
        """
        try:
            import requests
            if self._session is None:
                self._session = requests.Session()
                self._session.headers.update({
                    "User-Agent": "archive-validator/1.0 (link checker)"
                })

            try:
                resp = self._session.head(
                    url,
                    timeout=self.external_timeout,
                    allow_redirects=True,
                )
                # Some servers don't support HEAD — fall back to GET
                if resp.status_code == 405:
                    resp = self._session.get(
                        url,
                        timeout=self.external_timeout,
                        allow_redirects=True,
                        stream=True,
                    )
                return resp.status_code
            except requests.exceptions.RequestException:
                return None

        except ImportError:
            self._print("[WARNING] 'requests' library not available. "
                        "External link checking disabled.")
            return None

    def _print(self, message: str) -> None:
        """Print a progress message to stderr."""
        if self.verbose:
            print(message, file=sys.stderr, flush=True)
