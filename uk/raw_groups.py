#!/usr/bin/env python3
"""One-off: dump the raw groups out of a scraped search-targets CSV.

No constituency aggregation, no geo add-on, no AI assessment, no public/buy-
sell/member-count filtering — just explode(groups) into one row per group,
using the same parser libby_core/parse_groups.py already provides for the US
pipeline. Rows that haven't been scraped yet (empty groups) are dropped;
everything else passes through untouched.

    python -m uk.raw_groups --input uk/data/scraped/adhoc_wards_search_targets.csv
"""

import argparse
from pathlib import Path

import pandas as pd

from libby_core.parse_groups import explode_groups


def run(input_path: Path, output_path: Path) -> pd.DataFrame:
    df = pd.read_csv(input_path)
    exploded = explode_groups(df)
    exploded = exploded.drop(columns=["groups", "groups_list"], errors="ignore")

    if "url" in exploded.columns:
        exploded["url"] = exploded["url"].astype(str).str.strip().str.rstrip("/") + "/"
        exploded = exploded.drop_duplicates(subset=["url"])

    exploded.to_csv(output_path, index=False, encoding="utf-8", errors="surrogatepass")
    print(f"Wrote {len(exploded)} raw groups → {output_path}")
    return exploded


def main():
    parser = argparse.ArgumentParser(description="Dump raw, unfiltered groups from a scraped search-targets CSV")
    parser.add_argument("--input", required=True, help="Scraped CSV with a 'groups' column")
    parser.add_argument("--output", default=None, help="Output CSV path (default: <input-name>_raw_groups.csv, next to the input)")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_name(input_path.stem + "_raw_groups.csv")
    run(input_path, output_path)


if __name__ == "__main__":
    main()
