# archive-validator

A Python 3 command-line tool for validating archived website snapshots.

Crawls a single archive folder, detects broken internal links, searches other archive folders for possible replacement files, and generates a self-contained HTML report (plus optional JSON and CSV exports).

---

## Features

- Recursively scans HTML files for all link types: `<a href>`, `<img src>`, `<script src>`, `<link href>`, `<frame src>`, `<iframe src>`, `background` attributes
- Detects broken internal links by mapping URLs to local filesystem paths
- Searches other archive folders for replacement candidates using multiple matching strategies
- Generates a self-contained, filterable, sortable HTML report (no external dependencies)
- Optional JSON and CSV exports
- Optional external HTTP link checking
- Handles URL-encoded filenames, case sensitivity, directory index resolution, and query string stripping
- Read-only: never modifies any archive files

---

## Requirements

- Python 3.10 or later
- Ubuntu / Linux (also works on macOS and Windows)

---

## Installation

```bash
# Clone or download the repository
cd archive-updater

# Install dependencies
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---|---|
| `beautifulsoup4` | HTML parsing |
| `lxml` | Fast HTML parser backend (optional but recommended) |
| `requests` | External link checking (only needed with `--external`) |

All other functionality uses Python's standard library.

---

## Usage

```
python archive_validator.py --input-dir PATH [options]
```

### Required argument

| Argument | Description |
|---|---|
| `--input-dir PATH` | Archive snapshot directory to validate |

### URL options

| Argument | Description |
|---|---|
| `--base-url URL` | Public URL for the input archive. Enables clickable source links in the report, recognition of absolute URLs as internal, and hyperlinked expected paths in the broken links table. Example: `https://example.org/mirror/archives/archive-1998-06-01/` |
| `--search-base-url URL` | Public URL prefix for `--search-dir`. Enables clickable candidate links. Example: `https://example.org/mirror/archives/` |

### Candidate search options

| Argument | Default | Description |
|---|---|---|
| `--search-dir PATH` | — | Root directory containing all archive folders. Each immediate subdirectory is treated as a separate archive. The input archive is excluded automatically. |
| `--max-candidates N` | 5 | Maximum replacement candidates per broken link |
| `--fuzzy` | off | Enable fuzzy filename matching for candidates |
| `--index-db FILE` | `search_index.db` | Path to the persistent SQLite file index. Built on the first run, updated incrementally on subsequent runs. Defaults to `search_index.db` in the project root (not in the web root). |
| `--rebuild-index` | off | Force a full rebuild of the file index, ignoring cached directory timestamps. Use this if files were added or removed without updating directory mtimes. |

### Output options

| Argument | Default | Description |
|---|---|---|
| `--output FILE` | `report.html` | HTML report output path |
| `--json-output FILE` | — | Also export results as JSON |
| `--csv-output FILE` | — | Also export results as CSV |

### Filtering options

| Argument | Description |
|---|---|
| `--include-ext EXTENSIONS` | Comma-separated extensions to check. Example: `.html,.htm,.jpg,.gif,.png,.css,.js` |
| `--ignore-pattern REGEX` | Regex pattern for hrefs to skip. Can be specified multiple times. |

### Case sensitivity

| Argument | Description |
|---|---|
| `--case-insensitive` | Force case-insensitive filename matching |
| `--case-sensitive` | Force case-sensitive matching (Linux default) |

### Wayback Machine options

| Argument | Default | Description |
|---|---|---|
| `--wayback` | off | Search the Wayback Machine for broken links with no local candidates. Requires the `wayback_machine_downloader` Ruby gem (`gem install wayback_machine_downloader`). |
| `--original-url URL` | — | Root URL of the original website. Required with `--wayback`. Example: `http://www.stclares.ac.uk/` |
| `--wayback-staging PATH` | `wayback_staging` | Directory where Wayback downloads are staged for review before being applied to the archive |
| `--wayback-workers N` | 3 | Concurrent Wayback searches (keep low to avoid rate limiting) |

When `--wayback` is enabled, any broken link with no local candidates is searched on the Internet Archive. Matching files are downloaded to `--wayback-staging` and appear in the report as **🌐 Wayback** candidates with an **✓ Apply** button, identical to local candidates.

