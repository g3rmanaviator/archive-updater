"""
wayback.py — Wayback Machine candidate search via wayback_machine_downloader.

For broken links that have no local candidates, this module queries the
Internet Archive's Wayback Machine using the `wayback_machine_downloader`
Ruby gem as a subprocess.

Requirements:
    gem install wayback_machine_downloader

Usage:
    searcher = WaybackSearcher(
        original_url="http://www.stclares.ac.uk/",
        input_dir=Path("/htdocs/.../archive-2001-06-01"),
        staging_dir=Path("./wayback_staging"),
        workers=3,
        verbose=True,
    )
    broken_links = searcher.search_all(broken_links_without_candidates)

URL construction:
    --original-url  http://www.stclares.ac.uk/
    missing file    /htdocs/.../archive-2001-06-01/photos/students6.jpg
    → strip archive root → photos/students6.jpg
    → original URL  http://www.stclares.ac.uk/photos/students6.jpg
"""

import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin


@dataclass
class WaybackCandidate:
    """
    A replacement candidate sourced from the Wayback Machine.

    Attributes
    ----------
    staged_path : Path
        Local path where the file was downloaded (in the staging directory).
    wayback_url : str
        The Wayback Machine snapshot URL the file was retrieved from.
    original_url : str
        The original URL that was searched.
    snapshot_date : str
        Date of the Wayback snapshot (YYYYMMDD or similar, from the URL).
    confidence : int
        Always 85 for exact-URL Wayback matches.
    """
    staged_path: Path
    wayback_url: str
    original_url: str
    snapshot_date: str
    confidence: int = 85

    @property
    def local_path(self) -> Path:
        """Alias for compatibility with the Candidate interface."""
        return self.staged_path

    @property
    def public_url(self) -> Optional[str]:
        """The Wayback snapshot URL (clickable)."""
        return self.wayback_url

    @property
    def archive_folder(self) -> str:
        return "Wayback Machine"

    @property
    def match_type(self) -> str:
        return f"Wayback snapshot ({self.snapshot_date})"

    @property
    def display_path(self) -> str:
        return self.wayback_url or str(self.staged_path)


# Status constants
STATUS_PENDING = "pending"
STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"  # wayback not enabled or no original URL


@dataclass
class WaybackResult:
    """Result of a Wayback Machine search for a single broken link."""
    status: str = STATUS_PENDING
    candidate: Optional[WaybackCandidate] = None
    error: Optional[str] = None


