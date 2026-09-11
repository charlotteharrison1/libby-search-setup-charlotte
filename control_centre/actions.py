"""Fixed registry of commands the control centre can run. Deliberately NOT
a free-form shell box — every action builds an explicit argv list (never
shell=True / string interpolation of user input), so the UI can only ever
run one of these known commands, just with area-specific arguments filled
in. This is also what "show the command that's really running" means in
practice: build_command() returns the exact argv the UI displays and
subprocess actually executes — never a paraphrase.

Each action declares which area `type` (constituency/ward/either) it
applies to, since uk.pipeline and uk.pipeline_ward take different
arguments (a display name vs a scraped file path).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVICE = "libby"
REMOTE_BASE = "/home/pub/libby_download"


class UnknownAction(Exception):
    pass


def _ssh(remote_command: str, force_tty: bool = False) -> list[str]:
    # force_tty (-tt): a bare `ssh host "cmd"` never allocates a remote
    # pseudo-terminal, unlike a real interactive `ssh host` session. That
    # turned out to matter for run_remote_scrape specifically — script.py
    # launches Chrome successfully in a real interactive session but not
    # through a bare non-interactive one, and -tt is the standard fix for
    # this exact class of "remote GUI automation over SSH" problem. -tt
    # (not a single -t) forces allocation even though the LOCAL side (this
    # Flask subprocess) has no real terminal of its own to request one from.
    cmd = ["ssh"]
    if force_tty:
        cmd.append("-tt")
    cmd += [DEVICE, remote_command]
    return cmd


# Each builder takes (row: dict from status.build_status_table, params: dict
# from the request body) and returns an argv list. `cwd` is always REPO_ROOT
# except where noted.

def _build_prep(row, params):
    if row["type"] != "constituency":
        raise UnknownAction("Prep (generate+push) only supports constituencies — ward generation uses adhoc_wards.csv (see generate_search_ward.py) and isn't a one-name action.")
    cmd = [str(REPO_ROOT / "batch_pipeline.sh"), "prep"]
    if params.get("force"):
        cmd.append("--force")
    cmd.append(row["name"])
    return cmd


def _build_prep_bulk(row, params):
    # Doesn't use `row`/needs_area — the frontend's "Pick 5" resolves which
    # areas from the already-loaded status data (no server round-trip to
    # decide "next"), and sends the chosen names directly.
    names = params.get("names") or []
    if not names:
        raise UnknownAction("Bulk prep needs at least one area name (pick via \"Pick 5\" first)")
    cmd = [str(REPO_ROOT / "batch_pipeline.sh"), "prep"]
    if params.get("force"):
        cmd.append("--force")
    cmd.extend(names)
    return cmd


def _build_push(row, params):
    cmd = [str(REPO_ROOT / "sync_scrape.sh"), "push"]
    if row["type"] == "ward":
        cmd.append("--wards")
    cmd.append(row["name"])
    return cmd


def _build_pull(row, params):
    cmd = [str(REPO_ROOT / "sync_scrape.sh"), "pull"]
    if row["type"] == "ward":
        cmd.append("--wards")
    cmd.append(row["name"])
    return cmd


def _build_pull_about(row, params):
    cmd = [str(REPO_ROOT / "pull_about.sh")]
    if row["type"] == "ward":
        cmd.append("--wards")
    cmd.append(row["name"])
    return cmd


def _build_run_pipeline(row, params):
    if row["type"] == "constituency":
        # Must pass --input explicitly, pointing at this constituency's own
        # pulled scrape (uk/data/scraped/<slug>_search_targets.csv) — the
        # same file batch_pipeline.sh's "sync" mode uses. Without --input,
        # uk.pipeline falls back to its NEW_SCRAPE_PATH default
        # (master_constituency_place_data_file.csv), a single shared file
        # that's only ever correct for a full, all-constituencies run — not
        # for one constituency at a time.
        scraped_path = REPO_ROOT / "uk" / "data" / "scraped" / f"{row['slug']}_search_targets.csv"
        cmd = ["python3", "-m", "uk.pipeline", "--input", str(scraped_path), "--constituency", row["name"]]
        if params.get("stop_before_ai_assessment"):
            cmd.append("--stop-before-ai-assessment")
        if params.get("force"):
            # uk.pipeline_ward has no such flag — it always reprocesses fresh
            # (no per-ward resumability cache), so --force is constituency-only.
            cmd.append("--force")
    else:
        scraped_path = REPO_ROOT / "uk" / "data" / "scraped" / "wards" / f"{row['slug']}_search_targets.csv"
        cmd = ["python3", "-m", "uk.pipeline_ward", "--input", str(scraped_path)]
        if params.get("stop_before_ai_assessment"):
            cmd.append("--stop-before-ai-assessment")
    if params.get("about"):
        cmd.append("--about")
    elif params.get("context"):
        cmd.append("--context")
    return cmd


def _build_set_scrape_target(row, params):
    remote_cmd = f"cd {REMOTE_BASE} && ./set_scrape_target.sh"
    if params.get("force"):
        remote_cmd += " --force"
    remote_cmd += f" {row['slug']}"
    return _ssh(remote_cmd)


def _build_view_scrape_target(row, params):
    # Read-only: just prints clacton.json so you can see what's currently
    # configured (master_file_name / output_directory) without having to
    # ssh in by hand to check.
    remote_cmd = f"cd {REMOTE_BASE} && cat clacton.json"
    return _ssh(remote_cmd)


def _build_pick_next_scrape_target(row, params):
    remote_cmd = f"cd {REMOTE_BASE} && ./pick_next_scrape_target.sh"
    if params.get("dry_run"):
        remote_cmd += " --dry-run"
    return _ssh(remote_cmd)


def _build_set_about_target(row, params):
    remote_cmd = f"cd {REMOTE_BASE} && ./set_about_target.sh"
    if params.get("force"):
        remote_cmd += " --force"
    remote_cmd += f" {row['slug']}"
    return _ssh(remote_cmd)


def _build_pick_next_about_target(row, params):
    remote_cmd = f"cd {REMOTE_BASE} && ./pick_next_about_target.sh"
    if params.get("dry_run"):
        remote_cmd += " --dry-run"
    return _ssh(remote_cmd)


def _build_run_remote_scrape(row, params):
    # Replaces the manual `ssh libby` -> script.py -> press Enter routine.
    # Deliberately does NOT set the scrape target itself — that's a
    # separate decision (set_scrape_target / pick_next_scrape_target,
    # already their own actions) from "run whatever's currently
    # configured", which is what you actually do most of the time (e.g.
    # resuming after script.py's own overnight pause, with the target
    # unchanged from before).
    #
    # Root cause of the SessionNotCreatedException, confirmed by hand:
    # settings.py hardcodes headless=False, so Chrome always opens a real
    # window and needs an actual X display — a plain ssh session (even
    # with a pty) has no DISPLAY at all. There's a persistent Xtigervnc
    # session on :2 on this device; DISPLAY=:2 is what a manual run
    # actually relies on. Confirmed directly: with DISPLAY=:2 set, Chrome
    # launches and script.py reaches its "Log in manually and press
    # Enter..." prompt, same as a manual run — no exception.
    #
    # force_tty=True is still needed on top of that: -tt allocates a real
    # pseudo-terminal so the underlying ssh command behaves like a normal
    # interactive session for the process's stdin, which the login prompt
    # below waits on.
    #
    # script.py's login prompt still needs a keypress once the browser's
    # up (the profile is already authenticated — no real credentials
    # involved); the process's stdin is left open for the whole run (see
    # server.py's /api/stdin) so you can send it yourself once you can see
    # in the log that it's actually ready, same as a real SSH session.
    remote_cmd = f"cd {REMOTE_BASE} && DISPLAY=:2 python3 script.py --params clacton.json"
    return _ssh(remote_cmd, force_tty=True)


# id -> (label, category, needs_area, builder)
# needs_area=False for actions that don't take a specific area (e.g. "pick
# next" scans the device itself for what to do next).
ACTIONS = {
    "prep":                    ("Generate + push (prep)",        "Prep",    True,  _build_prep),
    "prep_bulk":               ("Bulk prep (Pick 5)",            "Prep",    False, _build_prep_bulk),
    "push":                    ("Push",                          "Push",    True,  _build_push),
    "pull":                    ("Pull scraped groups",           "Pull",    True,  _build_pull),
    "pull_about":              ("Pull About-scrape",             "Pull",    True,  _build_pull_about),
    "run_pipeline":            ("Run pipeline",                  "Process", True,  _build_run_pipeline),
    "set_scrape_target":       ("Set scrape target",             "Remote",  True,  _build_set_scrape_target),
    "view_scrape_target":      ("View current scrape target",    "Remote",  False, _build_view_scrape_target),
    "pick_next_scrape_target": ("Pick next scrape target",       "Remote",  False, _build_pick_next_scrape_target),
    "set_about_target":        ("Set About-scrape target",       "Remote",  True,  _build_set_about_target),
    "pick_next_about_target":  ("Pick next About-scrape target", "Remote",  False, _build_pick_next_about_target),
    "run_remote_scrape":       ("Run remote scrape (script.py, current target)", "Remote", False, _build_run_remote_scrape),
}


def build_command(action_id: str, row: dict | None, params: dict) -> list[str]:
    if action_id not in ACTIONS:
        raise UnknownAction(f"Unknown action: {action_id}")
    _, _, needs_area, builder = ACTIONS[action_id]
    if needs_area and row is None:
        raise UnknownAction(f"{action_id} requires an area")
    return builder(row, params)