### Other options

| Argument | Description |
|---|---|
| `--external` | Also check external HTTP links via HEAD/GET |
| `--quiet` | Suppress progress output |
| `--version` | Show version and exit |

---

## Example Commands

### 1. Basic local scan (no public URLs)

```bash
python archive_validator.py \
  --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01
```

Scans the archive, detects broken links, writes `report.html` in the current directory.

---

### 2. Full scan with public URLs and candidate search

```bash
python archive_validator.py \
  --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \
  --base-url https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/ \
  --search-dir /htdocs/alumni/mirror/archives \
  --search-base-url https://sugarhouse.stclaresalumni.org/mirror/archives/ \
  --output /tmp/report-1998-06-01.html \
  --json-output /tmp/report-1998-06-01.json \
  --csv-output /tmp/report-1998-06-01.csv
```

- Source page links in the report are clickable (open the live site)
- Replacement candidate links are clickable
- Results exported in three formats

---

### 3. Check external links with fuzzy matching

```bash
python archive_validator.py \
  --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \
  --search-dir /htdocs/alumni/mirror/archives \
  --external \
  --fuzzy \
  --max-candidates 10
```

---

### 4. Only check HTML and image files, case-insensitive

```bash
python archive_validator.py \
  --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \
  --include-ext .html,.htm,.jpg,.gif,.png \
  --case-insensitive
```

Useful for archives originally hosted on Windows servers where filenames may have inconsistent casing.

---

### 5. Ignore known-missing paths

```bash
python archive_validator.py \
  --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \
  --ignore-pattern "cgi-bin" \
  --ignore-pattern "counter\.gif" \
  --ignore-pattern "tracker\."
```

---

### 6. Quiet mode (for scripting / CI)

```bash
python archive_validator.py \
  --input-dir /htdocs/alumni/mirror/archives/archive-1998-06-01 \
  --quiet \
  --output report.html

echo "Exit code: $?"
# Exit code 0 = no broken links
# Exit code 1 = broken links found
# Exit code 2 = tool error
```

---

## URL-to-Filesystem Mapping

This is the core logic of the tool. Here is how links are resolved:

### Setup

```
--input-dir  /htdocs/alumni/mirror/archives/archive-1998-06-01
--base-url   https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/
```

### Case 1: Relative link

```html
<!-- In: /htdocs/.../archive-1998-06-01/about/index.html -->
<img src="images/logo.gif">
```

Resolved to: `/htdocs/.../archive-1998-06-01/about/images/logo.gif`

### Case 2: Root-relative link

```html
<a href="/mirror/archives/archive-1998-06-01/news/story.html">
```

Strip base path `/mirror/archives/archive-1998-06-01/` → `news/story.html`

Resolved to: `/htdocs/.../archive-1998-06-01/news/story.html`

### Case 3: Absolute URL (same host)

```html
<a href="https://sugarhouse.stclaresalumni.org/mirror/archives/archive-1998-06-01/page.html">
```

Matches `--base-url` → treated as internal → resolved to local path.

### Query strings and fragments

- `page.html?id=5` → checks `page.html` (query string stripped)
- `page.html#section` → checks `page.html` (fragment stripped, shown as-written in report)

### Directory links

- `href="section/"` → checks for `section/index.html`, `section/index.htm`, `section/default.html`, `section/default.htm` in that order

---

## Replacement Candidate Matching

When a broken link is found, the tool searches all other archive folders under `--search-dir` using these strategies (in priority order):

| Strategy | Base Confidence | Example |
|---|---|---|
| Exact relative path in another archive | 95% | `images/logo.gif` found at same path in `archive-2001-03-15` |
| Extension case difference | 80% | `logo.JPG` vs `logo.jpg` |
| Exact filename match (different location) | 70% | `logo.gif` found anywhere in another archive |
| URL-decoded filename match | 65% | `my%20file.jpg` matches `my file.jpg` |
| Case-insensitive filename match | 60% | `Logo.GIF` matches `logo.gif` |
| Fuzzy match (with `--fuzzy`) | 20–50% | `logoo.gif` is similar to `logo.gif` |

### Date proximity bonus

