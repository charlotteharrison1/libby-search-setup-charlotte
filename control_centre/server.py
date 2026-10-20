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
import time
import webbrowser
from pathlib import Path

import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control_centre import actions, run_queue, status
from libby_core import assessment, descriptions, parse_groups, prompts
from third_sector import config as ts_config, generate_search as ts_generate_search, generations as ts_generations, settings as ts_settings
from uk.generate_search import slugify
from uk.settings import GROUP_LOG_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = 5151

QUEUE_LOG_DIR = Path(__file__).resolve().parent / "queue_logs"
QUEUE_LOG_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(Path(__file__).resolve().parent / "static"))

# Tracks the one currently-running command, if any — shared between a
# manual /api/run and the queue worker (see _queue_worker_loop), so the two
# can never run two processes at once, and /api/kill or /api/stdin always
# acts on whichever is actually active. A second manual /api/run while
# something's already running is now refused (see api_run) rather than
# silently replacing the tracked process, now that "something else running"
# routinely means the queue, not just a forgotten earlier click.
_current_proc_lock = threading.Lock()
_current_proc: subprocess.Popen | None = None


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/group-log")
def group_log_page():
    # A real separate page (own URL, own browser tab) rather than a
    # same-page view toggle — so it can stay open and be refreshed while
    # the main page is mid-run without either hiding the other.
    return send_from_directory(app.static_folder, "group_log.html")


@app.route("/analysis-centre")
def analysis_centre_page():
    return send_from_directory(app.static_folder, "analysis_centre.html")


# Shared pipeline prompts editable from the main page. The "original" is the
# DEFAULT_PROMPT_TEMPLATE constant in code (never edited); saved edits live in
# libby_core/prompt_overrides.json — see libby_core/prompts.py.
_PROMPTS = {
    "assessment": ("Group relevance assessment (Yes / Unsure / No)", assessment.DEFAULT_PROMPT_TEMPLATE,
                   {"group_name", "area_kind", "area_description"}, {"group_name", "area_kind", "area_description", "about_context"}),
    "description": ("Area description (cached once per area)", descriptions.DEFAULT_PROMPT_TEMPLATE,
                    {"area_kind", "area_name"}, {"area_kind", "area_name"}),
}


@app.route("/api/prompts")
def api_prompts():
    return jsonify({"prompts": [
        {"id": pid, "label": label, "original": default, "current": prompts.get(pid, default),
         "placeholders": sorted(allowed), "required": sorted(required)}
        for pid, (label, default, required, allowed) in _PROMPTS.items()
    ]})


@app.route("/api/prompts/<pid>", methods=["PUT"])
def api_prompt_save(pid):
    """Body {"text": "..."} saves an override; {"text": null} reverts to the original."""
    if pid not in _PROMPTS:
        return jsonify({"ok": False, "errors": ["unknown prompt"]}), 404
    text = (request.get_json(force=True) or {}).get("text")
    _, default, required, allowed = _PROMPTS[pid]
    if text and text != default:
        errors = prompts.check_placeholders(pid, text, required, allowed)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
    prompts.set_override(pid, None if not text or text == default else text)
    return jsonify({"ok": True})


@app.route("/third-sector")
def third_sector_page():
    return send_from_directory(app.static_folder, "third_sector.html")


@app.route("/api/third_sector/config", methods=["GET", "PUT"])
def api_third_sector_config():
    """Topics + editable prompts (third_sector/prompts.json). PUT validates
    placeholders before saving; the previous file is kept as prompts.json.bak."""
    if request.method == "GET":
        return jsonify(ts_config.load())
    cfg = request.get_json(force=True) or {}
    errors = ts_config.validate(cfg)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    ts_config.save(cfg)
    return jsonify({"ok": True})


@app.route("/api/third_sector/topics")
def api_third_sector_topics():
    """Per-topic progress, read straight off the files each step writes."""
    out = []
    for t in ts_config.load()["topics"]:
        slug = slugify(t["name"])
        targets = ts_settings.SEARCH_TARGETS_DIR / f"{slug}_search_targets.csv"
        scraped = ts_settings.SCRAPED_DIR / f"{slug}_search_targets.csv"
        raw = ts_settings.OUTPUT_DIR / f"raw_{slug}.csv"
        out.append({
            "slug": slug, "name": t["name"],
            "generated": targets.exists(), "pulled": scraped.exists(), "collected": raw.exists(),
        })
    return jsonify({"topics": out})


