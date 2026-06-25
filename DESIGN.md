# Archive Validator — Design Document

## Project Summary

A Python 3 command-line tool that crawls a single archived website snapshot directory, detects broken internal links, searches other archive folders for replacement candidates, and generates a self-contained HTML report (plus optional JSON/CSV exports).

---

## Design Decisions

### DD-001: `--base-url` is Optional

**Decision:** `--base-url` is an optional argument.

**Rationale:** The crawl and dead-link detection are entirely filesystem-based. The base URL is only needed for two purposes:
1. Making source page links in the HTML report clickable (pointing to the live site).
2. Recognizing absolute URLs in HTML files (e.g. `href="https://sugarhouse.stclaresalumni.org/..."`) as internal links rather than external ones.

If omitted, the tool operates in fully local mode: all link resolution uses the filesystem, report links show local paths only, and absolute URLs are treated as external (and ignored unless `--external` is set).

---

### DD-002: `--search-base-url` is Optional

**Decision:** A separate `--search-base-url` argument (e.g. `https://sugarhouse.stclaresalumni.org/mirror/archives/`) is used to construct clickable public URLs for replacement candidates found in other archive folders.

**Rationale:** The search root (`--search-dir`) is a local filesystem path. To make candidate links in the report clickable, the tool needs to know the public URL prefix for that root. If omitted, candidate entries in the report show filesystem paths only (not hyperlinked).

**URL construction:** `candidate_public_url = search_base_url + relative_path_from_search_dir`

---

### DD-003: Archive Folder Detection

**Decision:** Any immediate subdirectory under `--search-dir` is treated as a separate archive folder. No naming convention is enforced.

**Rationale:** While the primary example uses `archive-YYYY-MM-DD`, folder names may vary. The tool identifies "other archives" simply as any sibling directory under the search root, excluding the input archive itself.

---

### DD-004: Directory Index Resolution

**Decision:** A link pointing to a directory (e.g. `href="section/"` or `href="section"`) is resolved by checking for the following index files in order:
1. `index.html`
2. `index.htm`
3. `default.html`
4. `default.htm`

If any of these exist, the link is considered resolved (not broken). If none exist, it is recorded as a broken link with type `page`.

**Rationale:** This matches the default behavior of Apache/Nginx web servers used in the era of these archives.

---

### DD-005: Internal vs. External Link Classification

**Decision:** A link is classified as **internal** if:
- It is a relative URL (no scheme), OR
- It is an absolute URL whose hostname+path prefix matches `--base-url` (when provided)

All other absolute URLs are **external** and are ignored by default (checked only when `--external` is set).

Fragment-only links (`#anchor`) and `mailto:`, `tel:`, `javascript:` links are always ignored.

---

### DD-006: Query String Handling

**Decision:** Query strings are stripped before resolving a URL to a filesystem path. The file is looked up without the query string component.

**Rationale:** Static archived files do not have server-side query string processing. A link like `page.html?id=5` maps to `page.html` on disk.

---

### DD-007: URL Fragment Handling

**Decision:** URL fragments (`#section`) are stripped before filesystem resolution. The fragment is preserved in the report display (shown as written in the source HTML) but does not affect file existence checking.

---

### DD-008: Replacement Candidate Matching Strategies

Candidates are found by searching recursively under `--search-dir` (excluding the input archive). Matching is attempted in this priority order, with associated confidence scores:

| # | Strategy | Confidence |
|---|---|---|
| 1 | Exact relative path match in another archive | 95% |
| 2 | Exact filename + same extension, different location | 70% |
| 3 | Case-insensitive filename match | 60% |
| 4 | URL-decoded filename match (e.g. `my%20file.jpg` → `my file.jpg`) | 65% |
| 5 | Extension case difference only (`.JPG` vs `.jpg`) | 80% |
| 6 | Fuzzy filename match (difflib SequenceMatcher ≥ 0.6 ratio) | 20–50% |

Multiple strategies may match the same file; the highest-confidence match type is reported.

---

### DD-009: Case Sensitivity

**Decision:** By default, filename matching for dead-link detection uses the **filesystem's native case sensitivity**. On Linux (where archives are hosted), this means case-sensitive matching. The `--case-insensitive` flag forces case-insensitive matching for both dead-link detection and candidate search.

---

### DD-010: HTML Report is Self-Contained

**Decision:** The generated HTML report embeds all CSS and JavaScript inline (no external dependencies). This makes the report a single portable `.html` file that can be shared, emailed, or archived without losing styling or interactivity.

**Implementation:** Jinja2 template with inline `<style>` and `<script>` blocks.

---

### DD-011: Progress Output

**Decision:** The tool prints progress to stderr during execution:
- Files discovered count
- Per-file processing status (current file / total files)
- Phase indicators: Scanning → Checking links → Searching candidates → Generating report

This keeps stdout clean for piping while giving the user visibility into long-running scans.

---

### DD-012: Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success, no broken links found |
| 1 | Broken links were found |
| 2 | Tool error (bad arguments, unreadable directory, etc.) |

---

### DD-013: Safety Guarantee

The tool is **read-only**. It never modifies, moves, or deletes any files in the input or search directories. It only writes the report file(s) to the path(s) specified by `--output`, `--json-output`, and `--csv-output`.

---

## File Structure

```
archive-updater/
├── archive_validator/
│   ├── __init__.py
│   ├── cli.py              # argparse CLI entry point, orchestration
│   ├── crawler.py          # Recursive HTML file scanner
│   ├── extractor.py        # Link extraction from HTML (all tag types)
│   ├── resolver.py         # URL ↔ filesystem path mapping logic
│   ├── detector.py         # Dead link detection (file existence checks)
│   ├── searcher.py         # Replacement candidate search engine
│   └── reporter.py         # HTML/JSON/CSV report generation
├── archive_validator.py    # Top-level entry point (python archive_validator.py ...)
├── requirements.txt
├── README.md
└── DESIGN.md               # This document
```

---

## CLI Arguments Reference

| Argument | Required | Default | Description |
|---|---|---|---|
| `--input-dir` | ✅ | — | Archive snapshot directory to validate |
| `--base-url` | ❌ | None | Public URL of the input archive (enables clickable links) |
| `--search-dir` | ❌ | None | Root directory to search for replacement candidates |
| `--search-base-url` | ❌ | None | Public URL prefix for search-dir (enables clickable candidates) |
| `--output` | ❌ | `report.html` | HTML report output path |
| `--json-output` | ❌ | None | JSON report output path |
| `--csv-output` | ❌ | None | CSV report output path |
| `--external` | ❌ | False | Also check external HTTP links |
| `--max-candidates` | ❌ | 5 | Max replacement candidates per broken link |
| `--include-ext` | ❌ | All | Comma-separated extensions to check |
| `--ignore-pattern` | ❌ | None | Regex pattern(s) for paths to skip |
| `--case-insensitive` | ❌ | False | Force case-insensitive filename matching |
| `--case-sensitive` | ❌ | False | Force case-sensitive matching (overrides default) |
| `--fuzzy` | ❌ | False | Enable fuzzy filename matching for candidates |
