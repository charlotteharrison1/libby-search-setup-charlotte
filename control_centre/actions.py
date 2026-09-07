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


def _ssh(remote_command: str) -> list[str]:
    return ["ssh", DEVICE, remote_command]


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
        cmd = ["python3", "-m", "uk.pipeline", "--constituency", row["name"]]
        if params.get("stop_before_ai_assessment"):
            cmd.append("--stop-before-ai-assessment")
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


# id -> (label, category, needs_area, builder)
# needs_area=False for actions that don't take a specific area (e.g. "pick
# next" scans the device itself for what to do next).
ACTIONS = {
    "prep":                    ("Generate + push (prep)",        "Prep",    True,  _build_prep),
    "push":                    ("Push",                          "Push",    True,  _build_push),
    "pull":                    ("Pull scraped groups",           "Pull",    True,  _build_pull),
    "pull_about":              ("Pull About-scrape",             "Pull",    True,  _build_pull_about),
    "run_pipeline":            ("Run pipeline",                  "Process", True,  _build_run_pipeline),
    "set_scrape_target":       ("Set scrape target",             "Remote",  True,  _build_set_scrape_target),
    "pick_next_scrape_target": ("Pick next scrape target",       "Remote",  False, _build_pick_next_scrape_target),
    "set_about_target":        ("Set About-scrape target",       "Remote",  True,  _build_set_about_target),
    "pick_next_about_target":  ("Pick next About-scrape target", "Remote",  False, _build_pick_next_about_target),
}


def build_command(action_id: str, row: dict | None, params: dict) -> list[str]:
    if action_id not in ACTIONS:
        raise UnknownAction(f"Unknown action: {action_id}")
    _, _, needs_area, builder = ACTIONS[action_id]
    if needs_area and row is None:
        raise UnknownAction(f"{action_id} requires an area")
    return builder(row, params)
