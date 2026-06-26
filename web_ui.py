"""
web_ui.py — Flask web front-end for archive-validator.

Provides a browser-based form for configuring and running the validator,
with live progress streaming via Server-Sent Events (SSE) and a link to
the generated report when the job completes.

Usage:
    pip install flask
    python web_ui.py

Then open http://127.0.0.1:5000 in your browser.

Security note: This app is intended for use on a trusted internal network
or localhost only. Do not expose it to the public internet without adding
authentication, as it can execute commands on the server filesystem.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, render_template, request, send_file, stream_with_context

# Apply log file (append-only record of every copy operation)
APPLY_LOG = Path(__file__).parent / "apply.log"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global job state (single-job-at-a-time model)
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_job_running = False
_job_output_queue: queue.Queue = queue.Queue()
_job_exit_code: int | None = None
_job_report_path: str | None = None


def _reset_job():
    global _job_running, _job_exit_code, _job_report_path
    _job_running = False
    _job_exit_code = None
    _job_report_path = None
    # Drain the queue
    while not _job_output_queue.empty():
        try:
            _job_output_queue.get_nowait()
        except queue.Empty:
            break


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Render the configuration form."""
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run_job():
    """
    Start a validation job from the submitted form data.
    Returns JSON with {ok: bool, error?: str}.
    """
    global _job_running, _job_exit_code, _job_report_path

    with _job_lock:
        if _job_running:
            return {"ok": False, "error": "A job is already running."}, 409

        # --- Build the command ---
        cmd = [sys.executable, "-m", "archive_validator"]

        input_dir = request.form.get("input_dir", "").strip()
        if not input_dir:
            return {"ok": False, "error": "--input-dir is required."}, 400
        cmd += ["--input-dir", input_dir]

        base_url = request.form.get("base_url", "").strip()
        if base_url:
            cmd += ["--base-url", base_url]

        search_base_url = request.form.get("search_base_url", "").strip()
        if search_base_url:
            cmd += ["--search-base-url", search_base_url]

        search_dir = request.form.get("search_dir", "").strip()
        if search_dir:
            cmd += ["--search-dir", search_dir]

        max_candidates = request.form.get("max_candidates", "5").strip()
        if max_candidates and max_candidates != "5":
            cmd += ["--max-candidates", max_candidates]

        if request.form.get("fuzzy"):
            cmd.append("--fuzzy")

        output = request.form.get("output", "report.html").strip() or "report.html"
        cmd += ["--output", output]

        json_output = request.form.get("json_output", "").strip()
        if json_output:
            cmd += ["--json-output", json_output]

        csv_output = request.form.get("csv_output", "").strip()
        if csv_output:
            cmd += ["--csv-output", csv_output]

        include_ext = request.form.get("include_ext", "").strip()
        if include_ext:
            cmd += ["--include-ext", include_ext]

        # ignore_patterns is a list of fields named "ignore_pattern"
        ignore_patterns = request.form.getlist("ignore_pattern")
        for pat in ignore_patterns:
            pat = pat.strip()
            if pat:
                cmd += ["--ignore-pattern", pat]

        case_mode = request.form.get("case_mode", "")
        if case_mode == "insensitive":
            cmd.append("--case-insensitive")
        elif case_mode == "sensitive":
            cmd.append("--case-sensitive")

        if request.form.get("external"):
            cmd.append("--external")

        # Wayback Machine options
        original_url = request.form.get("original_url", "").strip()
        if request.form.get("wayback") and original_url:
            cmd.append("--wayback")
            cmd += ["--original-url", original_url]
            wayback_staging = request.form.get("wayback_staging", "wayback_staging").strip()
            if wayback_staging:
                cmd += ["--wayback-staging", wayback_staging]
            wayback_workers = request.form.get("wayback_workers", "3").strip()
            if wayback_workers and wayback_workers != "3":
                cmd += ["--wayback-workers", wayback_workers]

        # Never pass --quiet so we always get progress output
        # (the UI shows it live)

        _reset_job()
        _job_running = True
        _job_report_path = output

        # Start the job in a background thread
        thread = threading.Thread(
            target=_run_subprocess,
            args=(cmd,),
            daemon=True,
        )
        thread.start()

    return {"ok": True}


