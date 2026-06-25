"""
searcher.py — Replacement candidate search engine.

For each broken link, this module searches other archive folders under
--search-dir to find possible replacement files.

Matching strategies (DD-008), in priority order:
  1. Exact relative path match in another archive         → 95% confidence
  2. Extension case difference only (.JPG vs .jpg)        → 80% confidence
  3. Exact filename match (different location)            → 70% confidence
  4. URL-decoded filename match                           → 65% confidence
  5. Case-insensitive filename match                      → 60% confidence
  6. Fuzzy filename match (difflib, ratio ≥ 0.6)         → 20–50% confidence

The search index is built once from all files under --search-dir
(excluding the input archive) and reused for all broken links.
"""

import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .detector import BrokenLink


@dataclass
class Candidate:
    """
    A replacement candidate for a broken link.

    Attributes
    ----------
    local_path : Path
        Absolute filesystem path of the candidate file.
    public_url : str or None
        Clickable public URL for the candidate (if search_base_url provided).
    archive_folder : str
        Name of the archive folder where the candidate was found.
    match_type : str
        Description of how the match was found.
    confidence : int
        Confidence score 0–100.
    """
    local_path: Path
    public_url: Optional[str]
    archive_folder: str
    match_type: str
    confidence: int

    @property
    def display_path(self) -> str:
        """Human-readable path for the report."""
        return self.public_url or str(self.local_path)