@app.route("/api/third_sector/terms/<slug>", methods=["GET", "PUT"])
def api_third_sector_terms(slug):
    """GET: the search terms in the topic's search-targets file (the editable
    source), plus per-term progress from the pulled scrape if there is one.
    PUT {"terms": [...]}: rewrite that file. Only the LOCAL file changes —
    re-push for libby to see it."""
    slug = slugify(slug)
    path = ts_settings.SEARCH_TARGETS_DIR / f"{slug}_search_targets.csv"
    scraped_path = ts_settings.SCRAPED_DIR / f"{slug}_search_targets.csv"
    if request.method == "PUT":
        try:
            ts_config.get_topic(slug)
        except KeyError:
            return jsonify({"ok": False, "message": "Unknown topic"}), 404
        terms = [str(t) for t in (request.get_json(force=True) or {}).get("terms", [])]
        n = ts_generate_search.save_terms(slug, terms)
        msg = f"Saved {n} terms locally — push again for libby to use them."
        if scraped_path.exists():
            msg += " (A pulled scrape already exists; it still reflects the old terms.)"
        return jsonify({"ok": True, "message": msg})
    if not path.exists():
        return jsonify({"terms": [], "scraped": False})
    df = pd.read_csv(path)
    progress = {}
    if scraped_path.exists():
        sc = pd.read_csv(scraped_path, dtype={"groups": str})
        progress = {r.search_string.lower(): (bool(r.processed), parse_groups.count_groups(r.groups)) for r in sc.itertuples()}
    return jsonify({"scraped": scraped_path.exists(), "terms": [
        {"term": t, "processed": progress.get(t.lower(), (False, None))[0],
         "n_groups": progress.get(t.lower(), (False, None))[1]}
        for t in df["search_string"]
    ]})


@app.route("/api/third_sector/generations/<slug>")
def api_third_sector_generations(slug):
    """History (newest last, prompts omitted for size) + per-prompt noise baselines."""
    entries = ts_generations.load(slugify(slug))
    return jsonify({
        "generations": [{k: e[k] for k in ("id", "stage", "source", "model", "prompt_hash")} | {"n": len(e["outputs"])} for e in entries],
        "baselines": ts_generations.baselines(entries),
    })


@app.route("/api/third_sector/generations/<slug>/compare")
def api_third_sector_compare(slug):
    byid = {e["id"]: e for e in ts_generations.load(slugify(slug))}
    a, b = byid.get(request.args.get("a")), byid.get(request.args.get("b"))
    if not a or not b:
        return jsonify({"error": "Unknown generation id"}), 400
    return jsonify(ts_generations.compare(a, b))


@app.route("/api/third_sector/group_log")
def api_third_sector_group_log():
    if not ts_settings.GROUP_LOG_PATH.exists():
        return jsonify({"rows": []})
    df = pd.read_csv(ts_settings.GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    return jsonify({"rows": df.to_dict(orient="records")})


@app.route("/api/status")
def api_status():
    rows = status.build_status_table()
    action_list = [
        {"id": aid, "label": label, "category": category, "needs_area": needs_area}
        for aid, (label, category, needs_area, _builder) in actions.ACTIONS.items()
    ]
    return jsonify({"areas": rows, "actions": action_list})


@app.route("/api/group_log")
def api_group_log():
    """Every group either pipeline has ever considered, accepted or not,
    and why — see uk.pipeline._upsert_group_log. Read fresh on every call
    (not cached) since it changes on every pipeline run."""
    if not GROUP_LOG_PATH.exists():
        return jsonify({"rows": []})
    # encoding_errors="surrogatepass" matches how this file is written
    # (errors="surrogatepass" — group names can carry unpaired surrogates
    # from mangled scraped emoji) — see uk.settings's note on why this,
    # not encoding="latin-1", is the correct way to read it back.
    df = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    return jsonify({"rows": df.to_dict(orient="records")})


@app.route("/api/recover_group", methods=["POST"])
def api_recover_group():
    """Manually recover one rejected group — shells out to
    uk/recover_group.py (never runs its logic in-process), same "only ever
    runs a real, known script" rule as every /api/run action. Runs
    synchronously and waits (no re-scraping/LLM calls involved, so this
    finishes in a second or two) rather than going through the streaming
    /api/run machinery, since the group-log page has no command log of its
    own to stream into."""
    body = request.get_json(force=True) or {}
    url = body.get("group_url")
    area_type = body.get("area_type")
    area_name = body.get("area_name")
    note = body.get("note", "")
    if not url or area_type not in ("constituency", "ward", "custom") or not area_name:
        return jsonify({
            "ok": False,
            "message": "group_url, area_type ('constituency', 'ward', or 'custom'), and area_name are required",
        }), 400

    cmd = [
        "python3", "-m", "uk.recover_group",
        "--url", url, "--area-type", area_type, "--area-name", area_name,
    ]
    if note:
        cmd += ["--note", note]

    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "message": "Recovery timed out after 60s"}), 504

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return jsonify({"ok": False, "message": output or f"recover_group.py exited {proc.returncode}"}), 400
    return jsonify({"ok": True, "message": output})