@app.route("/stream")
def stream():
    """
    SSE endpoint. Streams job output lines to the browser.
    Each event is a JSON object: {type: "line"|"done", text?: str, exit_code?: int}
    """
    def generate():
        while True:
            try:
                item = _job_output_queue.get(timeout=30)
            except queue.Empty:
                # Send a keepalive comment
                yield ": keepalive\n\n"
                continue

            if item is None:
                # Sentinel: job finished
                yield f"data: {json.dumps({'type': 'done', 'exit_code': _job_exit_code})}\n\n"
                break
            else:
                yield f"data: {json.dumps({'type': 'line', 'text': item})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@app.route("/status")
def status():
    """Return current job status as JSON."""
    return {
        "running": _job_running,
        "exit_code": _job_exit_code,
        "report_path": _job_report_path,
    }


@app.route("/report")
def serve_report():
    """Serve the generated HTML report file."""
    report_path = request.args.get("path", "report.html")
    p = Path(report_path)
    if not p.exists() or not p.is_file():
        return "Report not found.", 404
    # Only serve .html files for safety
    if p.suffix.lower() != ".html":
        return "Only HTML reports can be served.", 400
    return send_file(str(p.resolve()), mimetype="text/html")


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------

def _run_subprocess(cmd: list[str]) -> None:
    """
    Run the validator subprocess, feeding its stderr output into the queue.
    Puts None as a sentinel when done.
    """
    global _job_running, _job_exit_code

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
            text=True,
            bufsize=1,
            cwd=str(Path(__file__).parent),
        )

        for line in proc.stdout:
            _job_output_queue.put(line.rstrip("\n"))

        proc.wait()
        _job_exit_code = proc.returncode

    except Exception as e:
        _job_output_queue.put(f"[ERROR] Failed to start job: {e}")
        _job_exit_code = 2
    finally:
        _job_running = False
        _job_output_queue.put(None)  # Sentinel


# ---------------------------------------------------------------------------
# Apply endpoint — copy a candidate file to the archive
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["POST"])
def apply_candidate():
    """
    Copy a candidate file to the expected (broken) path.

    Request JSON: { "src": "/abs/path/to/candidate", "dst": "/abs/path/to/expected" }
    Response JSON: { "ok": bool, "message": str }

    Safety:
    - Both paths must be absolute
    - src must exist and be a file
    - dst must not already exist (prevents silent overwrites)
    - dst parent directory is created if needed
    - Every operation is logged to apply.log
    """
    data = request.get_json(force=True, silent=True) or {}
    src_str = data.get("src", "").strip()
    dst_str = data.get("dst", "").strip()

    if not src_str or not dst_str:
        return {"ok": False, "message": "Both 'src' and 'dst' are required."}, 400

    src = Path(src_str)
    dst = Path(dst_str)

    # Validate source
    if not src.is_absolute():
        return {"ok": False, "message": f"src must be an absolute path: {src}"}, 400
    if not src.exists():
        return {"ok": False, "message": f"Source file not found: {src}"}, 404
    if not src.is_file():
        return {"ok": False, "message": f"Source is not a file: {src}"}, 400

    # Validate destination
    if not dst.is_absolute():
        return {"ok": False, "message": f"dst must be an absolute path: {dst}"}, 400
    if dst.exists():
        return {
            "ok": False,
            "message": f"Destination already exists: {dst}. Remove it first if you want to replace it."
        }, 409

    # Create parent directories
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "message": f"Cannot create destination directory: {e}"}, 500

    # Copy the file (preserves timestamps)
    try:
        shutil.copy2(str(src), str(dst))
    except OSError as e:
        return {"ok": False, "message": f"Copy failed: {e}"}, 500

    # Log the operation
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] COPY {src} -> {dst}\n"
    try:
        with open(APPLY_LOG, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except OSError:
        pass  # Log failure is non-fatal

    return {
        "ok": True,
        "message": f"Copied successfully to {dst}",
        "dst": str(dst),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("archive-validator web UI")
    print("Open http://127.0.0.1:5000 in your browser")
    print("Press Ctrl+C to stop")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
