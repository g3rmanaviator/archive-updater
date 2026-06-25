"""
reporter.py -- HTML, JSON, and CSV report generation.

Generates a self-contained HTML report (DD-010: all CSS/JS inline),
plus optional JSON and CSV exports.

The HTML report includes:
  - Summary statistics
  - Filterable/sortable table of broken links
  - Replacement candidates with clickable links
  - Grouping by source page, link type, archive folder, missing filename
"""

import csv
import html as html_module
import json
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Optional

from .detector import BrokenLink
from .searcher import Candidate


# ---------------------------------------------------------------------------
# Summary statistics class
# ---------------------------------------------------------------------------

class ReportSummary:
    """Aggregated statistics for the report header."""

    def __init__(
        self,
        input_dir: Path,
        base_url: Optional[str],
        html_files: list,
        total_links: int,
        broken_links: list,
        generated_at: datetime = None,
    ):
        self.input_dir = input_dir
        self.base_url = base_url
        self.html_file_count = len(html_files)
        self.total_links = total_links
        self.broken_links = broken_links
        self.generated_at = generated_at or datetime.now()

        # Counts by type
        self.broken_total = len(broken_links)
        self.broken_images = sum(1 for b in broken_links if b.link_type == "image")
        self.broken_pages = sum(1 for b in broken_links if b.link_type == "page")
        self.broken_css = sum(1 for b in broken_links if b.link_type == "css")
        self.broken_js = sum(1 for b in broken_links if b.link_type == "js")
        self.broken_frames = sum(1 for b in broken_links if b.link_type == "frame")
        self.broken_assets = sum(
            1 for b in broken_links
            if b.link_type not in ("image", "page", "css", "js", "frame")
        )
        self.broken_external = sum(1 for b in broken_links if b.is_external)
        self.with_candidates = sum(1 for b in broken_links if b.candidates)


# ---------------------------------------------------------------------------
# HTML Report Generator
# ---------------------------------------------------------------------------

