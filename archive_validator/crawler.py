"""
crawler.py — Recursive HTML file scanner.

Walks an archive directory tree, finds all HTML files, and coordinates
link extraction from each file. Reports progress to stderr.
"""

import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

from .extractor import LinkExtractor, LinkRecord
from .resolver import URLResolver


# HTML file extensions to scan
HTML_EXTENSIONS = {".html", ".htm", ".shtml", ".shtm", ".xhtml"}


class Crawler:
    """
    Recursively scans an archive directory for HTML files and extracts links.

    Parameters
    ----------
    input_dir : Path
        The archive directory to crawl.
    resolver : URLResolver
        Used to build source URLs and resolve links.
    extractor : LinkExtractor
        Used to extract links from each HTML file.
    verbose : bool
        If True, print per-file progress to stderr.
    """

    def __init__(
        self,
        input_dir: Path,
        resolver: URLResolver,
        extractor: LinkExtractor,
        verbose: bool = True,
    ):
        self.input_dir = input_dir.resolve()
        self.resolver = resolver
        self.extractor = extractor
        self.verbose = verbose

    def find_html_files(self) -> list[Path]:
        """
        Recursively find all HTML files under input_dir.

        Returns
        -------
        list of Path
            Sorted list of absolute paths to HTML files.
        """
        html_files = []
        try:
            for path in self.input_dir.rglob("*"):
                if path.is_file() and path.suffix.lower() in HTML_EXTENSIONS:
                    html_files.append(path)
        except PermissionError as e:
            self._print(f"[WARNING] Permission denied scanning {self.input_dir}: {e}")

        html_files.sort()
        return html_files

    def crawl(self) -> tuple[list[Path], list[LinkRecord]]:
        """
        Crawl the archive directory and extract all links from all HTML files.

        Prints progress to stderr showing files processed / total.

        Returns
        -------
        tuple of (html_files, all_links)
            html_files : list of Path — all HTML files found
            all_links  : list of LinkRecord — all links extracted
        """
        self._print("[1/4] Scanning for HTML files...")
        html_files = self.find_html_files()
        total = len(html_files)
        self._print(f"      Found {total} HTML file(s) in {self.input_dir}")

        if total == 0:
            self._print("[WARNING] No HTML files found. Check --input-dir.")
            return [], []

        self._print(f"[2/4] Extracting links from {total} HTML file(s)...")
        all_links: list[LinkRecord] = []

        for i, html_file in enumerate(html_files, start=1):
            # Build the source URL for this file (if base_url is available)
            source_url = self.resolver.path_to_url(html_file)

            # Show progress: current/total and filename relative to input_dir
            try:
                rel_path = html_file.relative_to(self.input_dir)
            except ValueError:
                rel_path = html_file

            self._print(f"      [{i}/{total}] {rel_path}", end="\r")

            links = self.extractor.extract_from_file(html_file, source_url=source_url)
            all_links.extend(links)

        # Clear the \r line and print summary
        self._print(f"      Processed {total}/{total} files.          ")

        internal_links = [l for l in all_links if not l.is_external and not l.is_ignored]
        self._print(f"      Extracted {len(all_links)} total link(s), "
                    f"{len(internal_links)} internal link(s) to check.")

        return html_files, all_links

    def _print(self, message: str, end: str = "\n") -> None:
        """Print a progress message to stderr."""
        if self.verbose:
            print(message, file=sys.stderr, end=end, flush=True)
