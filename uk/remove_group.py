#!/usr/bin/env python3
"""Manually remove one group that was previously accepted — the reverse of
uk/recover_group.py.

No reconstruction needed here (removing a row, not adding one back):
locates whichever file currently holds this area's output —
Clacton-etc/groups/ if it's already been promoted, else Clacton-etc/inputs/
(or inputs/wards/) if it's still staged — removes the matching row (a .bak
backup of the file is written first), flips group_log.csv's row to
accepted="N", and records an "exclude" decision in group_overrides.csv so a
real future reprocess of this area (uk.pipeline --force, or any
uk.pipeline_ward run) doesn't silently re-add the group — see
uk.pipeline._load_overrides_by_area / _apply_exclusions.

Usage:
    python -m uk.remove_group --url <group url> --area-type constituency \\
        --area-name "Holborn and St Pancras" --note "not actually local"
    python -m uk.remove_group --url <group url> --area-type ward \\
        --area-name "Garden Suburb (Barnet)"
"""

import argparse
import logging
import shutil
import sys

import pandas as pd

from uk.pipeline import GROUP_LOG_COLUMNS
from uk.pipeline_ward import _parse_ward_area_name
from uk.recover_group import _append_override, _target_file
from uk.settings import GROUP_LOG_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


class RemovalError(Exception):
    """Raised for any expected failure (group/file not found, bad area) —
    meant to surface as a clean one-line error, not a stack trace."""


def _update_group_log_row_removed(area_type: str, area_name: str, url: str, name: str, note: str) -> None:
    """Targeted update — replaces just this one (area_type, area_name, url)
    row, same as uk.recover_group._update_group_log_row's identical
    reasoning (uk.pipeline._upsert_group_log's whole-area replace would
    wipe out every other group's log entry for this area)."""
    if GROUP_LOG_PATH.exists():
        df = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    else:
        df = pd.DataFrame(columns=GROUP_LOG_COLUMNS)
    mask = (df["area_type"] == area_type) & (df["area_name"] == area_name) & (df["group_url"] == url)
    df = df[~mask]
    reason = "Manually removed" + (f" — {note}" if note else "")
    new_row = pd.DataFrame([{
        "group": name, "group_url": url, "area_type": area_type, "area_name": area_name,
        "accepted": "N", "stage": "manually_excluded", "reason": reason,
    }])
    df = pd.concat([df, new_row], ignore_index=True).reindex(columns=GROUP_LOG_COLUMNS, fill_value="")
    df.to_csv(GROUP_LOG_PATH, index=False, encoding="utf-8", errors="surrogatepass")


def remove(area_type: str, area_name: str, url: str, note: str = "") -> dict:
    if area_type not in ("constituency", "ward"):
        raise RemovalError(f"Unknown area_type {area_type!r} (must be 'constituency' or 'ward')")

    ward_name = None
    if area_type == "ward":
        ward_name, _local_authority = _parse_ward_area_name(area_name)

    target = _target_file(area_type, area_name, ward_name)
    if target is None:
        raise RemovalError(
            f"{area_name!r} has no staged (Clacton-etc/inputs/) or promoted "
            "(Clacton-etc/groups/) groups file — nothing to remove"
        )

    existing = pd.read_csv(target, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    match = existing[existing["url"] == url]
    if match.empty:
        return {"ok": True, "already_absent": True, "message": f"{url!r} is not present in {target}"}
    name = str(match.iloc[0].get("name", ""))

    backup_path = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup_path)

    remaining = existing[existing["url"] != url]
    remaining.to_csv(target, index=False, encoding="utf-8", errors="surrogatepass")

    _update_group_log_row_removed(area_type, area_name, url, name, note)
    _append_override(area_type, area_name, url, note, decision="exclude")

    logger.info("Removed %r from %s (backup at %s)", name, target, backup_path)
    return {"ok": True, "already_absent": False, "message": f"Removed {name!r} from {target}", "file": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="The group's Facebook URL, exactly as it appears in group_log.csv")
    parser.add_argument("--area-type", required=True, choices=["constituency", "ward"])
    parser.add_argument("--area-name", required=True, help="area_name exactly as it appears in group_log.csv (for a ward, includes the local authority in parens)")
    parser.add_argument("--note", default="", help="Optional note recorded in group_log.csv and group_overrides.csv")
    args = parser.parse_args()

    try:
        result = remove(args.area_type, args.area_name, args.url, args.note)
    except RemovalError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info(result["message"])


if __name__ == "__main__":
    main()