# Inline CSS for the report (self-contained, DD-010)
REPORT_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 14px;
    color: #333;
    background: #f5f5f5;
    padding: 20px;
}
h1 { font-size: 1.6em; margin-bottom: 4px; color: #1a1a2e; }
h2 { font-size: 1.2em; margin: 24px 0 10px; color: #16213e; border-bottom: 2px solid #e0e0e0; padding-bottom: 6px; }
.meta { color: #666; font-size: 0.85em; margin-bottom: 20px; }
.meta a { color: #0066cc; }
.summary-grid { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
.card {
    background: #fff; border-radius: 8px; padding: 14px 18px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); min-width: 130px; text-align: center;
}
.card .num { font-size: 2em; font-weight: 700; line-height: 1.1; }
.card .label { font-size: 0.78em; color: #666; margin-top: 2px; }
.card.ok .num { color: #2e7d32; }
.card.warn .num { color: #e65100; }
.card.info .num { color: #1565c0; }
.card.neutral .num { color: #555; }
.filters {
    background: #fff; border-radius: 8px; padding: 14px 18px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin-bottom: 20px;
    display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end;
}
.filters label { font-size: 0.82em; color: #555; display: block; margin-bottom: 3px; }
.filters select, .filters input {
    padding: 5px 8px; border: 1px solid #ccc; border-radius: 4px;
    font-size: 0.9em; background: #fafafa;
}
.filters button {
    padding: 6px 14px; background: #1565c0; color: #fff;
    border: none; border-radius: 4px; cursor: pointer; font-size: 0.9em;
}
.filters button:hover { background: #0d47a1; }
.filter-count { font-size: 0.85em; color: #666; align-self: center; }
.table-wrap { overflow-x: auto; }
table {
    width: 100%; border-collapse: collapse; background: #fff;
    border-radius: 8px; overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1);
}
thead th {
    background: #1a1a2e; color: #fff; padding: 10px 12px;
    text-align: left; font-size: 0.82em; font-weight: 600;
    white-space: nowrap; cursor: pointer; user-select: none;
}
thead th:hover { background: #16213e; }
thead th.sorted-asc::after { content: " \\25b2"; }
thead th.sorted-desc::after { content: " \\25bc"; }
tbody tr { border-bottom: 1px solid #eee; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: #f0f4ff; }
tbody tr.hidden { display: none; }
td { padding: 9px 12px; vertical-align: top; font-size: 0.88em; }
td.source { max-width: 220px; word-break: break-all; }
td.broken-link { max-width: 200px; word-break: break-all; font-family: monospace; font-size: 0.85em; }
td.expected-path { max-width: 220px; word-break: break-all; font-family: monospace; font-size: 0.82em; color: #c62828; }
td.candidates { min-width: 200px; }
.badge {
    display: inline-block; padding: 2px 7px; border-radius: 10px;
    font-size: 0.78em; font-weight: 600; white-space: nowrap;
}
.badge-image { background: #e3f2fd; color: #1565c0; }
.badge-page  { background: #e8f5e9; color: #2e7d32; }
.badge-css   { background: #fce4ec; color: #880e4f; }
.badge-js    { background: #fff8e1; color: #e65100; }
.badge-frame { background: #f3e5f5; color: #6a1b9a; }
.badge-asset { background: #f5f5f5; color: #555; }
.badge-external { background: #fff3e0; color: #bf360c; }
.candidate {
    margin-bottom: 5px; padding: 5px 8px;
    background: #f9fbe7; border-left: 3px solid #aed581;
    border-radius: 0 4px 4px 0; font-size: 0.82em;
}
.candidate a { color: #2e7d32; word-break: break-all; }
.candidate .cand-meta { color: #777; font-size: 0.9em; margin-top: 2px; }
.confidence-bar {
    display: inline-block; height: 6px; border-radius: 3px;
    background: #aed581; vertical-align: middle; margin-right: 4px;
}
.no-candidates { color: #999; font-style: italic; font-size: 0.85em; }
.no-results {
    text-align: center; padding: 40px; color: #999;
    background: #fff; border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1);
}
.no-results.success { color: #2e7d32; font-size: 1.1em; }
footer { margin-top: 30px; text-align: center; color: #aaa; font-size: 0.8em; }
"""

# Inline JavaScript for filtering and sorting
REPORT_JS = """\
(function() {
    var rows = [];
    function initRows() {
        rows = Array.from(document.querySelectorAll('tbody tr[data-type]'));
    }
    function applyFilters() {
        var typeFilter = document.getElementById('filter-type').value;
        var archiveFilter = document.getElementById('filter-archive').value;
        var searchFilter = document.getElementById('filter-search').value.toLowerCase();
        var visible = 0;
        rows.forEach(function(row) {
            var type = row.getAttribute('data-type') || '';
            var archive = row.getAttribute('data-archive') || '';
            var text = row.textContent.toLowerCase();
            var show = true;
            if (typeFilter && type !== typeFilter) show = false;
            if (archiveFilter && archive !== archiveFilter) show = false;
            if (searchFilter && text.indexOf(searchFilter) === -1) show = false;
            row.classList.toggle('hidden', !show);
            if (show) visible++;
        });
        var countEl = document.getElementById('filter-count');
        if (countEl) countEl.textContent = 'Showing ' + visible + ' of ' + rows.length + ' broken links';
    }
    function resetFilters() {
        document.getElementById('filter-type').value = '';
        document.getElementById('filter-archive').value = '';
        document.getElementById('filter-search').value = '';
        applyFilters();
    }
    var sortCol = -1;
    var sortAsc = true;
    function sortTable(colIndex) {
        var tbody = document.querySelector('tbody');
        if (!tbody) return;
        var allRows = Array.from(tbody.querySelectorAll('tr[data-type]'));
        if (sortCol === colIndex) { sortAsc = !sortAsc; } else { sortCol = colIndex; sortAsc = true; }
        allRows.sort(function(a, b) {
            var aText = (a.cells[colIndex] ? a.cells[colIndex].textContent : '').trim().toLowerCase();
            var bText = (b.cells[colIndex] ? b.cells[colIndex].textContent : '').trim().toLowerCase();
            return sortAsc ? aText.localeCompare(bText) : bText.localeCompare(aText);
        });
        allRows.forEach(function(r) { tbody.appendChild(r); });
        document.querySelectorAll('thead th').forEach(function(th, i) {
            th.classList.remove('sorted-asc', 'sorted-desc');
            if (i === colIndex) th.classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
        });
        applyFilters();
    }
    document.addEventListener('DOMContentLoaded', function() {
        initRows();
        applyFilters();
        var typeEl = document.getElementById('filter-type');
        var archiveEl = document.getElementById('filter-archive');
        var searchEl = document.getElementById('filter-search');
        var resetEl = document.getElementById('filter-reset');
        if (typeEl) typeEl.addEventListener('change', applyFilters);
        if (archiveEl) archiveEl.addEventListener('change', applyFilters);
        if (searchEl) searchEl.addEventListener('input', applyFilters);
        if (resetEl) resetEl.addEventListener('click', resetFilters);
        document.querySelectorAll('thead th[data-col]').forEach(function(th) {
            th.addEventListener('click', function() {
                sortTable(parseInt(th.getAttribute('data-col')));
            });
        });
    });
})();
"""


def _e(text) -> str:
    """HTML-escape a value using Python's html.escape (safe against entity mangling)."""
    return html_module.escape(str(text), quote=True)


def _badge(link_type: str, is_external: bool = False) -> str:
    """Generate an HTML badge for a link type."""
    if is_external:
        return '<span class="badge badge-external">external</span>'
    valid = ("image", "page", "css", "js", "frame")
    cls = "badge-" + link_type if link_type in valid else "badge-asset"
    return '<span class="badge ' + cls + '">' + _e(link_type) + '</span>'


def _confidence_bar(confidence: int) -> str:
    """Generate a small inline confidence bar."""
    width = max(4, confidence)
    return '<span class="confidence-bar" style="width:' + str(width) + 'px" title="' + str(confidence) + '%"></span>'


def _card(num, label, cls="neutral") -> str:
    """Generate a summary card HTML block."""
    color_cls = cls if cls in ("ok", "warn", "info") else "neutral"
    return (
        '<div class="card ' + color_cls + '">'
        '<div class="num">' + str(num) + '</div>'
        '<div class="label">' + _e(label) + '</div>'
        '</div>'
    )


def generate_html_report(
    summary: ReportSummary,
    broken_links: list,
    output_path: Path,
) -> None:
    """
    Generate a self-contained HTML report and write it to output_path.

    Parameters
    ----------
    summary : ReportSummary
    broken_links : list of BrokenLink
    output_path : Path
    """

    # Collect unique values for filter dropdowns
    all_types = sorted(set(b.link_type for b in broken_links))
    all_archives = sorted(set(
        c.archive_folder
        for b in broken_links
        for c in b.candidates
    ))

    # Build the broken links table rows
    rows_html = []
    for b in broken_links:
        # Source cell
        if b.source_url:
            source_cell = '<a href="' + _e(b.source_url) + '" target="_blank">' + _e(b.source_url) + '</a>'
        else:
            source_cell = _e(str(b.source_file))

        # Broken link cell
        broken_cell = _e(b.raw_href)

        # Expected path cell — hyperlink it when base_url is available
        if b.expected_path and summary.base_url and summary.input_dir:
            try:
                rel = b.expected_path.resolve().relative_to(summary.input_dir.resolve())
                rel_str = "/".join(rel.parts)
                path_url = summary.base_url.rstrip("/") + "/" + rel_str
                path_cell = (
                    '<a href="' + _e(path_url) + '" target="_blank">'
                    + _e(b.expected_path_display)
                    + '</a>'
                )
            except ValueError:
                path_cell = _e(b.expected_path_display)
        else:
            path_cell = _e(b.expected_path_display)

        # Type badge
        type_cell = _badge(b.link_type, b.is_external)
        if b.is_external and b.http_status:
            type_cell += ' <span style="color:#999;font-size:0.85em">HTTP ' + str(b.http_status) + '</span>'

        # Candidates cell
        if b.candidates:
            cand_parts = []
            for c in b.candidates:
                bar = _confidence_bar(c.confidence)
                if c.public_url:
                    link_html = '<a href="' + _e(c.public_url) + '" target="_blank">' + _e(c.public_url) + '</a>'
                else:
                    link_html = _e(str(c.local_path))
                cand_parts.append(
                    '<div class="candidate">'
                    + bar + link_html
                    + '<div class="cand-meta">'
                    + _e(c.archive_folder) + ' &bull; ' + _e(c.match_type) + ' &bull; ' + str(c.confidence) + '%'
                    + '</div></div>'
                )
            cand_cell = "\n".join(cand_parts)
        else:
            cand_cell = '<span class="no-candidates">No candidates found</span>'

        # Data attributes for filtering
        archive_attr = b.candidates[0].archive_folder if b.candidates else ""

        rows_html.append(
            '<tr data-type="' + _e(b.link_type) + '" data-archive="' + _e(archive_attr) + '">'
            + '<td class="source">' + source_cell + '</td>'
            + '<td class="broken-link">' + broken_cell + '</td>'
            + '<td class="expected-path">' + path_cell + '</td>'
            + '<td>' + type_cell + '</td>'
            + '<td class="candidates">' + cand_cell + '</td>'
            + '</tr>'
        )

    table_body = "\n".join(rows_html)

    # Build filter dropdowns
    type_options = '<option value="">All types</option>' + "".join(
        '<option value="' + _e(t) + '">' + _e(t) + '</option>' for t in all_types
    )
    archive_options = '<option value="">All archives</option>' + "".join(
        '<option value="' + _e(a) + '">' + _e(a) + '</option>' for a in all_archives
    )

    # Summary cards
    ok_cls = "ok" if summary.broken_total == 0 else "warn"
    cards_html = "".join([
        _card(summary.html_file_count, "HTML Files Scanned", "info"),
        _card(summary.total_links, "Links Checked", "info"),
        _card(summary.broken_total, "Broken Links", ok_cls),
        _card(summary.broken_images, "Broken Images", "warn" if summary.broken_images else "ok"),
        _card(summary.broken_pages, "Broken Pages", "warn" if summary.broken_pages else "ok"),
        _card(summary.broken_css + summary.broken_js, "Broken CSS/JS",
              "warn" if (summary.broken_css + summary.broken_js) else "ok"),
        _card(summary.broken_frames, "Broken Frames", "warn" if summary.broken_frames else "ok"),
        _card(summary.with_candidates, "With Candidates", "info"),
    ])

    # No-results message or full table
    if not broken_links:
        main_content = '<div class="no-results success">&#10003; No broken links found!</div>'
    else:
        filters_section = (
            '<div class="filters">'
            '<div><label for="filter-type">Link Type</label>'
            '<select id="filter-type">' + type_options + '</select></div>'
            '<div><label for="filter-archive">Candidate Archive</label>'
            '<select id="filter-archive">' + archive_options + '</select></div>'
            '<div><label for="filter-search">Search</label>'
            '<input type="text" id="filter-search" placeholder="Filter by any text..." style="width:220px"></div>'
            '<button id="filter-reset">Reset</button>'
            '<span class="filter-count" id="filter-count"></span>'
            '</div>'
        )

        table_section = (
            '<div class="table-wrap">'
            '<table>'
            '<thead><tr>'
            '<th data-col="0">Source Page</th>'
            '<th data-col="1">Broken Link</th>'
            '<th data-col="2">Expected Path</th>'
            '<th data-col="3">Type</th>'
            '<th data-col="4">Replacement Candidates</th>'
            '</tr></thead>'
            '<tbody>' + table_body + '</tbody>'
            '</table>'
            '</div>'
        )

        main_content = filters_section + "\n" + table_section

    # Archive info line
    archive_name = summary.input_dir.name
    if summary.base_url:
        archive_link = '<a href="' + _e(summary.base_url) + '" target="_blank">' + _e(summary.base_url) + '</a>'
    else:
        archive_link = _e(str(summary.input_dir))

    generated_str = summary.generated_at.strftime("%Y-%m-%d %H:%M:%S")

    # Assemble the full HTML document
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "<title>Archive Validator Report &#8212; " + _e(archive_name) + "</title>",
        "<style>",
        REPORT_CSS,
        "</style>",
        "</head>",
        "<body>",
        "<h1>Archive Validator Report</h1>",
        '<p class="meta">Archive: ' + archive_link + "<br>Generated: " + _e(generated_str) + "</p>",
        "<h2>Summary</h2>",
        '<div class="summary-grid">',
        cards_html,
        "</div>",
        "<h2>Broken Links</h2>",
        main_content,
        "<footer>Generated by archive-validator &bull; " + _e(generated_str) + "</footer>",
        "<script>",
        REPORT_JS,
        "</script>",
        "</body>",
        "</html>",
    ]

    output_path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# JSON Export
# ---------------------------------------------------------------------------

def generate_json_report(
    summary: ReportSummary,
    broken_links: list,
    output_path: Path,
) -> None:
    """
    Export results as a machine-readable JSON file.
    """
    data = {
        "generated_at": summary.generated_at.isoformat(),
        "input_dir": str(summary.input_dir),
        "base_url": summary.base_url,
        "summary": {
            "html_files_scanned": summary.html_file_count,
            "total_links_checked": summary.total_links,
            "broken_total": summary.broken_total,
            "broken_images": summary.broken_images,
            "broken_pages": summary.broken_pages,
            "broken_css": summary.broken_css,
            "broken_js": summary.broken_js,
            "broken_frames": summary.broken_frames,
            "broken_assets": summary.broken_assets,
            "broken_external": summary.broken_external,
            "with_candidates": summary.with_candidates,
        },
        "broken_links": [
            {
                "source_file": str(b.source_file),
                "source_url": b.source_url,
                "raw_href": b.raw_href,
                "resolved_url": b.resolved_url,
                "expected_path": str(b.expected_path) if b.expected_path else None,
                "link_type": b.link_type,
                "tag_name": b.tag_name,
                "attr_name": b.attr_name,
                "is_external": b.is_external,
                "http_status": b.http_status,
                "candidates": [
                    {
                        "local_path": str(c.local_path),
                        "public_url": c.public_url,
                        "archive_folder": c.archive_folder,
                        "match_type": c.match_type,
                        "confidence": c.confidence,
                    }
                    for c in b.candidates
                ],
            }
            for b in broken_links
        ],
    }

    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

def generate_csv_report(
    summary: ReportSummary,
    broken_links: list,
    output_path: Path,
) -> None:
    """
    Export results as a CSV file (one row per broken link x candidate).

    If a broken link has no candidates, it still appears as one row
    with empty candidate columns.
    """
    fieldnames = [
        "source_file",
        "source_url",
        "raw_href",
        "resolved_url",
        "expected_path",
        "link_type",
        "tag_name",
        "is_external",
        "http_status",
        "candidate_path",
        "candidate_url",
        "candidate_archive",
        "candidate_match_type",
        "candidate_confidence",
    ]

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()

    for b in broken_links:
        base_row = {
            "source_file": str(b.source_file),
            "source_url": b.source_url or "",
            "raw_href": b.raw_href,
            "resolved_url": b.resolved_url or "",
            "expected_path": str(b.expected_path) if b.expected_path else "",
            "link_type": b.link_type,
            "tag_name": b.tag_name,
            "is_external": str(b.is_external),
            "http_status": str(b.http_status) if b.http_status else "",
        }

        if b.candidates:
            for c in b.candidates:
                row = dict(base_row)
                row.update({
                    "candidate_path": str(c.local_path),
                    "candidate_url": c.public_url or "",
                    "candidate_archive": c.archive_folder,
                    "candidate_match_type": c.match_type,
                    "candidate_confidence": str(c.confidence),
                })
                writer.writerow(row)
        else:
            row = dict(base_row)
            row.update({
                "candidate_path": "",
                "candidate_url": "",
                "candidate_archive": "",
                "candidate_match_type": "",
                "candidate_confidence": "",
            })
            writer.writerow(row)

    output_path.write_text(output.getvalue(), encoding="utf-8")
