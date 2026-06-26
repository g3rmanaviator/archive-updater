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
# Deduplication helper (defined early — used by ReportSummary and reporter)
# ---------------------------------------------------------------------------

def _dedup_broken_links(broken_links: list) -> list:
    """
    Deduplicate broken links by their resolved expected_path + link_type.

    Multiple source pages referencing the same missing file are collapsed
    into a single entry. Returns a list of
    (representative_BrokenLink, sources_list) tuples, where sources_list
    is a list of (source_url, source_file, raw_href) for every reference
    that pointed to this target.

    External links (no expected_path) are keyed by raw_href instead.
    """
    from collections import OrderedDict

    groups: OrderedDict = OrderedDict()

    for b in broken_links:
        if b.is_external:
            key = ("__external__:" + b.raw_href, b.link_type)
        else:
            key = (str(b.expected_path) if b.expected_path else b.raw_href, b.link_type)

        if key not in groups:
            groups[key] = {
                "representative": b,
                "sources": [],
            }
        groups[key]["sources"].append((b.source_url, b.source_file, b.raw_href))

    return [(g["representative"], g["sources"]) for g in groups.values()]


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
        generated_at: Optional[datetime] = None,
    ):
        self.input_dir = input_dir
        self.base_url = base_url
        self.html_file_count = len(html_files)
        self.total_links = total_links
        self.broken_links = broken_links
        self.generated_at = generated_at or datetime.now()

        # Counts by type (raw — includes duplicate references to the same missing file)
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

        # Deduplicated counts (unique missing files/targets)
        deduped = _dedup_broken_links(broken_links)
        self.unique_broken_total = len(deduped)
        self.unique_with_candidates = sum(1 for b, _ in deduped if b.candidates)


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
/* Apply button */
.btn-apply {
    display: inline-block; margin-left: 8px; padding: 2px 8px;
    background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7;
    border-radius: 4px; cursor: pointer; font-size: 0.8em; font-weight: 600;
    vertical-align: middle; white-space: nowrap;
}
.btn-apply:hover { background: #c8e6c9; }
.btn-apply:disabled { background: #f5f5f5; color: #aaa; border-color: #ddd; cursor: not-allowed; }
.btn-apply.applied { background: #1565c0; color: #fff; border-color: #1565c0; cursor: default; }
/* Apply confirmation panel */
.apply-confirm {
    margin-top: 6px; padding: 8px 10px;
    background: #fff8e1; border: 1px solid #ffcc80; border-radius: 4px;
    font-size: 0.82em;
}
.apply-confirm .apply-paths { font-family: monospace; font-size: 0.9em; color: #333; margin: 4px 0 8px; }
.apply-confirm .apply-actions { display: flex; gap: 8px; }
.btn-confirm-apply {
    padding: 4px 12px; background: #1565c0; color: #fff;
    border: none; border-radius: 4px; cursor: pointer; font-size: 0.85em; font-weight: 600;
}
.btn-confirm-apply:hover { background: #0d47a1; }
.btn-cancel-apply {
    padding: 4px 10px; background: #fff; color: #555;
    border: 1px solid #ccc; border-radius: 4px; cursor: pointer; font-size: 0.85em;
}
.apply-result { margin-top: 5px; font-size: 0.82em; font-weight: 600; }
.apply-result.ok { color: #2e7d32; }
.apply-result.err { color: #c62828; }
/* Wayback candidate */
.wayback-candidate { background: #e8eaf6; border-left-color: #7986cb; }
.wayback-candidate a { color: #283593; }
.wayback-badge {
    display: inline-block; padding: 1px 6px; border-radius: 10px;
    background: #3949ab; color: #fff; font-size: 0.75em; font-weight: 700;
    margin-right: 4px; vertical-align: middle;
}
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

    // ── Apply candidate ────────────────────────────────────────────────────
    window.applyCandidate = function(btn) {
        var src = btn.getAttribute('data-src');
        var dst = btn.getAttribute('data-dst');
        var candidateDiv = btn.closest('.candidate');

        // Remove any existing confirmation panel
        var existing = candidateDiv.querySelector('.apply-confirm');
        if (existing) { existing.remove(); return; }

        // Build confirmation panel
        var panel = document.createElement('div');
        panel.className = 'apply-confirm';
        panel.innerHTML =
            '<strong>Copy file to archive?</strong>' +
            '<div class="apply-paths">' +
            'From: ' + escHtml(src) + '<br>' +
            'To:&nbsp;&nbsp; ' + escHtml(dst) +
            '</div>' +
            '<div class="apply-actions">' +
            '<button class="btn-cancel-apply" onclick="this.closest(\'.apply-confirm\').remove()">Cancel</button>' +
            '<button class="btn-confirm-apply">&#10003; Confirm Copy</button>' +
            '</div>' +
            '<div class="apply-result"></div>';

        candidateDiv.appendChild(panel);

        panel.querySelector('.btn-confirm-apply').addEventListener('click', function() {
            var confirmBtn = this;
            confirmBtn.disabled = true;
            confirmBtn.textContent = 'Copying\u2026';

            fetch('/apply', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({src: src, dst: dst})
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var resultEl = panel.querySelector('.apply-result');
                if (data.ok) {
                    resultEl.className = 'apply-result ok';
                    resultEl.textContent = '\u2713 ' + data.message;
                    btn.className = 'btn-apply applied';
                    btn.disabled = true;
                    btn.textContent = '\u2713 Applied';
                    // Remove cancel button
                    var cancelBtn = panel.querySelector('.btn-cancel-apply');
                    if (cancelBtn) cancelBtn.remove();
                    confirmBtn.remove();
                } else {
                    resultEl.className = 'apply-result err';
                    resultEl.textContent = '\u2717 ' + data.message;
                    confirmBtn.disabled = false;
                    confirmBtn.textContent = '\u2713 Confirm Copy';
                }
            })
            .catch(function(err) {
                var resultEl = panel.querySelector('.apply-result');
                resultEl.className = 'apply-result err';
                resultEl.textContent = '\u2717 Request failed: ' + err;
                confirmBtn.disabled = false;
                confirmBtn.textContent = '\u2713 Confirm Copy';
            });
        });
    };

    function escHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
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

    # Deduplicate: collapse multiple references to the same missing file
    deduped = _dedup_broken_links(broken_links)

    # Collect unique values for filter dropdowns (from deduplicated set)
    all_types = sorted(set(b.link_type for b, _ in deduped))
    all_archives = sorted(set(
        c.archive_folder
        for b, _ in deduped
        for c in b.candidates
    ))

    # Build the broken links table rows
    rows_html = []
    for b, sources in deduped:
        # Source cell — list all pages that referenced this missing target
        source_parts = []
        seen_sources = set()
        for src_url, src_file, src_href in sources:
            display = src_url or str(src_file)
            if display in seen_sources:
                continue
            seen_sources.add(display)
            if src_url:
                source_parts.append(
                    '<a href="' + _e(src_url) + '" target="_blank">' + _e(src_url) + '</a>'
                )
            else:
                source_parts.append(_e(str(src_file)))
        source_cell = "<br>".join(source_parts)

        # Broken link cell — show unique hrefs used to reference this target
        seen_hrefs = []
        for _, _, src_href in sources:
            if src_href not in seen_hrefs:
                seen_hrefs.append(src_href)
        broken_cell = "<br>".join(_e(h) for h in seen_hrefs)

        # Reference count badge (only shown when > 1 source)
        if len(seen_sources) > 1:
            source_cell = (
                '<span style="font-size:0.78em;color:#1565c0;font-weight:600">'
                + str(len(seen_sources)) + ' pages</span><br>' + source_cell
            )

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
                # Apply button — only shown when report is served via Flask web UI
                # and the expected path is known (so we have a dst)
                apply_btn = ""
                if b.expected_path:
                    apply_btn = (
                        '<button class="btn-apply" '
                        'data-src="' + _e(str(c.local_path)) + '" '
                        'data-dst="' + _e(str(b.expected_path)) + '" '
                        'onclick="applyCandidate(this)">&#10003; Apply</button>'
                    )
                cand_parts.append(
                    '<div class="candidate">'
                    + bar + link_html
                    + apply_btn
                    + '<div class="cand-meta">'
                    + _e(c.archive_folder) + ' &bull; ' + _e(c.match_type) + ' &bull; ' + str(c.confidence) + '%'
                    + '</div></div>'
                )
            cand_cell = "\n".join(cand_parts)
        else:
            # Check for a Wayback candidate attached to this broken link
            wayback_cand = getattr(b, "wayback_candidate", None)
            if wayback_cand:
                bar = _confidence_bar(wayback_cand.confidence)
                link_html = '<a href="' + _e(wayback_cand.wayback_url) + '" target="_blank">' + _e(wayback_cand.wayback_url) + '</a>'
                apply_btn = ""
                if b.expected_path:
                    apply_btn = (
                        '<button class="btn-apply" '
                        'data-src="' + _e(str(wayback_cand.staged_path)) + '" '
                        'data-dst="' + _e(str(b.expected_path)) + '" '
                        'onclick="applyCandidate(this)">&#10003; Apply</button>'
                    )
                cand_cell = (
                    '<div class="candidate wayback-candidate">'
                    + '<span class="wayback-badge">&#127760; Wayback</span> '
                    + bar + link_html
                    + apply_btn
                    + '<div class="cand-meta">'
                    + _e(wayback_cand.archive_folder) + ' &bull; ' + _e(wayback_cand.match_type)
                    + ' &bull; ' + str(wayback_cand.confidence) + '%'
                    + '</div></div>'
                )
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
    ok_cls = "ok" if summary.unique_broken_total == 0 else "warn"
    cards_html = "".join([
        _card(summary.html_file_count, "HTML Files Scanned", "info"),
        _card(summary.total_links, "Links Checked", "info"),
        _card(summary.unique_broken_total, "Unique Missing Files", ok_cls),
        _card(summary.broken_total, "Total References", "warn" if summary.broken_total else "ok"),
        _card(summary.broken_images, "Broken Images", "warn" if summary.broken_images else "ok"),
        _card(summary.broken_pages, "Broken Pages", "warn" if summary.broken_pages else "ok"),
        _card(summary.broken_css + summary.broken_js, "Broken CSS/JS",
              "warn" if (summary.broken_css + summary.broken_js) else "ok"),
        _card(summary.broken_frames, "Broken Frames", "warn" if summary.broken_frames else "ok"),
        _card(summary.unique_with_candidates, "With Candidates", "info"),
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