@app.route("/api/remove_group", methods=["POST"])
def api_remove_group():
    """Manually remove one accepted group — the reverse of
    /api/recover_group, same shell-out-to-a-real-script pattern."""
    body = request.get_json(force=True) or {}
    url = body.get("group_url")
    area_type = body.get("area_type")
    area_name = body.get("area_name")
    note = body.get("note", "")
    if not url or area_type not in ("constituency", "ward", "custom") or not area_name:
        return jsonify({
            "ok": False,
            "message": "group_url, area_type ('constituency', 'ward', or 'custom'), and area_name are required",
        }), 400

    cmd = [
        "python3", "-m", "uk.remove_group",
        "--url", url, "--area-type", area_type, "--area-name", area_name,
    ]
    if note:
        cmd += ["--note", note]

    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "message": "Removal timed out after 60s"}), 504

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return jsonify({"ok": False, "message": output or f"remove_group.py exited {proc.returncode}"}), 400
    return jsonify({"ok": True, "message": output})


@app.route("/api/analysis/private_by_area_type")
def api_analysis_private_by_area_type():
    from uk import analysis
    return jsonify(analysis.private_groups_by_area_type())


@app.route("/api/analysis/group_stats_by_area_type")
def api_analysis_group_stats_by_area_type():
    from uk import analysis
    return jsonify(analysis.group_stats_by_area_type())


@app.route("/api/analysis/accepted_group_points")
def api_analysis_accepted_group_points():
    from uk import analysis
    return jsonify(analysis.accepted_group_points())


@app.route("/api/analysis/accepted_averages_by_area")
def api_analysis_accepted_averages_by_area():
    from uk import analysis
    return jsonify(analysis.accepted_averages_by_area())


def _resolve_command(body: dict) -> tuple[list[str] | None, tuple[dict, int] | None]:
    """Shared by /api/run and /api/preview: resolve the request body to an
    argv list via actions.build_command — the ONE place command-building
    happens, so a preview can never drift from what actually executes.
    Returns (cmd, None) on success or (None, (json_body, status_code)) on
    error."""
    action_id = body.get("action_id")
    slug = body.get("slug")
    params = body.get("params", {})

    if action_id not in actions.ACTIONS and action_id not in actions.TS_ACTIONS:
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


def _stream_command(cmd: list[str], cwd: Path | None = None, result: dict | None = None):
    """Generator: first line is the literal command (so the UI can show
    exactly what's running before any output arrives), then each
    stdout/stderr line as it's produced, then a final exit-code line.

    stdin is always left open (not closed, nothing auto-written) so
    /api/stdin can send it real input for the life of the run — e.g.
    run_remote_scrape's script.py, which blocks on a login-confirmation
    keypress. Every other action just never gets anything written to it,
    which is harmless — none of them read stdin at all.

    If `result` (a dict) is given, its "returncode" key is set once the
    process exits (or to None if it never started) — lets a caller that
    isn't an HTTP response (the queue worker, writing to a log file
    instead) learn the outcome without parsing the yielded text."""
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
        if result is not None:
            result["returncode"] = None
        return

    with _current_proc_lock:
        _current_proc = proc
    try:
        for line in proc.stdout:
            yield line
        proc.wait()
        if result is not None:
            result["returncode"] = proc.returncode
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
    # A pre-check, not a hard lock (the generator below is what actually
    # claims _current_proc, and Flask doesn't start it until the response
    # is consumed) — good enough for a single-user local tool where the
    # realistic race is "the queue happened to pick something up between
    # this check and your click", not concurrent clients fighting over it.
    with _current_proc_lock:
        busy = _current_proc is not None
    if busy:
        return jsonify({"error": "Another command (manual or queued) is already running — stop it first or wait."}), 409
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