When archive folder names contain a date (e.g. `archive-2001-06-01`), a small bonus is added to break ties between equally-matched candidates:

| Proximity | Bonus |
|---|---|
| Same archive, different subfolder | +5 |
| Within 3 months of the target archive | +4 |
| Within 1 year | +3 |
| Within 2 years | +2 |
| More than 2 years away | +0 |

This means a file found in `archive-2001-03-15` will rank above the same file found in `archive-1998-09-01` when validating `archive-2001-06-01`.

### Versions found

The report shows how many total copies of a filename exist across all indexed archives (e.g. **6 versions found**). This helps you quickly assess whether a file is common across snapshots or unique to one period.

### Performance: persistent SQLite index

The file index is stored in `search_index.db` (project root) and updated incrementally:

- **First run**: walks all archive folders and populates the database (~30s for 1M files)
- **Subsequent runs**: checks directory mtimes — unchanged folders are skipped entirely (~0.1s)
- **Changed folders**: only re-scanned folders are re-indexed

Use `--rebuild-index` to force a full rescan if files were added without updating directory timestamps.

---

## Report Features

The HTML report is a single self-contained file with:

- **Summary cards**: files scanned, links checked, broken by type, candidates found
- **Filterable table**: filter by link type, candidate archive, or free-text search
- **Sortable columns**: click any column header to sort
- **Clickable links**: source pages and candidates link to the live site (when `--base-url` / `--search-base-url` provided)
- **Confidence bars**: visual indicator of candidate match quality

---

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success — no broken links found |
| 1 | Broken links were found |
| 2 | Tool error (bad arguments, unreadable directory, etc.) |

---

## Project Structure

```
archive-updater/
├── archive_validator/
│   ├── __init__.py       # Package metadata
│   ├── cli.py            # Argument parsing and pipeline orchestration
│   ├── crawler.py        # Recursive HTML file scanner
│   ├── extractor.py      # Link extraction from HTML (all tag types)
│   ├── resolver.py       # URL <-> filesystem path mapping logic
│   ├── detector.py       # Dead link detection (file existence checks)
│   ├── searcher.py       # Replacement candidate search engine
│   ├── fileindex.py      # Persistent SQLite file index (incremental updates)
│   ├── reporter.py       # HTML/JSON/CSV report generation
│   └── wayback.py        # Wayback Machine candidate search (via Ruby gem)
├── templates/
│   └── index.html        # Web UI form template
├── static/
│   └── ui.css            # Web UI stylesheet
├── archive_validator.py  # Top-level entry point (CLI)
├── web_ui.py             # Flask web front-end (includes /apply endpoint)
├── search_index.db       # SQLite file index (auto-created on first run)
├── apply.log             # Append-only log of every file copy operation (auto-created)
├── wayback_staging/      # Downloaded Wayback files awaiting review (auto-created)
├── requirements.txt
├── README.md
└── DESIGN.md             # Architecture and design decisions
```

---

## Web UI

A browser-based front-end is available as an alternative to the CLI. It mirrors the look and feel of the report page and streams live progress output as the job runs.

### Setup

```bash
pip install flask
```

### Start the server

```bash
python web_ui.py
```

Then open **http://127.0.0.1:5000** in your browser.

### Features

- Form fields for every CLI option, grouped and labelled the same way as this README
- Live progress log — streams the validator's output in real time as it runs
- Result banner on completion: ✓ clean / ⚠ broken links found / ✕ error
- **View Report** button that opens the generated HTML report directly in the browser
- Dynamic ignore-pattern list (add/remove rows)
- **✓ Apply** button on each candidate — click to review the copy operation (From/To paths), then confirm to copy the file into the archive. Every copy is logged to `apply.log`.
- **🌐 Wayback** candidates appear identically to local candidates and support the same Apply workflow

### Security note

The web UI runs commands on the server filesystem. It binds to `127.0.0.1:5000` by default and should only be accessed over a trusted network or SSH tunnel. Do not expose it to the public internet without adding authentication.

---

## Safety

The tool is **read-only**. It never modifies, moves, or deletes any files in the input or search directories. It only writes the report file(s) to the paths specified by `--output`, `--json-output`, and `--csv-output`.
