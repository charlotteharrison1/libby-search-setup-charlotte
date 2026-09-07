#!/usr/bin/env python3
"""Libby Control Centre — a small local web dashboard over the existing
CLI scripts (sync_scrape.sh, pull_about.sh, batch_pipeline.sh, uk.pipeline,
uk.pipeline_ward, and the remote_scripts on the libby device).

Deliberately not a rewrite of any pipeline logic — every action here shells
out to the exact same script/command you'd type yourself (see actions.py's
build_command), and the UI always shows that literal command before/while
it runs. This is a local, single-user tool: binds to 127.0.0.1 only, and
only ever runs one of a fixed set of known commands (see actions.py) —
never arbitrary shell input from the browser.

Run:
    python3 control_centre/server.py
    (opens http://127.0.0.1:5151 automatically)
"""

import shlex
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control_centre import actions, status

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = 5151

app = Flask(__name__, static_folder=str(Path(__file__).resolve().parent / "static"))


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/status")
def api_status():
    rows = status.build_status_table()
    action_list = [
        {"id": aid, "label": label, "category": category, "needs_area": needs_area}
        for aid, (label, category, needs_area, _builder) in actions.ACTIONS.items()
    ]
    return jsonify({"areas": rows, "actions": action_list})


def _resolve_command(body: dict) -> tuple[list[str] | None, tuple[dict, int] | None]:
    """Shared by /api/run and /api/preview: resolve the request body to an
    argv list via actions.build_command — the ONE place command-building
    happens, so a preview can never drift from what actually executes.
    Returns (cmd, None) on success or (None, (json_body, status_code)) on
    error."""
    action_id = body.get("action_id")
    slug = body.get("slug")
    params = body.get("params", {})

    if action_id not in actions.ACTIONS:
        return None, ({"error": f"Unknown action: {action_id}"}, 400)

    row = None
    if slug:
        row = next((r for r in status.build_status_table() if r["slug"] == slug), None)
        if row is None:
            return None, ({"error": f"Unknown area slug: {slug}"}, 400)

    try:
        cmd = actions.build_command(action_id, row, params)
    except actions.UnknownAction as e:
        return None, ({"error": str(e)}, 400)

    return cmd, None


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Returns the exact command a matching /api/run call would execute,
    without running it — lets the UI show it before you click Run, not
    just as the run starts."""
    cmd, error = _resolve_command(request.get_json(force=True))
    if error:
        body, code = error
        return jsonify(body), code
    return jsonify({"command": shlex.join(cmd)})


def _stream_command(cmd: list[str], cwd: Path | None = None):
    """Generator: first line is the literal command (so the UI can show
    exactly what's running before any output arrives), then each
    stdout/stderr line as it's produced, then a final exit-code line."""
    yield f"$ {shlex.join(cmd)}\n\n"
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or REPO_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        yield f"!! Could not start command: {e}\n"
        return

    for line in proc.stdout:
        yield line
    proc.wait()
    yield f"\n[exit code {proc.returncode}]\n"


@app.route("/api/run", methods=["POST"])
def api_run():
    cmd, error = _resolve_command(request.get_json(force=True))
    if error:
        body, code = error
        return jsonify(body), code
    return Response(_stream_command(cmd), mimetype="text/plain")


def main():
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Libby Control Centre running at {url}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