# ── Queue ─────────────────────────────────────────────────────────────────
# A persisted, unattended-friendly alternative to clicking Run per area —
# see control_centre/run_queue.py for the state management. The worker below is
# the only thing that actually executes a queued item; it reuses
# _stream_command exactly as a manual /api/run does, just writing the
# output to a log file instead of an HTTP response, and only ever starts
# something when _current_proc is free (see api_run's matching check).

@app.route("/api/queue")
def api_queue_list():
    return jsonify({"items": run_queue.list_items(), "paused": run_queue.is_paused()})


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    body = request.get_json(force=True) or {}
    action_id = body.get("action_id")
    slug = body.get("slug")
    params = body.get("params", {})

    # Same validation /api/run and /api/preview already share — a bad queue
    # entry is rejected immediately, not discovered later by the worker.
    cmd, error = _resolve_command({"action_id": action_id, "slug": slug, "params": params})
    if error:
        body_, code = error
        return jsonify(body_), code

    action_label = actions.ACTIONS[action_id][0]
    area_label = None
    if slug:
        row = next((r for r in status.build_status_table() if r["slug"] == slug), None)
        area_label = row["name"] if row else slug
    label = f"{action_label} — {area_label}" if area_label else action_label

    item = run_queue.add(action_id, slug, params, label)
    return jsonify({"ok": True, "item": dict(item)})


@app.route("/api/queue/remove", methods=["POST"])
def api_queue_remove():
    item_id = (request.get_json(force=True) or {}).get("id")
    return jsonify({"ok": run_queue.remove(item_id)})


@app.route("/api/queue/retry", methods=["POST"])
def api_queue_retry():
    item_id = (request.get_json(force=True) or {}).get("id")
    return jsonify({"ok": run_queue.retry(item_id)})


@app.route("/api/queue/clear_finished", methods=["POST"])
def api_queue_clear_finished():
    return jsonify({"ok": True, "cleared": run_queue.clear_finished()})


@app.route("/api/queue/pause", methods=["POST"])
def api_queue_pause():
    run_queue.set_paused(True)
    return jsonify({"ok": True})


@app.route("/api/queue/resume", methods=["POST"])
def api_queue_resume():
    run_queue.set_paused(False)
    return jsonify({"ok": True})


@app.route("/api/queue/log/<item_id>")
def api_queue_log(item_id):
    log_path = QUEUE_LOG_DIR / f"{item_id}.log"
    if not log_path.exists():
        return jsonify({"log": ""})
    return jsonify({"log": log_path.read_text(errors="replace")})


def _queue_worker_loop():
    while True:
        time.sleep(2)
        if run_queue.is_paused():
            continue
        with _current_proc_lock:
            if _current_proc is not None:
                continue  # a manual run or another queue item is already active

        item = run_queue.next_pending()
        if item is None:
            continue

        row = None
        if item["slug"]:
            row = next((r for r in status.build_status_table() if r["slug"] == item["slug"]), None)
        try:
            cmd = actions.build_command(item["action_id"], row, item["params"])
        except actions.UnknownAction as e:
            # The area's state changed since this was queued (e.g. its
            # scrape file went away) — fail it visibly rather than looping
            # on it forever.
            run_queue.mark_running(item)
            (QUEUE_LOG_DIR / f"{item['id']}.log").write_text(f"Could not build command: {e}\n")
            run_queue.mark_finished(item, 1)
            continue

        run_queue.mark_running(item)
        log_path = QUEUE_LOG_DIR / f"{item['id']}.log"
        result: dict = {}
        with open(log_path, "w") as logf:
            for line in _stream_command(cmd, result=result):
                logf.write(line)
                logf.flush()
        run_queue.mark_finished(item, result.get("returncode", 1))


def main():
    url = f"http://127.0.0.1:{PORT}"
    threading.Thread(target=_queue_worker_loop, daemon=True).start()
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Libby Control Centre running at {url}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
