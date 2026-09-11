#!/usr/bin/env python3
"""Rebuild blakenall_sources_viewer.html from blakenall_sources.csv.

The viewer is a self-contained HTML page with the CSV's rows baked in as a
JSON constant — it never reads the CSV at view time. To add/remove/edit
groups: change blakenall_sources.csv, run this script, reopen the page.
Everything on the page (tiles, donut, bars, chips, table, counts) is
recomputed from the embedded data on load.

CSV columns expected (same as the file this was built from):
    name, url, members, posts_a_month, found_in, found_by, tags, sources
- found_in: '; '-separated. For the donut's ward/constituency split to
  classify a row, use these exact labels (or 'not in any scrape' / blank):
      Harden, Goscote & Ryecroft (ward)
      Bloxwich East & Blakenall Heath (ward)
      Walsall and Bloxwich (constituency)
- tags / sources: '; '-separated, free text.

Run from the repository root:
    python3 build_blakenall_viewer.py
"""

import html
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
CSV_PATH = HERE / "blakenall_sources.csv"
HTML_PATH = HERE / "blakenall_sources_viewer.html"


def clean_name(name: str, url: str) -> str:
    """Strip encoding-damaged runs (mangled surrogates from the scrape),
    keep readable latin text; fall back to the group id when nothing
    readable survives."""
    kept = re.sub(r"[^\x20-\x7E£&'’]", "", str(name))
    kept = re.sub(r"\s{2,}", " ", kept).strip(" -,&")
    if len(kept) < 3:
        gid = str(url).rstrip("/").rsplit("/", 1)[-1]
        return f"(name unreadable — group {gid})"
    return kept


def split(v) -> list[str]:
    return [s.strip() for s in str(v).split(";") if s.strip()]


def main() -> None:
    df = pd.read_csv(CSV_PATH).fillna("")
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "name": html.escape(clean_name(r["name"], r["url"])),
            "url": html.escape(str(r["url"])),
            "members": int(float(r["members"] or 0)),
            "posts": int(float(r["posts_a_month"])) if r["posts_a_month"] != "" else 0,
            "found_in": [] if r["found_in"] == "not in any scrape" else split(r["found_in"]),
            "tags": split(r["tags"]),
            "sources": split(r["sources"]),
        })

    tpl = HTML_PATH.read_text()
    out, n = re.subn(
        r"const DATA = .*?;\n",
        "const DATA = " + json.dumps(rows, ensure_ascii=False) + ";\n",
        tpl, count=1, flags=re.S,
    )
    if n != 1:
        sys.exit(f"Could not find the 'const DATA = ...;' line in {HTML_PATH}")
    HTML_PATH.write_text(out)
    print(f"Rebuilt {HTML_PATH.name} with {len(rows)} groups from {CSV_PATH.name}")


if __name__ == "__main__":
    main()
