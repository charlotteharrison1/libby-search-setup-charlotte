#!/usr/bin/env python3
"""Build a small, targeted queue of groups worth About-scraping next: every
group still assessed "Unsure" in a finished run, that doesn't already have
About-context available.

This is the "escalate on uncertainty" half of --context (see uk/pipeline.py
/ uk/pipeline_ward.py and uk/about_context.py): rather than About-scraping
every group in an area (slow, and mostly wasted on groups that were never
ambiguous), scrape only the specific groups that came back Unsure without
context — a much smaller, high-value set — then feed the result back into
the same global about_pages cache every area already draws on.

    python -m uk.queue_unsure_for_about

Output: uk/output/unsure_queue.csv (see uk.settings.UNSURE_QUEUE_PATH) — a
groups_file in exactly the shape libby_download's scrape_group_about.py
already expects (a `groups` column of JSON [{url, name, details}, ...]), so
it can be pointed at directly with no changes to that script. Extra columns
(url, name, members, posts_a_month, source_areas) are for YOUR review before
you push it — scrape_group_about.py only ever reads `groups`.

This script only reads existing output files and the about_pages cache; it
never scrapes or calls an LLM itself. Deliberately no automation past this
point — see the module docstring's "how it fits together" note in the repo
README for the manual steps (push, scrape, pull back) that come after.
"""

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from uk import about_context
from uk.settings import ABOUT_PAGES_DIR, OUTPUT_DIR, UNSURE_QUEUE_PATH, WARD_OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

QUEUE_COLUMNS = ["url", "name", "members", "posts_a_month", "source_areas", "groups"]


def _area_name_from_path(path: Path) -> str:
    """'groups_Garden Suburb.csv' -> 'Garden Suburb'. Filename, not an
    internal column, is the reliable source of an area's display name here:
    a ward's own groups_<ward>.csv carries its parent constituency's name in
    PCON24NM (see uk/pipeline_ward.py's FINAL_COLUMNS), not the ward's own —
    the filename is the one place both constituency and ward outputs agree."""
    stem = path.stem
    return stem[len("groups_"):] if stem.startswith("groups_") else stem


def _unsure_rows(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
    except Exception as e:
        logger.warning("Could not read %s (%s) — skipping", path, e)
        return pd.DataFrame()
    if "first_assessment" not in df.columns or "url" not in df.columns:
        return pd.DataFrame()
    is_unsure = df["first_assessment"].astype(str).str.strip().str.casefold() == "unsure"
    return df[is_unsure]


def build_queue(about_lookup: dict[str, str] | None = None) -> pd.DataFrame:
    """Scan every groups_*.csv under uk/output/ and uk/output/wards/ for
    still-"Unsure" groups, drop any whose URL is already in the global
    about-context cache (re-scraping wouldn't change an already-informed
    "Unsure" verdict), dedupe by URL, and return one row per group ready to
    write as a groups_file for scrape_group_about.py."""
    if about_lookup is None:
        about_lookup = about_context.load_global_about_context(ABOUT_PAGES_DIR)

    sources = sorted(OUTPUT_DIR.glob("groups_*.csv")) + sorted(WARD_OUTPUT_DIR.glob("groups_*.csv"))

    groups: dict[str, dict] = {}
    n_unsure_seen = 0
    n_already_cached = 0
    for path in sources:
        area_name = _area_name_from_path(path)
        unsure = _unsure_rows(path)
        for _, row in unsure.iterrows():
            url = row.get("url")
            if not isinstance(url, str) or not url:
                continue
            n_unsure_seen += 1
            if url in about_lookup:
                n_already_cached += 1
                continue
            entry = groups.setdefault(url, {
                "url": url,
                "name": row.get("name", ""),
                "members": row.get("members"),
                "posts_a_month": row.get("posts_a_month"),
                "source_areas": set(),
            })
            entry["source_areas"].add(area_name)

    logger.info(
        "Scanned %d file(s): %d Unsure row(s) seen, %d already have About-context "
        "(skipped), %d unique group(s) queued",
        len(sources), n_unsure_seen, n_already_cached, len(groups),
    )

    rows = []
    for url, g in groups.items():
        members = g["members"]
        posts = g["posts_a_month"]
        details = "Public"
        if pd.notna(members):
            details += f" · {int(members)} members"
        if pd.notna(posts):
            details += f" · {int(posts)} posts a month"
        rows.append({
            "url": url,
            "name": g["name"],
            "members": members,
            "posts_a_month": posts,
            "source_areas": "; ".join(sorted(g["source_areas"])),
            "groups": json.dumps([{"url": url, "name": g["name"], "details": details}]),
        })

    queue = pd.DataFrame(rows, columns=QUEUE_COLUMNS)
    if not queue.empty:
        queue = queue.sort_values("members", ascending=False, na_position="last")
    return queue


def main():
    parser = argparse.ArgumentParser(
        description="Build a groups_file of Unsure, not-yet-About-scraped groups for scrape_group_about.py"
    )
    parser.add_argument("--output", default=None, help=f"Output CSV path (default: {UNSURE_QUEUE_PATH})")
    args = parser.parse_args()

    queue = build_queue()
    out_path = Path(args.output) if args.output else UNSURE_QUEUE_PATH
    queue.to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("Wrote %d group(s) → %s", len(queue), out_path)

    if not queue.empty:
        preview = queue.head(20)[["name", "members", "source_areas"]]
        logger.info("Top by members:\n%s", preview.to_string(index=False))


if __name__ == "__main__":
    main()
