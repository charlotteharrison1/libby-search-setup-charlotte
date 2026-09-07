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

# Tracks the one currently-running command, if any, so /api/kill has
# something to stop. This is a single-user local tool — one slot is enough;
# a second run started before the first finishes just replaces the tracked
# process (its own /api/run call still streams its own output/exit code
# regardless of tracking).
_current_proc_lock = threading.Lock()
_current_proc: subprocess.Popen | None = None


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
    stdout/stderr line as it's produced, then a final exit-code line.

    stdin is always left open (not closed, nothing auto-written) so
    /api/stdin can send it real input for the life of the run — e.g.
    run_remote_scrape's script.py, which blocks on a login-confirmation
    keypress. Every other action just never gets anything written to it,
    which is harmless — none of them read stdin at all."""
    global _current_proc

    yield f"$ {shlex.join(cmd)}\n\n"
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        yield f"!! Could not start command: {e}\n"
        return

    with _current_proc_lock:
        _current_proc = proc
    try:
        for line in proc.stdout:
            yield line
        proc.wait()
        if proc.returncode is not None and proc.returncode < 0:
            yield f"\n[stopped — signal {-proc.returncode}]\n"
        else:
            yield f"\n[exit code {proc.returncode}]\n"
    finally:
        with _current_proc_lock:
            if _current_proc is proc:
                _current_proc = None
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass


@app.route("/api/run", methods=["POST"])
def api_run():
    cmd, error = _resolve_command(request.get_json(force=True))
    if error:
        body, code = error
        return jsonify(body), code
    return Response(_stream_command(cmd), mimetype="text/plain")


@app.route("/api/stdin", methods=["POST"])
def api_stdin():
    """Sends a line of text to the currently-running command's stdin — a
    real interactive terminal for the one process /api/kill also tracks,
    not a timed guess at what it needs. Used for e.g. run_remote_scrape's
    script.py, which waits for a keypress once you can see in the log that
    the browser's actually come up."""
    text = (request.get_json(force=True) or {}).get("text", "")
    with _current_proc_lock:
        proc = _current_proc
    if proc is None or proc.poll() is not None:
        return jsonify({"ok": False, "message": "No command is currently running"}), 400
    try:
        proc.stdin.write(text + "\n")
        proc.stdin.flush()
    except (OSError, ValueError) as e:
        return jsonify({"ok": False, "message": f"Could not send input: {e}"}), 400
    return jsonify({"ok": True, "message": "Sent"})


@app.route("/api/kill", methods=["POST"])
def api_kill():
    """Stops the currently-running command, if any. For a remote (ssh)
    command this terminates the local ssh client — that closes the
    connection, which in practice ends the remote command too for the
    plain (non-backgrounded) `ssh host 'cmd'` form every action here uses,
    but isn't a hard guarantee the way it is for a local process."""
    with _current_proc_lock:
        proc = _current_proc
    if proc is None or proc.poll() is not None:
        return jsonify({"ok": False, "message": "No command is currently running"}), 400

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    return jsonify({"ok": True, "message": "Stop signal sent"})


def main():
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Libby Control Centre running at {url}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
