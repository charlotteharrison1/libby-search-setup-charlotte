#!/usr/bin/env python3
"""Collect a topic's scraped groups: one row per unique group, NO AI filtering
(yet) and no rule-based dropping either — the point is to see exactly what the
raw search returns. Writes third_sector/output/raw_<topic>.csv and upserts the
third-sector group log (stage "collected", accepted blank = not yet judged).

    python -m third_sector.pipeline --topic uk_environmental_groups
"""

import argparse
import logging
import sys

import pandas as pd

from libby_core.parse_groups import explode_groups
from third_sector import config
from third_sector.settings import GROUP_LOG_PATH, OUTPUT_DIR, SCRAPED_DIR

logger = logging.getLogger(__name__)

GROUP_LOG_COLUMNS = ["group", "group_url", "area_type", "area_name", "accepted", "stage", "reason", "members", "posts_a_month"]


def upsert_group_log(topic_name: str, rows: list[dict]) -> None:
    """Replace this topic's rows, same semantics as uk.pipeline._upsert_group_log."""
    new = pd.DataFrame(rows, columns=GROUP_LOG_COLUMNS)
    if GROUP_LOG_PATH.exists():
        old = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
        new = pd.concat([old[old["area_name"] != topic_name], new], ignore_index=True)
    new.reindex(columns=GROUP_LOG_COLUMNS, fill_value="").to_csv(
        GROUP_LOG_PATH, index=False, encoding="utf-8", errors="surrogatepass"
    )


def run(slug: str):
    topic = config.get_topic(slug)
    path = SCRAPED_DIR / f"{slug}_search_targets.csv"
    if not path.exists():
        logger.error("No scraped file at %s — pull it first.", path)
        sys.exit(1)

    df = pd.read_csv(path, dtype={"groups": str})
    exploded = explode_groups(df).drop(columns=["groups", "groups_list"], errors="ignore")
    if exploded.empty:
        logger.error("No groups in %s (scrape not finished?)", path)
        sys.exit(1)

    exploded["url"] = exploded["url"].astype(str).str.strip().str.rstrip("/") + "/"
    # keep which search terms found each group, then one row per group
    found_by = exploded.groupby("url")["search_string"].agg(lambda s: "; ".join(sorted(set(s)))).rename("found_by")
    groups = exploded.drop_duplicates(subset=["url"]).merge(found_by, on="url").drop(columns=["search_string"])
    groups["n_searches"] = groups["found_by"].str.count(";") + 1
    groups = groups.sort_values("member_count", ascending=False, na_position="last")

    out = OUTPUT_DIR / f"raw_{slug}.csv"
    groups.to_csv(out, index=False, encoding="utf-8", errors="surrogatepass")

    def _num(v):
        return "" if pd.isna(v) else str(int(v))

    upsert_group_log(topic["name"], [
        {
            "group": r.get("name", ""), "group_url": r["url"], "area_type": "topic", "area_name": topic["name"],
            "accepted": "", "stage": "collected", "reason": f"Found by: {r['found_by']}",
            "members": _num(r.get("member_count")),
            "posts_a_month": _num(r["posts_per_day"] * 30) if pd.notna(r.get("posts_per_day")) else "",
        }
        for r in groups.to_dict("records")
    ])
    logger.info("%d unique groups from %d rows → %s (log: %s)", len(groups), len(exploded), out, GROUP_LOG_PATH)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--topic", required=True, help="Topic slug (see prompts.json)")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(p.parse_args().topic)


if __name__ == "__main__":
    main()
