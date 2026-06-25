"""
extractor.py — Link extraction from HTML files.

Parses HTML using BeautifulSoup and extracts all links from the following
tag/attribute combinations:

  Tag         Attribute    Link type
  ----------  -----------  ---------
  <a>         href         page
  <img>       src          image
  <script>    src          js
  <link>      href         css (typically)
  <frame>     src          frame
  <iframe>    src          frame
  <body>      background   image
  <table>     background   image
  <td>        background   image
  <th>        background   image

Each extracted link is returned as a LinkRecord dataclass containing
all metadata needed for dead-link detection and reporting.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag


@dataclass
class LinkRecord:
    """
    Represents a single link extracted from an HTML file.

    Attributes
    ----------
    source_file : Path
        Absolute path of the HTML file containing this link.
    source_url : str or None
        Public URL of the source HTML file (if base_url was provided).
    raw_href : str
        The raw attribute value exactly as written in the HTML.
    tag_name : str
        The HTML tag name (e.g. 'a', 'img', 'script').
    attr_name : str
        The attribute name (e.g. 'href', 'src', 'background').
    link_type : str
        Categorized type: 'page', 'image', 'css', 'js', 'frame', 'asset'.
    is_external : bool
        True if this link points to an external resource.
    is_ignored : bool
        True if this link was skipped (mailto:, #anchor, etc.).
    resolved_path : Path or None
        Resolved local filesystem path (set by detector).
    resolved_url : str or None
        Resolved public URL (set by detector).
    """
    source_file: Path
    source_url: Optional[str]
    raw_href: str
    tag_name: str
    attr_name: str
    link_type: str
    is_external: bool = False
    is_ignored: bool = False
    resolved_path: Optional[Path] = None
    resolved_url: Optional[str] = None


# Maps (tag_name, attribute_name) → default link type
# The resolver's get_link_type() provides more nuanced typing
TAG_ATTR_MAP = [
    ("a",       "href"),
    ("img",     "src"),
    ("script",  "src"),
    ("link",    "href"),
    ("frame",   "src"),
    ("iframe",  "src"),
    ("body",    "background"),
    ("table",   "background"),
    ("td",      "background"),
    ("th",      "background"),
    ("input",   "src"),      # <input type="image" src="...">
    ("source",  "src"),      # <audio>/<video> sources
    ("embed",   "src"),
    ("object",  "data"),
]


class LinkExtractor:
    """
    Extracts all links from an HTML file using BeautifulSoup.

    Parameters
    ----------
    resolver : URLResolver
        Used to classify links as internal/external/ignored and determine
        link types.
    include_extensions : set or None
        If provided, only links whose target extension is in this set
        will be included. None means include all.
    ignore_patterns : list of re.Pattern
        Compiled regex patterns; links matching any pattern are skipped.
    """

    def __init__(self, resolver, include_extensions=None, ignore_patterns=None):
        self.resolver = resolver
        self.include_extensions = include_extensions  # set of lowercase extensions e.g. {'.html', '.jpg'}
        self.ignore_patterns = ignore_patterns or []

    def extract_from_file(self, html_file: Path, source_url: str = None) -> list[LinkRecord]:
        """
        Parse an HTML file and extract all links.

        Parameters
        ----------
        html_file : Path
            Absolute path to the HTML file to parse.
        source_url : str or None
            Public URL of this HTML file (for report display).

        Returns
        -------
        list of LinkRecord
            All links found in the file, including ignored/external ones
            (flagged accordingly).
        """
        try:
            content = html_file.read_bytes()
        except (OSError, PermissionError) as e:
            return []

        # Use BeautifulSoup with lxml parser (falls back to html.parser)
        try:
            soup = BeautifulSoup(content, "lxml")
        except Exception:
            try:
                soup = BeautifulSoup(content, "html.parser")
            except Exception:
                return []

        records = []
        seen_hrefs = set()  # Deduplicate identical links from the same source file

        for tag_name, attr_name in TAG_ATTR_MAP:
            for tag in soup.find_all(tag_name, **{attr_name: True}):
                href = tag.get(attr_name, "").strip()

                if not href:
                    continue

                # Deduplicate: same href from same source file
                dedup_key = (tag_name, attr_name, href)
                if dedup_key in seen_hrefs:
                    continue
                seen_hrefs.add(dedup_key)

                # Check ignore patterns
                if self._matches_ignore_pattern(href):
                    record = LinkRecord(
                        source_file=html_file,
                        source_url=source_url,
                        raw_href=href,
                        tag_name=tag_name,
                        attr_name=attr_name,
                        link_type=self.resolver.get_link_type(tag_name, attr_name, href),
                        is_ignored=True,
                    )
                    records.append(record)
                    continue

                # Classify the link
                is_ignored = self.resolver.is_ignored_link(href)
                is_external = False if is_ignored else self.resolver.is_external_link(href)

                link_type = self.resolver.get_link_type(tag_name, attr_name, href)

                # Check extension filter (only applies to non-ignored, non-external links)
                if (not is_ignored and not is_external and
                        self.include_extensions is not None):
                    ext = self._get_extension(href)
                    if ext not in self.include_extensions:
                        # Skip this link — not in the included extensions
                        continue

                record = LinkRecord(
                    source_file=html_file,
                    source_url=source_url,
                    raw_href=href,
                    tag_name=tag_name,
                    attr_name=attr_name,
                    link_type=link_type,
                    is_external=is_external,
                    is_ignored=is_ignored,
                )
                records.append(record)

        return records

    def _matches_ignore_pattern(self, href: str) -> bool:
        """Check if href matches any of the user-supplied ignore patterns."""
        for pattern in self.ignore_patterns:
            if pattern.search(href):
                return True
        return False

    def _get_extension(self, href: str) -> str:
        """
        Extract the file extension from a URL path.

        Returns lowercase extension including the dot, e.g. '.html',
        or empty string if no extension.
        """
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(href).path)
        return Path(path).suffix.lower()
