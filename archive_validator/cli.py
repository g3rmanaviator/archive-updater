"""
cli.py -- Command-line interface and main orchestration logic.

Parses arguments, wires together all components, and runs the full
validation pipeline:

  1. Crawl: find HTML files and extract links
  2. Detect: check internal links for broken targets
  3. Search: find replacement candidates in other archives
  4. Report: generate HTML, JSON, and/or CSV output

Exit codes (DD-012):
  0 = success, no broken links found
  1 = broken links were found
  2 = tool error (bad arguments, unreadable directory, etc.)
"""

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from .crawler import Crawler
from .detector import DeadLinkDetector
from .extractor import LinkExtractor
from .reporter import ReportSummary, generate_csv_report, generate_html_report, generate_json_report
from .resolver import URLResolver
from .searcher import ReplacementSearcher
from .wayback import WaybackSearcher


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="archive-validator",
        description=(
            "Validate archived website snapshots by detecting broken internal links "
            "and finding replacement candidates in other archive folders."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic local scan
  python archive_validator.py --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01

  # Full scan with public URLs and candidate search
  python archive_validator.py \\
    --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \\
    --base-url https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/ \\
    --search-dir /htdocs/alumni/mirror/archives \\
    --search-base-url https://sugarhouse.stclaresalumni.org/mirror/archives/ \\
    --output report.html \\
    --json-output report.json \\
    --csv-output report.csv

  # Check external links too, with fuzzy matching
  python archive_validator.py \\
    --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \\
    --search-dir /htdocs/alumni/mirror/archives \\
    --external --fuzzy --max-candidates 10

  # Only check HTML and image files, case-insensitive
  python archive_validator.py \\
    --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \\
    --include-ext .html,.htm,.jpg,.gif,.png \\
    --case-insensitive

  # Ignore cgi-bin and tracking pixel paths
  python archive_validator.py \\
    --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \\
    --ignore-pattern "cgi-bin" --ignore-pattern "counter\\.gif"
""",
    )

    parser.add_argument(
        "--version", action="version", version=f"archive-validator {__version__}"
    )

    # --- Required ---
    parser.add_argument(
        "--input-dir",
        required=True,
        metavar="PATH",
        help="Archive snapshot directory to validate (required).",
    )

    # --- URL options ---
    url_group = parser.add_argument_group("URL options")
    url_group.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help=(
            "Public URL corresponding to --input-dir. "
            "Enables clickable source links in the report and recognition of "
            "absolute URLs as internal links. "
            "Example: https://example.org/mirror/archives/archive-1998-06-01/"
        ),
    )
    url_group.add_argument(
        "--search-base-url",
        metavar="URL",
        default=None,
        help=(
            "Public URL prefix for --search-dir. "
            "Enables clickable candidate links in the report. "
            "Example: https://example.org/mirror/archives/"
        ),
    )

    # --- Search options ---
    search_group = parser.add_argument_group("Candidate search options")
    search_group.add_argument(
        "--search-dir",
        metavar="PATH",
        default=None,
        help=(
            "Root directory containing all archive folders. "
            "Each immediate subdirectory is treated as a separate archive. "
            "The input archive is automatically excluded from search results."
        ),
    )
    search_group.add_argument(
        "--max-candidates",
        type=int,
        default=5,
        metavar="N",
        help="Maximum replacement candidates to show per broken link (default: 5).",
    )
    search_group.add_argument(
        "--fuzzy",
        action="store_true",
        default=False,
        help="Enable fuzzy filename matching for replacement candidates.",
    )
    search_group.add_argument(
        "--index-db",
        metavar="FILE",
        default=None,
        help=(
            "Path to the persistent SQLite file index database. "
            "Defaults to search_index.db in the project root directory. "
            "The index is built on the first run and updated incrementally on subsequent runs."
        ),
    )
    search_group.add_argument(
        "--rebuild-index",
        action="store_true",
        default=False,
        help=(
            "Force a full rebuild of the file index, ignoring cached directory mtimes. "
            "Use this if files have been added or removed without updating directory timestamps."
        ),
    )

    # --- Output options ---
    output_group = parser.add_argument_group("Output options")
    output_group.add_argument(
        "--output",
        metavar="FILE",
        default="report.html",
        help="HTML report output path (default: report.html).",
    )
    output_group.add_argument(
        "--json-output",
        metavar="FILE",
        default=None,
        help="Also export results as JSON to this path.",
    )
    output_group.add_argument(
        "--csv-output",
        metavar="FILE",
        default=None,
        help="Also export results as CSV to this path.",
    )

    # --- Filtering options ---
    filter_group = parser.add_argument_group("Filtering options")
    filter_group.add_argument(
        "--include-ext",
        metavar="EXTENSIONS",
        default=None,
        help=(
            "Comma-separated list of file extensions to check. "
            "Only links pointing to these extensions will be validated. "
            "Example: .html,.htm,.jpg,.gif,.png,.css,.js"
        ),
    )
    filter_group.add_argument(
        "--ignore-pattern",
        metavar="REGEX",
        action="append",
        default=[],
        dest="ignore_patterns",
        help=(
            "Regex pattern for link hrefs to skip. "
            "Can be specified multiple times. "
            "Example: --ignore-pattern 'cgi-bin' --ignore-pattern 'counter\\.gif'"
        ),
    )

    # --- Case sensitivity ---
    case_group = parser.add_mutually_exclusive_group()
    case_group.add_argument(
        "--case-insensitive",
        action="store_true",
        default=False,
        help="Force case-insensitive filename matching (useful for old Windows-hosted archives).",
    )
    case_group.add_argument(
        "--case-sensitive",
        action="store_true",
        default=False,
        help="Force case-sensitive filename matching (default on Linux).",
    )

    # --- External link checking ---
    parser.add_argument(
        "--external",
        action="store_true",
        default=False,
        help="Also check external HTTP links (uses HTTP HEAD/GET requests).",
    )

    # --- Wayback Machine options ---
    wayback_group = parser.add_argument_group("Wayback Machine options")
    wayback_group.add_argument(
        "--wayback",
        action="store_true",
        default=False,
        help=(
            "Search the Wayback Machine (web.archive.org) for broken links "
            "that have no local candidates. Requires the "
            "'wayback_machine_downloader' Ruby gem to be installed."
        ),
    )
    wayback_group.add_argument(
        "--original-url",
        metavar="URL",
        default=None,
        help=(
            "Root URL of the original website, used to construct Wayback Machine "
            "search URLs. Required when --wayback is set. "
            "Example: http://www.stclares.ac.uk/"
        ),
    )
    wayback_group.add_argument(
        "--wayback-staging",
        metavar="PATH",
        default="wayback_staging",
        help=(
            "Directory where files downloaded from the Wayback Machine are staged "
            "for review before being applied to the archive (default: wayback_staging)."
        ),
    )
    wayback_group.add_argument(
        "--wayback-workers",
        type=int,
        default=3,
        metavar="N",
        help="Number of concurrent Wayback Machine searches (default: 3).",
    )

    # --- Verbosity ---
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress progress output (only errors are shown).",
    )

    return parser


def run(argv=None) -> int:
    """
    Main entry point. Parses arguments and runs the validation pipeline.

    Returns an exit code (0, 1, or 2).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    verbose = not args.quiet

    # --- Validate input directory ---
    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"[ERROR] --input-dir does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not input_dir.is_dir():
        print(f"[ERROR] --input-dir is not a directory: {input_dir}", file=sys.stderr)
        return 2

    # --- Validate search directory (optional) ---
    search_dir = None
    if args.search_dir:
        search_dir = Path(args.search_dir)
        if not search_dir.exists():
            print(f"[ERROR] --search-dir does not exist: {search_dir}", file=sys.stderr)
            return 2
        if not search_dir.is_dir():
            print(f"[ERROR] --search-dir is not a directory: {search_dir}", file=sys.stderr)
            return 2

    # --- Parse include extensions ---
    include_extensions = None
    if args.include_ext:
        include_extensions = set()
        for ext in args.include_ext.split(","):
            ext = ext.strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                include_extensions.add(ext)

    # --- Compile ignore patterns ---
    ignore_patterns = []
    for pattern_str in args.ignore_patterns:
        try:
            ignore_patterns.append(re.compile(pattern_str))
        except re.error as e:
            print(f"[ERROR] Invalid --ignore-pattern '{pattern_str}': {e}", file=sys.stderr)
            return 2

    # --- Determine case sensitivity ---
    # --case-insensitive takes precedence; --case-sensitive is explicit but is
    # also the default on Linux. If neither is set, use False (native behavior).
    case_insensitive = args.case_insensitive

    # --- Validate output path ---
    output_path = Path(args.output)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[ERROR] Cannot create output directory for '{output_path}': {e}", file=sys.stderr)
        return 2

    # --- Validate Wayback options ---
    if args.wayback and not args.original_url:
        print("[ERROR] --wayback requires --original-url to be set.", file=sys.stderr)
        return 2

    wayback_staging = Path(args.wayback_staging) if args.wayback else None

    # --- Print startup info ---
    if verbose:
        print(f"archive-validator {__version__}", file=sys.stderr)
        print(f"Input archive : {input_dir}", file=sys.stderr)
        if args.base_url:
            print(f"Base URL      : {args.base_url}", file=sys.stderr)
        if search_dir:
            print(f"Search dir    : {search_dir}", file=sys.stderr)
        if args.search_base_url:
            print(f"Search URL    : {args.search_base_url}", file=sys.stderr)
        if args.wayback:
            print(f"Wayback URL   : {args.original_url}", file=sys.stderr)
            print(f"Wayback stage : {wayback_staging}", file=sys.stderr)
        print(f"Output        : {output_path}", file=sys.stderr)
        print("", file=sys.stderr)

    # =========================================================================
    # Pipeline Step 1 & 2: Crawl and extract links
    # =========================================================================
    resolver = URLResolver(
        input_dir=input_dir,
        base_url=args.base_url,
        case_insensitive=case_insensitive,
    )

    extractor = LinkExtractor(
        resolver=resolver,
        include_extensions=include_extensions,
        ignore_patterns=ignore_patterns,
    )

    crawler = Crawler(
        input_dir=input_dir,
        resolver=resolver,
        extractor=extractor,
        verbose=verbose,
    )

    html_files, all_links = crawler.crawl()

    if not html_files:
        if verbose:
            print("[WARNING] No HTML files found. Generating empty report.", file=sys.stderr)

    # =========================================================================
    # Pipeline Step 3: Detect broken links
    # =========================================================================
    detector = DeadLinkDetector(
        resolver=resolver,
        check_external=args.external,
        verbose=verbose,
    )

    all_links, broken_links = detector.detect(all_links)

    # =========================================================================
    # Pipeline Step 4: Search for replacement candidates
    # =========================================================================
    if search_dir and broken_links:
        # Resolve the index DB path (default: search_index.db in project root)
        if args.index_db:
            db_path = Path(args.index_db)
        else:
            # Project root = directory containing this package
            project_root = Path(__file__).parent.parent
            db_path = project_root / "search_index.db"

        searcher = ReplacementSearcher(
            search_dir=search_dir,
            input_dir=input_dir,
            db_path=db_path,
            search_base_url=args.search_base_url,
            max_candidates=args.max_candidates,
            case_insensitive=case_insensitive,
            fuzzy=args.fuzzy,
            force_rebuild=args.rebuild_index,
            verbose=verbose,
        )
        broken_links = searcher.search_all(broken_links)
    elif broken_links and not search_dir and verbose:
        print("      (No --search-dir provided; skipping candidate search.)", file=sys.stderr)

    # =========================================================================
    # Pipeline Step 4.5: Wayback Machine search (for links with no local candidates)
    # =========================================================================
    wayback_searcher = None
    if args.wayback and broken_links:
        no_candidates = [b for b in broken_links if not b.candidates and not b.is_external]
        if no_candidates:
            if verbose:
                print(
                    f"[3.5/4] Searching Wayback Machine for "
                    f"{len(no_candidates)} link(s) with no local candidates...",
                    file=sys.stderr,
                )
            wayback_searcher = WaybackSearcher(
                original_url=args.original_url,
                input_dir=input_dir,
                staging_dir=wayback_staging,
                workers=args.wayback_workers,
                verbose=verbose,
            )
            wayback_searcher.search_all(broken_links)
        elif verbose:
            print(
                "      (All broken links have local candidates; skipping Wayback search.)",
                file=sys.stderr,
            )

    # =========================================================================
    # Pipeline Step 5: Generate reports
    # =========================================================================
    if verbose:
        print("[4/4] Generating report(s)...", file=sys.stderr)

    # Count internal links that were actually checked
    internal_checked = sum(
        1 for l in all_links
        if not l.is_external and not l.is_ignored and l.resolved_path is not None
    )
    # Also count external links if checked
    external_checked = sum(1 for l in all_links if l.is_external) if args.external else 0
    total_checked = internal_checked + external_checked

    summary = ReportSummary(
        input_dir=input_dir,
        base_url=args.base_url,
        html_files=html_files,
        total_links=total_checked,
        broken_links=broken_links,
    )

    # HTML report
    try:
        generate_html_report(summary, broken_links, output_path)
        if verbose:
            print(f"      HTML report: {output_path}", file=sys.stderr)
    except OSError as e:
        print(f"[ERROR] Failed to write HTML report: {e}", file=sys.stderr)
        return 2

    # JSON report (optional)
    if args.json_output:
        json_path = Path(args.json_output)
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            generate_json_report(summary, broken_links, json_path)
            if verbose:
                print(f"      JSON report: {json_path}", file=sys.stderr)
        except OSError as e:
            print(f"[ERROR] Failed to write JSON report: {e}", file=sys.stderr)
            return 2

    # CSV report (optional)
    if args.csv_output:
        csv_path = Path(args.csv_output)
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            generate_csv_report(summary, broken_links, csv_path)
            if verbose:
                print(f"      CSV report : {csv_path}", file=sys.stderr)
        except OSError as e:
            print(f"[ERROR] Failed to write CSV report: {e}", file=sys.stderr)
            return 2

    # =========================================================================
    # Final summary
    # =========================================================================
    if verbose:
        print("", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"  HTML files scanned : {summary.html_file_count}", file=sys.stderr)
        print(f"  Links checked      : {summary.total_links}", file=sys.stderr)
        print(f"  Broken links       : {summary.broken_total}", file=sys.stderr)
        if summary.broken_total:
            print(f"    - Images         : {summary.broken_images}", file=sys.stderr)
            print(f"    - Pages          : {summary.broken_pages}", file=sys.stderr)
            print(f"    - CSS/JS         : {summary.broken_css + summary.broken_js}", file=sys.stderr)
            print(f"    - Frames         : {summary.broken_frames}", file=sys.stderr)
            print(f"    - With candidates: {summary.with_candidates}", file=sys.stderr)
        print("=" * 50, file=sys.stderr)

    # Exit code: 1 if broken links found, 0 if clean (DD-012)
    return 1 if broken_links else 0
