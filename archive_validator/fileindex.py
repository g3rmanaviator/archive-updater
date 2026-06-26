"""
fileindex.py — Persistent SQLite file index for the replacement candidate search.

Replaces the per-run in-memory dict built by searcher.py with a persistent
SQLite database that is updated incrementally. On the first run the full
filesystem walk is performed and the database is populated. On subsequent
runs, only archive folders whose directory mtime has changed are re-scanned.

Schema
------
files
    id           INTEGER PRIMARY KEY
    archive      TEXT NOT NULL        -- immediate subdirectory name under search_dir
    archive_date TEXT                 -- parsed from folder name (YYYY-MM-DD), nullable
    rel_path     TEXT NOT NULL        -- path relative to archive root, lowercase, /
    filename     TEXT NOT NULL        -- lowercase filename only
    abs_path     TEXT NOT NULL        -- absolute filesystem path (unique)
    mtime        REAL NOT NULL        -- file st_mtime (float, Unix timestamp)
    file_size    INTEGER              -- file size in bytes

archive_mtimes
    archive      TEXT PRIMARY KEY     -- archive folder name
    dir_mtime    REAL NOT NULL        -- mtime of the archive directory itself

meta
    key          TEXT PRIMARY KEY
    value        TEXT

Usage
-----
    from archive_validator.fileindex import FileIndex
    from pathlib import Path

    idx = FileIndex(
        db_path=Path("search_index.db"),
        search_dir=Path("/htdocs/alumni/mirror"),
        input_dir=Path("/htdocs/alumni/mirror/archives/archive-2001-06-01"),
        verbose=True,
    )
    idx.update()   # incremental update (fast on subsequent runs)

    # Query by filename (case-insensitive — stored lowercase)
    rows = idx.find_by_filename("1.gif")
    # → list of FileRow(archive, archive_date, rel_path, abs_path, mtime, file_size)

    # Query by relative path within an archive
    rows = idx.find_by_relpath("images/logo.gif")
    # → list of FileRow(...)

    # Count all versions of a filename across all archives
    count = idx.count_by_filename("1.gif")
"""

import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# Current schema version — bump when schema changes require a rebuild
_SCHEMA_VERSION = "2"

# Regex to extract a date from archive folder names like:
#   archive-2001-06-01
#   archive-1998-09-15
#   2001-06-01
_ARCHIVE_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')


@dataclass
class FileRow:
    """A single row from the files table."""
    archive: str
    archive_date: Optional[str]   # "YYYY-MM-DD" or None
    rel_path: str                 # lowercase, forward slashes, relative to archive root
    abs_path: str                 # absolute filesystem path (original case)
    mtime: float
    file_size: int

    @property
    def filename(self) -> str:
        """Lowercase filename (last component of rel_path)."""
        return self.rel_path.rsplit("/", 1)[-1]

    @property
    def archive_date_parsed(self) -> Optional[date]:
        """Return archive_date as a date object, or None."""
        if self.archive_date:
            try:
                return date.fromisoformat(self.archive_date)
            except ValueError:
                pass
        return None


def _parse_archive_date(folder_name: str) -> Optional[str]:
    """
    Extract a YYYY-MM-DD date string from an archive folder name.

    Examples:
        "archive-2001-06-01"  → "2001-06-01"
        "archive-1998-09-15"  → "1998-09-15"
        "mirror"              → None
    """
    m = _ARCHIVE_DATE_RE.search(folder_name)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(year, month, day)  # validate
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


