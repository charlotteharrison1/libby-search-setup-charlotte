#!/usr/bin/env python3
"""Rebuild blakenall_activity_viewer.html from blakenall_opinion_data/.

The whole opinion_data export is already filtered to political content, so
"political activity" here just means: how many posts, and how many comments
on those posts, came from each group. One row per distinct Facebook post is
built from comments/*.csv (post_id ties multiple comments back to the post
they're on); the viewer itself does all the group/type/category/date
rollups client-side from that flat row list, so re-running this script is
the only step needed after editing the data.

To relabel groups by type (news/discussion, civic org, noticeboard —
whatever taxonomy makes sense to you): edit
blakenall_opinion_data/group_types.csv (group,type — type is free text,
blank means "unlabeled") and re-run this script. No group_types.csv, or all
rows blank, is fine — the group-level chart doesn't depend on it, and the
type-rollup chart just shows an empty state until types are filled in.

Run from the repository root:
    python3 build_blakenall_activity_viewer.py
"""

import csv
import glob
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "blakenall_opinion_data"
COMMENTS_GLOB = str(DATA_DIR / "comments" / "*.csv")
TYPES_PATH = DATA_DIR / "group_types.csv"
HTML_PATH = HERE / "blakenall_activity_viewer.html"


def load_group_types() -> dict[str, str]:
    if not TYPES_PATH.exists():
        return {}
    with open(TYPES_PATH, newline="", encoding="utf-8") as f:
        return {row["group"]: (row.get("type") or "").strip() for row in csv.DictReader(f)}


def build_posts() -> list[dict]:
    """One row per distinct post_id, aggregated across its comments."""
    posts: dict[str, dict] = {}
    for path in sorted(glob.glob(COMMENTS_GLOB)):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pid = row["post_id"]
                if pid not in posts:
                    posts[pid] = {
                        "id": pid,
                        "group": row["group"],
                        "category": row["category"],
                        "issue": row["issue"],
                        "date": row["date"],
                        "link": row["link"].split("?")[0],
                        "post_summary": row.get("post_summary", "") or "",
                        "n_comments": 0,
                        "n_reactions": 0,
                    }
                p = posts[pid]
                p["date"] = min(p["date"], row["date"])
                p["n_comments"] += 1
                p["n_reactions"] += int(row["num_reactions"] or 0)
    return list(posts.values())


def main() -> None:
    if not DATA_DIR.exists():
        sys.exit(f"{DATA_DIR} not found — nothing to build from")

    types = load_group_types()
    posts = build_posts()
    if not posts:
        sys.exit(f"No posts found under {COMMENTS_GLOB}")

    rows = []
    for p in posts:
        rows.append({
            "group": html.escape(p["group"]),
            "type": html.escape(types.get(p["group"], "")),
            "category": html.escape(p["category"]),
            "issue": html.escape(p["issue"]),
            "date": p["date"],
            "comments": p["n_comments"],
            "reactions": p["n_reactions"],
            "link": html.escape(p["link"]),
            "summary": html.escape(p["post_summary"]),
        })
    rows.sort(key=lambda r: r["date"])

    tpl = HTML_PATH.read_text()
    import re
    out, n = re.subn(
        r"const POSTS = .*?;\n",
        "const POSTS = " + json.dumps(rows, ensure_ascii=False) + ";\n",
        tpl, count=1, flags=re.S,
    )
    if n != 1:
        sys.exit(f"Could not find the 'const POSTS = ...;' line in {HTML_PATH}")
    HTML_PATH.write_text(out)

    n_typed = sum(1 for g, t in types.items() if t)
    print(f"Rebuilt {HTML_PATH.name}: {len(rows)} posts across "
          f"{len({r['group'] for r in rows})} groups "
          f"({n_typed} group(s) typed in {TYPES_PATH.name})")


if __name__ == "__main__":
    main()
