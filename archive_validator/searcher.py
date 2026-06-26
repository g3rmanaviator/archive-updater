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

Date proximity bonus (applied on top of base confidence):
  Same archive, different subfolder                       → +5
  Within 3 months of the target archive date             → +4
  Within 1 year                                          → +3
  Within 2 years                                         → +2
  More than 2 years away                                 → +0

The search index is backed by a persistent SQLite database (FileIndex)
that is updated incrementally — only re-scanning archive folders whose
directory mtime has changed since the last run.
"""

import sys
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .detector import BrokenLink
from .fileindex import FileIndex, FileRow, _parse_archive_date


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
    archive_date : str or None
        Date of the candidate's archive (YYYY-MM-DD), if parseable.
    match_type : str
        Description of how the match was found.
    confidence : int
        Confidence score 0–100.
    total_versions : int
        Total number of files with the same filename across all archives.
    """
    local_path: Path
    public_url: Optional[str]
    archive_folder: str
    archive_date: Optional[str]
    match_type: str
    confidence: int
    total_versions: int = 0

    @property
    def display_path(self) -> str:
        """Human-readable path for the report."""
        return self.public_url or str(self.local_path)


def _date_proximity_bonus(
    target_date: Optional[date],
    candidate_date_str: Optional[str],
    same_archive: bool,
) -> int:
    """
    Compute a small date-proximity bonus for a candidate.

    Parameters
    ----------
    target_date : date or None
        The date of the archive being validated.
    candidate_date_str : str or None
        The archive_date of the candidate ("YYYY-MM-DD" or None).
    same_archive : bool
        True if the candidate is in the same archive as the broken link
        (different subfolder).

    Returns
    -------
    int bonus (0–5)
    """
    if same_archive:
        return 5
    if target_date is None or candidate_date_str is None:
        return 0
    try:
        cand_date = date.fromisoformat(candidate_date_str)
    except ValueError:
        return 0
    days_diff = abs((cand_date - target_date).days)
    if days_diff <= 90:
        return 4
    if days_diff <= 365:
        return 3
    if days_diff <= 730:
        return 2
    return 0


