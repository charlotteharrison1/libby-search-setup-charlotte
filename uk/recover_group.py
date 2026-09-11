#!/usr/bin/env python3
"""Manually recover one group that a pipeline run rejected.

No re-scraping, no LLM calls: the group's full row is re-derived from that
area's own already-scraped CSV (see uk.pipeline.reconstruct_constituency_
candidates / uk.pipeline_ward.reconstruct_ward_candidates — the same
combine/aggregate step a normal run does, just without the filtering or AI
assessment stages), then appended to whichever file currently holds that
area's output — Clacton-etc/groups/ if it's already been promoted, else
Clacton-etc/inputs/ (or inputs/wards/) if it's still staged.

Three things happen, in order, and only if the group isn't already there:
  1. The row is appended to that groups_<Name>.csv (a .bak backup of the
     file is written first).
  2. group_log.csv's row for this group is updated to accepted="Y",
     stage="recovered".
  3. The decision is recorded in group_overrides.csv, so a real future
     reprocess of this area (uk.pipeline --force, or any uk.pipeline_ward
     run) re-applies it instead of silently dropping the group again — see
     uk.pipeline._load_overrides_by_area / _missing_override_rows.

Usage:
    python -m uk.recover_group --url <group url> --area-type constituency \\
        --area-name "Holborn and St Pancras" --note "clearly local, About page confirms"
    python -m uk.recover_group --url <group url> --area-type ward \\
        --area-name "Garden Suburb (Barnet)"
"""

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from uk import pipeline, pipeline_ward
from uk.pipeline import GROUP_LOG_COLUMNS
from uk.pipeline_ward import FINAL_COLUMNS, _parse_ward_area_name
from uk.settings import CLACTON_GROUPS_DIR, CLACTON_INPUTS_DIR, GROUP_LOG_PATH, GROUP_OVERRIDES_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

OVERRIDE_COLUMNS = ["group_url", "area_type", "area_name", "note", "recovered_at"]


class RecoveryError(Exception):
    """Raised for any expected failure (group/file not found, bad area) —
    meant to surface as a clean one-line error, not a stack trace."""


def _target_file(area_type: str, area_name: str, ward_name: str | None) -> Path | None:
    """Whichever of the promoted (Clacton-etc/groups/) or staged
    (Clacton-etc/inputs/...) location currently holds this area's output
    file — promoted takes priority, since that's the more "final" state.
    None if the area has neither yet (nothing to recover into)."""
    filename_base = ward_name if area_type == "ward" else area_name
    promoted = CLACTON_GROUPS_DIR / f"groups_{filename_base}.csv"
    if promoted.exists():
        return promoted
    if area_type == "ward":
        staged = CLACTON_INPUTS_DIR / "wards" / f"groups_{filename_base}.csv"
    else:
        staged = CLACTON_INPUTS_DIR / f"groups_{filename_base}.csv"
    if staged.exists():
        return staged
    return None


def _update_group_log_row(area_type: str, area_name: str, url: str, name: str, note: str) -> None:
    """Targeted update — replaces just this one (area_type, area_name, url)
    row, unlike uk.pipeline._upsert_group_log's whole-area replace (which
    would wipe out every other group's log entry for this area)."""
    if GROUP_LOG_PATH.exists():
        df = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    else:
        df = pd.DataFrame(columns=GROUP_LOG_COLUMNS)
    mask = (df["area_type"] == area_type) & (df["area_name"] == area_name) & (df["group_url"] == url)
    df = df[~mask]
    reason = "Manually recovered" + (f" — {note}" if note else "")
    new_row = pd.DataFrame([{
        "group": name, "group_url": url, "area_type": area_type, "area_name": area_name,
        "accepted": "Y", "stage": "recovered", "reason": reason,
    }])
    df = pd.concat([df, new_row], ignore_index=True).reindex(columns=GROUP_LOG_COLUMNS, fill_value="")
    df.to_csv(GROUP_LOG_PATH, index=False, encoding="utf-8", errors="surrogatepass")


def _append_override(area_type: str, area_name: str, url: str, note: str) -> None:
    row = {
        "group_url": url, "area_type": area_type, "area_name": area_name,
        "note": note, "recovered_at": datetime.now(timezone.utc).isoformat(),
    }
    if GROUP_OVERRIDES_PATH.exists():
        df = pd.read_csv(GROUP_OVERRIDES_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
        mask = (df["area_type"] == area_type) & (df["area_name"] == area_name) & (df["group_url"] == url)
        df = df[~mask]
    else:
        df = pd.DataFrame(columns=OVERRIDE_COLUMNS)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True).reindex(columns=OVERRIDE_COLUMNS, fill_value="")
    df.to_csv(GROUP_OVERRIDES_PATH, index=False, encoding="utf-8", errors="surrogatepass")


def recover(area_type: str, area_name: str, url: str, note: str = "") -> dict:
    if area_type == "constituency":
        pool = pipeline.reconstruct_constituency_candidates(area_name)
        ward_name = None
    elif area_type == "ward":
        ward_name, local_authority = _parse_ward_area_name(area_name)
        pool = pipeline_ward.reconstruct_ward_candidates(ward_name, local_authority)
    else:
        raise RecoveryError(f"Unknown area_type {area_type!r} (must be 'constituency' or 'ward')")

    match = pool[pool["url"] == url]
    if match.empty:
        raise RecoveryError(
            f"{url!r} not found in {area_name!r}'s current scraped candidate pool — "
            "cannot recover (the scrape may have changed since this group was last seen)"
        )
    row = match.iloc[0]
    name = str(row.get("name", ""))

    target = _target_file(area_type, area_name, ward_name)
    if target is None:
        raise RecoveryError(
            f"{area_name!r} has no staged (Clacton-etc/inputs/) or promoted "
            "(Clacton-etc/groups/) groups file yet — nothing to recover into"
        )

    existing = pd.read_csv(target, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    if url in set(existing["url"]):
        return {"ok": True, "already_present": True, "message": f"{name!r} is already present in {target}"}

    backup_path = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup_path)

    new_row = {col: row.get(col, "") for col in FINAL_COLUMNS}
    new_row["first_assessment"] = "Manually recovered"
    if area_type == "ward":
        # _aggregate_ward_groups never sets these (the real pipeline only
        # sets them once, blanket, right before writing) — every ward
        # group's locality is unconditionally "C" (no geo add-on for wards).
        new_row["locality"] = "C"
        new_row["locality_name"] = ""

    existing = pd.concat([existing, pd.DataFrame([new_row])], ignore_index=True)
    existing = existing.reindex(columns=FINAL_COLUMNS)
    existing.to_csv(target, index=False, encoding="utf-8", errors="surrogatepass")

    _update_group_log_row(area_type, area_name, url, name, note)
    _append_override(area_type, area_name, url, note)

    logger.info("Recovered %r into %s (backup at %s)", name, target, backup_path)
    return {"ok": True, "already_present": False, "message": f"Recovered {name!r} into {target}", "file": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="The group's Facebook URL, exactly as it appears in group_log.csv")
    parser.add_argument("--area-type", required=True, choices=["constituency", "ward"])
    parser.add_argument("--area-name", required=True, help="area_name exactly as it appears in group_log.csv (for a ward, includes the local authority in parens)")
    parser.add_argument("--note", default="", help="Optional note recorded in group_log.csv and group_overrides.csv")
    args = parser.parse_args()

    try:
        result = recover(args.area_type, args.area_name, args.url, args.note)
    except RecoveryError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info(result["message"])


if __name__ == "__main__":
    main()