class FileIndex:
    """
    Persistent SQLite index of all files under a search directory.

    Parameters
    ----------
    db_path : Path
        Path to the SQLite database file. Created if it does not exist.
    search_dir : Path
        Root directory containing archive folders to index.
    input_dir : Path
        The archive currently being validated — excluded from the index.
    verbose : bool
        If True, print progress to stderr.
    """

    def __init__(
        self,
        db_path: Path,
        search_dir: Path,
        input_dir: Path,
        verbose: bool = True,
    ):
        self.db_path = db_path.resolve()
        self.search_dir = search_dir.resolve()
        self.input_dir = input_dir.resolve()
        self.verbose = verbose
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open (or return cached) database connection."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            # Performance settings
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-32000")   # 32 MB cache
            conn.execute("PRAGMA temp_store=MEMORY")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> bool:
        """
        Create tables if they don't exist.

        Returns True if the schema was freshly created or upgraded
        (caller should do a full rebuild).
        """
        conn = self._connect()
        cur = conn.cursor()

        # Check schema version
        cur.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        cur.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        existing_version = row[0] if row else None

        if existing_version != _SCHEMA_VERSION:
            # Drop and recreate everything
            self._print(
                f"      [Index] Schema version mismatch "
                f"({existing_version!r} → {_SCHEMA_VERSION!r}). Rebuilding..."
            )
            cur.execute("DROP TABLE IF EXISTS files")
            cur.execute("DROP TABLE IF EXISTS archive_mtimes")
            cur.execute("DROP TABLE IF EXISTS meta")
            conn.commit()
            needs_rebuild = True
        else:
            needs_rebuild = False

        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id           INTEGER PRIMARY KEY,
                archive      TEXT NOT NULL,
                archive_date TEXT,
                rel_path     TEXT NOT NULL,
                filename     TEXT NOT NULL,
                abs_path     TEXT NOT NULL,
                mtime        REAL NOT NULL,
                file_size    INTEGER,
                UNIQUE(abs_path)
            )
        """)

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_filename ON files(filename)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_relpath ON files(archive, rel_path)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive ON files(archive)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS archive_mtimes (
                archive   TEXT PRIMARY KEY,
                dir_mtime REAL NOT NULL
            )
        """)

        cur.execute(
            "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,)
        )
        conn.commit()
        return needs_rebuild

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    def update(self, force_rebuild: bool = False) -> dict:
        """
        Incrementally update the index.

        Scans archive folders under search_dir (excluding input_dir).
        Folders whose directory mtime has not changed since the last
        index run are skipped entirely.

        Parameters
        ----------
        force_rebuild : bool
            If True, ignore cached mtimes and re-index everything.

        Returns
        -------
        dict with keys: total_files, added, updated, deleted, skipped_folders
        """
        needs_rebuild = self._ensure_schema()
        if needs_rebuild:
            force_rebuild = True

        conn = self._connect()

        # Find all immediate subdirectories of search_dir (= archive folders)
        try:
            archive_dirs = sorted(
                d for d in self.search_dir.iterdir()
                if d.is_dir() and d.resolve() != self.input_dir
            )
        except PermissionError as e:
            self._print(f"[WARNING] Cannot read search-dir: {e}")
            return {"total_files": 0, "added": 0, "updated": 0,
                    "deleted": 0, "skipped_folders": 0}

        stats = {"total_files": 0, "added": 0, "updated": 0,
                 "deleted": 0, "skipped_folders": 0}

        for archive_dir in archive_dirs:
            archive_name = archive_dir.name
            archive_date = _parse_archive_date(archive_name)

            # Check if this archive folder has changed since last index
            try:
                current_dir_mtime = archive_dir.stat().st_mtime
            except OSError:
                continue

            if not force_rebuild:
                cur = conn.execute(
                    "SELECT dir_mtime FROM archive_mtimes WHERE archive=?",
                    (archive_name,)
                )
                row = cur.fetchone()
                if row and abs(row[0] - current_dir_mtime) < 0.001:
                    # Directory mtime unchanged — skip this archive
                    stats["skipped_folders"] += 1
                    # Still count its files for the total
                    cur2 = conn.execute(
                        "SELECT COUNT(*) FROM files WHERE archive=?",
                        (archive_name,)
                    )
                    stats["total_files"] += cur2.fetchone()[0]
                    continue

            self._print(f"      [Index] Scanning {archive_name}...")
            self._index_archive(
                conn, archive_dir, archive_name, archive_date, stats
            )

            # Update the stored directory mtime
            conn.execute(
                "INSERT OR REPLACE INTO archive_mtimes VALUES (?, ?)",
                (archive_name, current_dir_mtime)
            )
            conn.commit()

        # Update meta
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_updated', ?)",
            (datetime.now().isoformat(),)
        )
        conn.commit()

        return stats

    def _index_archive(
        self,
        conn: sqlite3.Connection,
        archive_dir: Path,
        archive_name: str,
        archive_date: Optional[str],
        stats: dict,
    ) -> None:
        """
        Re-index a single archive folder.

        Upserts new/changed files and deletes removed files.
        """
        # Collect all current files in this archive
        current_abs_paths: set[str] = set()
        rows_to_upsert = []

        try:
            for file_path in archive_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    st = file_path.stat()
                except OSError:
                    continue

                abs_path_str = str(file_path)
                current_abs_paths.add(abs_path_str)

                try:
                    rel = file_path.relative_to(archive_dir)
                    rel_str = "/".join(rel.parts).lower()
                except ValueError:
                    rel_str = file_path.name.lower()

                filename = file_path.name.lower()
                mtime = st.st_mtime
                file_size = st.st_size

                rows_to_upsert.append((
                    archive_name,
                    archive_date,
                    rel_str,
                    filename,
                    abs_path_str,
                    mtime,
                    file_size,
                ))
                stats["total_files"] += 1

        except PermissionError as e:
            self._print(f"[WARNING] Cannot read {archive_name}: {e}")
            return

        # Bulk upsert
        conn.executemany("""
            INSERT INTO files
                (archive, archive_date, rel_path, filename, abs_path, mtime, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(abs_path) DO UPDATE SET
                archive      = excluded.archive,
                archive_date = excluded.archive_date,
                rel_path     = excluded.rel_path,
                filename     = excluded.filename,
                mtime        = excluded.mtime,
                file_size    = excluded.file_size
        """, rows_to_upsert)

        stats["added"] += len(rows_to_upsert)

        # Delete rows for files that no longer exist in this archive
        cur = conn.execute(
            "SELECT abs_path FROM files WHERE archive=?", (archive_name,)
        )
        stored_paths = {row[0] for row in cur.fetchall()}
        removed = stored_paths - current_abs_paths
        if removed:
            conn.executemany(
                "DELETE FROM files WHERE abs_path=?",
                [(p,) for p in removed]
            )
            stats["deleted"] += len(removed)

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def find_by_filename(self, filename: str) -> list[FileRow]:
        """
        Find all indexed files with the given filename (case-insensitive).

        Parameters
        ----------
        filename : str
            Filename to search for (any case — normalized internally).

        Returns
        -------
        list of FileRow, ordered by archive_date DESC (most recent first).
        """
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT archive, archive_date, rel_path, abs_path, mtime, file_size
            FROM files
            WHERE filename = ?
            ORDER BY archive_date DESC NULLS LAST, archive
            """,
            (filename.lower(),)
        )
        return [FileRow(*row) for row in cur.fetchall()]

    def find_by_relpath(self, rel_path: str) -> list[FileRow]:
        """
        Find all indexed files with the given relative path (case-insensitive).

        The rel_path should be relative to the archive root, using forward
        slashes. e.g. "images/logo.gif"

        Returns
        -------
        list of FileRow, ordered by archive_date DESC.
        """
        conn = self._connect()
        cur = conn.execute(
            """
            SELECT archive, archive_date, rel_path, abs_path, mtime, file_size
            FROM files
            WHERE rel_path = ?
            ORDER BY archive_date DESC NULLS LAST, archive
            """,
            (rel_path.lower(),)
        )
        return [FileRow(*row) for row in cur.fetchall()]

    def count_by_filename(self, filename: str) -> int:
        """Return the total number of indexed files with the given filename."""
        conn = self._connect()
        cur = conn.execute(
            "SELECT COUNT(*) FROM files WHERE filename=?",
            (filename.lower(),)
        )
        return cur.fetchone()[0]

    def total_file_count(self) -> int:
        """Return the total number of files in the index."""
        conn = self._connect()
        cur = conn.execute("SELECT COUNT(*) FROM files")
        return cur.fetchone()[0]

    def archive_count(self) -> int:
        """Return the number of distinct archives in the index."""
        conn = self._connect()
        cur = conn.execute("SELECT COUNT(DISTINCT archive) FROM files")
        return cur.fetchone()[0]

    def last_updated(self) -> Optional[str]:
        """Return the ISO timestamp of the last index update, or None."""
        conn = self._connect()
        cur = conn.execute(
            "SELECT value FROM meta WHERE key='last_updated'"
        )
        row = cur.fetchone()
        return row[0] if row else None

    def all_archive_names(self) -> list[str]:
        """Return a sorted list of all archive folder names in the index."""
        conn = self._connect()
        cur = conn.execute(
            "SELECT DISTINCT archive FROM files ORDER BY archive"
        )
        return [row[0] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _print(self, message: str) -> None:
        if self.verbose:
            print(message, file=sys.stderr, flush=True)