class ReplacementSearcher:
    """
    Finds replacement candidates for broken links using a persistent
    SQLite file index (FileIndex).

    Parameters
    ----------
    search_dir : Path
        Root directory containing all archive folders.
    input_dir : Path
        The archive being validated (excluded from search results).
    db_path : Path
        Path to the SQLite index database.
    search_base_url : str or None
        Public URL prefix for search_dir. Used to construct clickable
        candidate URLs. e.g. "https://example.org/mirror/"
    max_candidates : int
        Maximum number of candidates to return per broken link.
    case_insensitive : bool
        If True, use case-insensitive matching throughout.
    fuzzy : bool
        If True, enable fuzzy filename matching as a fallback strategy.
    force_rebuild : bool
        If True, force a full rebuild of the file index.
    verbose : bool
        If True, print progress to stderr.
    """

    def __init__(
        self,
        search_dir: Path,
        input_dir: Path,
        db_path: Path,
        search_base_url: str = None,
        max_candidates: int = 5,
        case_insensitive: bool = False,
        fuzzy: bool = False,
        force_rebuild: bool = False,
        verbose: bool = True,
    ):
        self.search_dir = search_dir.resolve()
        self.input_dir = input_dir.resolve()
        self.max_candidates = max_candidates
        self.case_insensitive = case_insensitive
        self.fuzzy = fuzzy
        self.force_rebuild = force_rebuild
        self.verbose = verbose

        # Normalize search_base_url
        if search_base_url:
            self.search_base_url = search_base_url.rstrip("/") + "/"
        else:
            self.search_base_url = None

        # Parse the target archive's date from its folder name
        self._target_archive_date: Optional[date] = None
        date_str = _parse_archive_date(input_dir.name)
        if date_str:
            try:
                self._target_archive_date = date.fromisoformat(date_str)
            except ValueError:
                pass

        # FileIndex instance (opened lazily)
        self._index: Optional[FileIndex] = None
        self._db_path = db_path
        self._index_built = False

        # Fuzzy search cache: filename_lower → list of (FileRow, ratio)
        # Built lazily on first fuzzy search
        self._fuzzy_cache: Optional[list[tuple[str, str, str]]] = None

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def build_index(self) -> None:
        """
        Update the persistent file index.

        On the first run this walks all archive folders and populates the
        database. On subsequent runs, only changed folders are re-scanned.
        """
        self._print("      Building search index from other archive folders...")

        self._index = FileIndex(
            db_path=self._db_path,
            search_dir=self.search_dir,
            input_dir=self.input_dir,
            verbose=self.verbose,
        )

        stats = self._index.update(force_rebuild=self.force_rebuild)

        total = stats["total_files"]
        skipped = stats["skipped_folders"]
        added = stats["added"]
        deleted = stats["deleted"]

        if skipped > 0 and added == 0 and deleted == 0:
            self._print(
                f"      Index up to date: {total:,} file(s) across "
                f"{self._index.archive_count()} archive folder(s) "
                f"({skipped} folder(s) unchanged, skipped)."
            )
        else:
            self._print(
                f"      Indexed {total:,} file(s) across "
                f"{self._index.archive_count()} archive folder(s)."
            )
            if added or deleted:
                self._print(
                    f"      Changes: +{added} added, -{deleted} removed."
                )

        self._index_built = True

    # ------------------------------------------------------------------
    # Candidate search
    # ------------------------------------------------------------------

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

        candidates: dict[str, Candidate] = {}  # abs_path → best candidate

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
            rows = self._index.find_by_relpath(target_rel_str)
            for row in rows:
                same_archive = (row.archive == self.input_dir.name)
                bonus = _date_proximity_bonus(
                    self._target_archive_date, row.archive_date, same_archive
                )
                self._add_candidate(
                    candidates, row,
                    "exact relative path in another archive",
                    95 + bonus,
                )

        # --- Strategy 2: Extension case difference (.JPG vs .jpg) (80%) ---
        target_stem = Path(target_name_lower).stem
        target_ext_lower = Path(target_name_lower).suffix.lower()

        # We need all files whose lowercase filename has the same stem+ext
        # but whose actual filename differs in case from the target.
        # Query by filename (already lowercase in DB) — then filter.
        rows = self._index.find_by_filename(target_name_lower)
        for row in rows:
            # The stored filename is lowercase; the actual file may differ in case.
            # Strategy 2 applies when the actual file's name differs from target_name.
            actual_name = Path(row.abs_path).name
            if actual_name != target_name:
                cand_stem = Path(row.filename).stem
                cand_ext = Path(row.filename).suffix.lower()
                if cand_stem == target_stem and cand_ext == target_ext_lower:
                    same_archive = (row.archive == self.input_dir.name)
                    bonus = _date_proximity_bonus(
                        self._target_archive_date, row.archive_date, same_archive
                    )
                    self._add_candidate(
                        candidates, row,
                        "extension case difference",
                        80 + bonus,
                    )

        # --- Strategy 3: Exact filename match (different location) (70%) ---
        rows = self._index.find_by_filename(target_name_lower)
        for row in rows:
            actual_name = Path(row.abs_path).name
            if actual_name == target_name:
                same_archive = (row.archive == self.input_dir.name)
                bonus = _date_proximity_bonus(
                    self._target_archive_date, row.archive_date, same_archive
                )
                self._add_candidate(
                    candidates, row,
                    "exact filename match",
                    70 + bonus,
                )

        # --- Strategy 4: URL-decoded filename match (65%) ---
        if target_name_decoded != target_name:
            rows = self._index.find_by_filename(target_name_decoded_lower)
            for row in rows:
                same_archive = (row.archive == self.input_dir.name)
                bonus = _date_proximity_bonus(
                    self._target_archive_date, row.archive_date, same_archive
                )
                self._add_candidate(
                    candidates, row,
                    "URL-decoded filename match",
                    65 + bonus,
                )

        # --- Strategy 5: Case-insensitive filename match (60%) ---
        rows = self._index.find_by_filename(target_name_lower)
        for row in rows:
            same_archive = (row.archive == self.input_dir.name)
            bonus = _date_proximity_bonus(
                self._target_archive_date, row.archive_date, same_archive
            )
            self._add_candidate(
                candidates, row,
                "case-insensitive filename match",
                60 + bonus,
            )

        # --- Strategy 6: Fuzzy filename match (20–50%) ---
        if self.fuzzy and len(candidates) < self.max_candidates:
            fuzzy_results = self._fuzzy_search(target_name_lower)
            for row, ratio in fuzzy_results:
                confidence = int(ratio * 50)
                same_archive = (row.archive == self.input_dir.name)
                bonus = _date_proximity_bonus(
                    self._target_archive_date, row.archive_date, same_archive
                )
                self._add_candidate(
                    candidates, row,
                    f"fuzzy match (similarity {ratio:.0%})",
                    confidence + bonus,
                )

        # Count total versions of this filename across all archives
        total_versions = self._index.count_by_filename(target_name_lower)

        # Attach total_versions to all candidates
        for c in candidates.values():
            c.total_versions = total_versions

        # Sort by confidence descending, then by archive date proximity,
        # then by archive name for stability
        sorted_candidates = sorted(
            candidates.values(),
            key=lambda c: (-c.confidence, c.archive_folder, str(c.local_path))
        )

        return sorted_candidates[:self.max_candidates]

    def _add_candidate(
        self,
        candidates: dict,
        row: FileRow,
        match_type: str,
        confidence: int,
    ) -> None:
        """
        Add a candidate to the dict, keeping only the highest-confidence
        entry per unique file path.
        """
        abs_path_str = row.abs_path
        existing = candidates.get(abs_path_str)
        if existing is None or existing.confidence < confidence:
            candidates[abs_path_str] = Candidate(
                local_path=Path(row.abs_path),
                public_url=self._build_public_url(Path(row.abs_path)),
                archive_folder=row.archive,
                archive_date=row.archive_date,
                match_type=match_type,
                confidence=min(confidence, 100),  # cap at 100
            )

    def _build_public_url(self, file_path: Path) -> Optional[str]:
        """
        Construct a public URL for a candidate file using search_base_url.
        """
        if not self.search_base_url:
            return None
        try:
            rel = file_path.relative_to(self.search_dir)
            rel_str = "/".join(rel.parts)
            return self.search_base_url + rel_str
        except ValueError:
            return None

    def _fuzzy_search(self, target_lower: str) -> list[tuple[FileRow, float]]:
        """
        Search for files with similar names using difflib SequenceMatcher.

        Only returns matches with ratio ≥ 0.6.
        Builds a cache of all distinct filenames on first call.
        """
        if self._fuzzy_cache is None:
            # Build a list of all distinct (filename, archive, archive_date)
            # from the index for fuzzy comparison
            conn = self._index._connect()
            cur = conn.execute(
                "SELECT DISTINCT filename, archive, archive_date FROM files"
            )
            self._fuzzy_cache = [(r[0], r[1], r[2]) for r in cur.fetchall()]

        results = []
        for filename_lower, archive, archive_date in self._fuzzy_cache:
            if filename_lower == target_lower:
                continue
            ratio = SequenceMatcher(None, target_lower, filename_lower).ratio()
            if ratio >= 0.6:
                # Fetch the actual file rows for this filename
                rows = self._index.find_by_filename(filename_lower)
                for row in rows:
                    results.append((row, ratio))

        results.sort(key=lambda x: -x[1])
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

        self._print(
            f"      Searching for replacement candidates for "
            f"{len(broken_links)} broken link(s)..."
        )

        with_candidates = 0
        for broken in broken_links:
            broken.candidates = self.find_candidates(broken)
            if broken.candidates:
                with_candidates += 1

        self._print(
            f"      Found candidates for {with_candidates} of "
            f"{len(broken_links)} broken link(s)."
        )

        # Close the index connection when done
        if self._index:
            self._index.close()

        return broken_links

    def _print(self, message: str) -> None:
        """Print a progress message to stderr."""
        if self.verbose:
            print(message, file=sys.stderr, flush=True)