class ReplacementSearcher:
    """
    Builds a search index from all archive folders and finds replacement
    candidates for broken links.

    Parameters
    ----------
    search_dir : Path
        Root directory containing all archive folders.
    input_dir : Path
        The archive being validated (excluded from search results).
    search_base_url : str or None
        Public URL prefix for search_dir. Used to construct clickable
        candidate URLs. e.g. "https://example.org/mirror/archives/"
    max_candidates : int
        Maximum number of candidates to return per broken link.
    case_insensitive : bool
        If True, use case-insensitive matching throughout.
    fuzzy : bool
        If True, enable fuzzy filename matching as a fallback strategy.
    verbose : bool
        If True, print progress to stderr.
    """

    def __init__(
        self,
        search_dir: Path,
        input_dir: Path,
        search_base_url: str = None,
        max_candidates: int = 5,
        case_insensitive: bool = False,
        fuzzy: bool = False,
        verbose: bool = True,
    ):
        self.search_dir = search_dir.resolve()
        self.input_dir = input_dir.resolve()
        self.max_candidates = max_candidates
        self.case_insensitive = case_insensitive
        self.fuzzy = fuzzy
        self.verbose = verbose

        # Normalize search_base_url
        if search_base_url:
            self.search_base_url = search_base_url.rstrip("/") + "/"
        else:
            self.search_base_url = None

        # The search index: built once, reused for all broken links
        # Maps lowercase filename → list of (Path, archive_folder_name)
        self._index_by_name: dict[str, list[tuple[Path, str]]] = {}
        # Maps (archive_folder, relative_path_from_archive_root) → Path
        self._index_by_relpath: dict[tuple[str, str], Path] = {}
        # All archive folder names (for display)
        self._archive_folders: list[str] = []

        self._index_built = False

    def build_index(self) -> None:
        """
        Recursively scan all archive folders under search_dir (excluding
        input_dir) and build the search index.

        This is called once before searching begins.
        """
        self._print("      Building search index from other archive folders...")

        if not self.search_dir.exists():
            self._print(f"[WARNING] --search-dir does not exist: {self.search_dir}")
            self._index_built = True
            return

        # Find all immediate subdirectories of search_dir = archive folders
        try:
            archive_dirs = [
                d for d in sorted(self.search_dir.iterdir())
                if d.is_dir() and d.resolve() != self.input_dir
            ]
        except PermissionError as e:
            self._print(f"[WARNING] Cannot read search-dir: {e}")
            self._index_built = True
            return

        self._archive_folders = [d.name for d in archive_dirs]
        total_files = 0

        for archive_dir in archive_dirs:
            archive_name = archive_dir.name
            try:
                for file_path in archive_dir.rglob("*"):
                    if not file_path.is_file():
                        continue

                    total_files += 1

                    # Index by lowercase filename
                    lower_name = file_path.name.lower()
                    if lower_name not in self._index_by_name:
                        self._index_by_name[lower_name] = []
                    self._index_by_name[lower_name].append((file_path, archive_name))

                    # Index by (archive_name, relative_path_from_archive_root)
                    try:
                        rel = file_path.relative_to(archive_dir)
                        rel_str = "/".join(rel.parts).lower()
                        self._index_by_relpath[(archive_name, rel_str)] = file_path
                    except ValueError:
                        pass

            except PermissionError as e:
                self._print(f"[WARNING] Cannot read archive folder {archive_name}: {e}")

        self._print(f"      Indexed {total_files} file(s) across "
                    f"{len(archive_dirs)} archive folder(s).")
        self._index_built = True

    def find_candidates(self, broken: BrokenLink) -> list[Candidate]:
        """
        Find replacement candidates for a single broken link.

        Parameters
        ----------
        broken : BrokenLink
            The broken link to find replacements for.

        Returns
        -------
        list of Candidate
            Up to max_candidates candidates, sorted by confidence descending.
        """
        if not self._index_built:
            self.build_index()

        if broken.expected_path is None:
            return []

        candidates: dict[Path, Candidate] = {}  # path → best candidate

        target_path = broken.expected_path
        target_name = target_path.name
        target_name_lower = target_name.lower()
        target_name_decoded = unquote(target_name)
        target_name_decoded_lower = target_name_decoded.lower()

        # Determine the relative path of the target within the input archive
        # e.g. "images/logo.gif" — used for strategy 1
        try:
            target_rel = target_path.relative_to(self.input_dir)
            target_rel_str = "/".join(target_rel.parts).lower()
        except ValueError:
            target_rel_str = None

        # --- Strategy 1: Exact relative path in another archive (95%) ---
        if target_rel_str:
            for archive_name in self._archive_folders:
                key = (archive_name, target_rel_str)
                if key in self._index_by_relpath:
                    found = self._index_by_relpath[key]
                    self._add_candidate(
                        candidates, found, archive_name,
                        "exact relative path in another archive", 95
                    )

        # --- Strategy 2: Extension case difference (.JPG vs .jpg) (80%) ---
        # The target name and a candidate differ only in extension case
        target_stem = Path(target_name_lower).stem
        target_ext_lower = Path(target_name_lower).suffix.lower()

        for lower_name, entries in self._index_by_name.items():
            cand_stem = Path(lower_name).stem
            cand_ext = Path(lower_name).suffix.lower()
            if (cand_stem == target_stem and
                    cand_ext == target_ext_lower and
                    lower_name != target_name_lower):
                # Same stem and extension when lowercased, but different case
                for file_path, archive_name in entries:
                    if file_path.name != target_name:  # actual case differs
                        self._add_candidate(
                            candidates, file_path, archive_name,
                            "extension case difference", 80
                        )

        # --- Strategy 3: Exact filename match (different location) (70%) ---
        if target_name in [e[0].name for e in self._index_by_name.get(target_name_lower, [])]:
            for file_path, archive_name in self._index_by_name.get(target_name_lower, []):
                if file_path.name == target_name:
                    self._add_candidate(
                        candidates, file_path, archive_name,
                        "exact filename match", 70
                    )

        # --- Strategy 4: URL-decoded filename match (65%) ---
        if target_name_decoded != target_name:
            decoded_lower = target_name_decoded_lower
            for file_path, archive_name in self._index_by_name.get(decoded_lower, []):
                self._add_candidate(
                    candidates, file_path, archive_name,
                    "URL-decoded filename match", 65
                )

        # --- Strategy 5: Case-insensitive filename match (60%) ---
        for file_path, archive_name in self._index_by_name.get(target_name_lower, []):
            self._add_candidate(
                candidates, file_path, archive_name,
                "case-insensitive filename match", 60
            )

        # --- Strategy 6: Fuzzy filename match (20–50%) ---
        if self.fuzzy and len(candidates) < self.max_candidates:
            fuzzy_results = self._fuzzy_search(target_name_lower)
            for file_path, archive_name, ratio in fuzzy_results:
                confidence = int(ratio * 50)  # ratio 0.6–1.0 → confidence 30–50
                self._add_candidate(
                    candidates, file_path, archive_name,
                    f"fuzzy match (similarity {ratio:.0%})", confidence
                )

        # Sort by confidence descending, then by archive name for stability
        sorted_candidates = sorted(
            candidates.values(),
            key=lambda c: (-c.confidence, c.archive_folder, str(c.local_path))
        )

        return sorted_candidates[:self.max_candidates]

    def _add_candidate(
        self,
        candidates: dict,
        file_path: Path,
        archive_name: str,
        match_type: str,
        confidence: int,
    ) -> None:
        """
        Add a candidate to the dict, keeping only the highest-confidence
        entry per unique file path.
        """
        existing = candidates.get(file_path)
        if existing is None or existing.confidence < confidence:
            candidates[file_path] = Candidate(
                local_path=file_path,
                public_url=self._build_public_url(file_path),
                archive_folder=archive_name,
                match_type=match_type,
                confidence=confidence,
            )

    def _build_public_url(self, file_path: Path) -> Optional[str]:
        """
        Construct a public URL for a candidate file using search_base_url.

        URL construction: search_base_url + relative_path_from_search_dir
        e.g. https://example.org/mirror/archives/ + archive-2001/images/logo.gif
        """
        if not self.search_base_url:
            return None
        try:
            rel = file_path.relative_to(self.search_dir)
            rel_str = "/".join(rel.parts)
            return self.search_base_url + rel_str
        except ValueError:
            return None

    def _fuzzy_search(self, target_lower: str) -> list[tuple[Path, str, float]]:
        """
        Search for files with similar names using difflib SequenceMatcher.

        Only returns matches with ratio ≥ 0.6.

        Returns list of (file_path, archive_name, ratio).
        """
        results = []
        for lower_name, entries in self._index_by_name.items():
            ratio = SequenceMatcher(None, target_lower, lower_name).ratio()
            if ratio >= 0.6 and lower_name != target_lower:
                for file_path, archive_name in entries:
                    results.append((file_path, archive_name, ratio))

        # Sort by ratio descending
        results.sort(key=lambda x: -x[2])
        return results

    def search_all(self, broken_links: list[BrokenLink]) -> list[BrokenLink]:
        """
        Find replacement candidates for all broken links.

        Populates the `candidates` field on each BrokenLink in place.

        Parameters
        ----------
        broken_links : list of BrokenLink

        Returns
        -------
        list of BrokenLink
            Same list with candidates populated.
        """
        if not broken_links:
            return broken_links

        if not self._index_built:
            self.build_index()

        self._print(f"      Searching for replacement candidates for "
                    f"{len(broken_links)} broken link(s)...")

        with_candidates = 0
        for broken in broken_links:
            broken.candidates = self.find_candidates(broken)
            if broken.candidates:
                with_candidates += 1

        self._print(f"      Found candidates for {with_candidates} of "
                    f"{len(broken_links)} broken link(s).")

        return broken_links

    def _print(self, message: str) -> None:
        """Print a progress message to stderr."""
        if self.verbose:
            print(message, file=sys.stderr, flush=True)