class WaybackSearcher:
    """
    Searches the Wayback Machine for broken links that have no local candidates.

    Uses `wayback_machine_downloader` (Ruby gem) as a subprocess.

    Parameters
    ----------
    original_url : str
        Root URL of the original site, e.g. "http://www.stclares.ac.uk/"
    input_dir : Path
        The archive being validated (used to compute relative paths).
    staging_dir : Path
        Directory where downloaded files are stored for review.
    workers : int
        Number of concurrent Wayback searches.
    verbose : bool
        If True, print progress to stderr.
    """

    def __init__(
        self,
        original_url: str,
        input_dir: Path,
        staging_dir: Optional[Path] = None,
        workers: int = 3,
        verbose: bool = True,
    ):
        self.original_url = original_url.rstrip("/") + "/"
        self.input_dir = input_dir.resolve()
        self.staging_dir = (staging_dir or Path("wayback_staging")).resolve()
        self.workers = workers
        self.verbose = verbose

        # Thread-safe results dict: broken_link_id → WaybackResult
        # Key is str(expected_path) for internal links
        self._results: dict[str, WaybackResult] = {}
        self._results_lock = threading.Lock()

    def _result_key(self, broken) -> str:
        """Stable key for a broken link."""
        return str(broken.expected_path) if broken.expected_path else broken.raw_href

    def get_result(self, broken) -> Optional[WaybackResult]:
        """Get the current Wayback result for a broken link (thread-safe)."""
        key = self._result_key(broken)
        with self._results_lock:
            return self._results.get(key)

    def _build_original_url(self, broken) -> Optional[str]:
        """
        Construct the original site URL for a missing file.

        Strips the archive root from the expected path to get the
        site-relative path, then prepends original_url.

        e.g.
          input_dir   = /htdocs/.../archive-2001-06-01
          expected    = /htdocs/.../archive-2001-06-01/photos/students6.jpg
          relative    = photos/students6.jpg
          result      = http://www.stclares.ac.uk/photos/students6.jpg
        """
        if not broken.expected_path:
            return None
        try:
            rel = broken.expected_path.resolve().relative_to(self.input_dir)
            rel_str = "/".join(rel.parts)
            return self.original_url + rel_str
        except ValueError:
            return None

    def _staging_path_for(self, original_url_path: str) -> Path:
        """
        Compute the staging path for a given original URL path.

        e.g. "photos/students6.jpg" → staging_dir/photos/students6.jpg
        """
        # Strip leading slash
        rel = original_url_path.lstrip("/")
        return self.staging_dir / rel

    def _check_gem_available(self) -> bool:
        """Check that wayback_machine_downloader is installed."""
        try:
            result = subprocess.run(
                ["wayback_machine_downloader", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _search_one(self, broken, original_url: str) -> WaybackResult:
        """
        Run wayback_machine_downloader for a single URL.

        Downloads to staging_dir. Returns a WaybackResult.
        """
        # Determine staging subdirectory (mirror the original URL path structure)
        from urllib.parse import urlparse
        parsed = urlparse(original_url)
        staged = self._staging_path_for(parsed.path)

        # wayback_machine_downloader downloads into a subdirectory structure
        # under --directory. We use the staging_dir as the root.
        staged.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "wayback_machine_downloader",
            original_url,
            "--exact-url",
            "--directory", str(self.staging_dir),
        ]

        self._print(f"      [Wayback] Searching: {original_url}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 2 minutes per file
            )
        except subprocess.TimeoutExpired:
            return WaybackResult(status=STATUS_ERROR, error="Timed out after 120s")
        except FileNotFoundError:
            return WaybackResult(
                status=STATUS_ERROR,
                error="wayback_machine_downloader not found. Install with: gem install wayback_machine_downloader"
            )

        # Parse output to find downloaded file and snapshot URL
        # wayback_machine_downloader outputs lines like:
        #   Downloading 1 file(s)...
        #   https://web.archive.org/web/20010615120000*/http://www.stclares.ac.uk/photos/students6.jpg
        #   Saved to: websites/www.stclares.ac.uk/photos/students6.jpg
        output = proc.stdout + proc.stderr
        wayback_url = None
        snapshot_date = "unknown"

        for line in output.splitlines():
            line = line.strip()
            if "web.archive.org/web/" in line:
                # Extract the snapshot URL
                parts = line.split()
                for part in parts:
                    if "web.archive.org/web/" in part:
                        wayback_url = part.strip()
                        # Extract date from URL: /web/20010615120000/
                        import re
                        m = re.search(r'/web/(\d{8})', wayback_url)
                        if m:
                            d = m.group(1)
                            snapshot_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                        break

        # Find the downloaded file — wayback_machine_downloader saves to
        # <staging_dir>/websites/<hostname>/<path>
        from urllib.parse import urlparse as _urlparse
        parsed2 = _urlparse(original_url)
        hostname = parsed2.netloc
        rel_path = parsed2.path.lstrip("/")
        candidate_path = self.staging_dir / "websites" / hostname / rel_path

        if candidate_path.exists() and candidate_path.is_file():
            self._print(f"      [Wayback] Found: {candidate_path}")
            return WaybackResult(
                status=STATUS_FOUND,
                candidate=WaybackCandidate(
                    staged_path=candidate_path,
                    wayback_url=wayback_url or f"https://web.archive.org/web/*/{original_url}",
                    original_url=original_url,
                    snapshot_date=snapshot_date,
                    confidence=85,
                ),
            )
        elif "No files found" in output or proc.returncode != 0:
            self._print(f"      [Wayback] Not found: {original_url}")
            return WaybackResult(status=STATUS_NOT_FOUND)
        else:
            # Unexpected output — treat as not found
            self._print(f"      [Wayback] No result for: {original_url}")
            return WaybackResult(status=STATUS_NOT_FOUND)

    def _worker(self, broken, original_url: str) -> None:
        """Thread worker: search and store result."""
        key = self._result_key(broken)
        result = self._search_one(broken, original_url)
        with self._results_lock:
            self._results[key] = result
        # Attach candidate to the broken link object if found
        if result.status == STATUS_FOUND and result.candidate:
            broken.wayback_candidate = result.candidate

    def search_all(self, broken_links: list) -> None:
        """
        Search the Wayback Machine for all broken links with no local candidates.

        Runs searches concurrently using a thread pool. Results are stored
        in self._results and can be polled via get_result().

        This method returns immediately after launching threads — callers
        should poll get_result() or wait for threads to finish.

        Parameters
        ----------
        broken_links : list of BrokenLink
            All broken links (only those with no local candidates and a
            resolvable expected_path are searched).
        """
        if not self._check_gem_available():
            self._print(
                "[WARNING] wayback_machine_downloader not found. "
                "Install with: gem install wayback_machine_downloader"
            )
            # Mark all as skipped
            for b in broken_links:
                if not b.candidates and not b.is_external:
                    key = self._result_key(b)
                    with self._results_lock:
                        self._results[key] = WaybackResult(
                            status=STATUS_ERROR,
                            error="wayback_machine_downloader not installed"
                        )
            return

        # Ensure staging dir exists
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        # Filter to links worth searching
        to_search = []
        for b in broken_links:
            if b.candidates or b.is_external:
                continue  # Has local candidates or is external — skip
            original_url = self._build_original_url(b)
            if not original_url:
                key = self._result_key(b)
                with self._results_lock:
                    self._results[key] = WaybackResult(status=STATUS_SKIPPED)
                continue
            # Mark as pending
            key = self._result_key(b)
            with self._results_lock:
                self._results[key] = WaybackResult(status=STATUS_PENDING)
            to_search.append((b, original_url))

        if not to_search:
            return

        self._print(
            f"      [Wayback] Searching for {len(to_search)} file(s) "
            f"with no local candidates..."
        )

        # Launch worker threads (up to self.workers at a time)
        semaphore = threading.Semaphore(self.workers)
        threads = []

        def _run(broken, url):
            with semaphore:
                self._worker(broken, url)

        for broken, url in to_search:
            t = threading.Thread(target=_run, args=(broken, url), daemon=True)
            threads.append(t)
            t.start()

        # Wait for all threads to complete
        for t in threads:
            t.join()

        found = sum(
            1 for r in self._results.values()
            if r.status == STATUS_FOUND
        )
        self._print(
            f"      [Wayback] Complete. Found {found} of {len(to_search)} file(s)."
        )

    def get_all_results(self) -> dict:
        """Return a snapshot of all results (thread-safe)."""
        with self._results_lock:
            return dict(self._results)

    def _print(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr, flush=True)
