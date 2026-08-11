#!/usr/bin/env python3
"""Ward-level pipeline: parse a scraped ad-hoc ward search-targets CSV,
aggregate to one row per group, AI-assess relevance, write a filtered output.

Companion to uk/pipeline.py, but for wards instead of constituencies —
deliberately NOT a thin wrapper around it, because two of its assumptions
don't hold at ward scale:

- The distance-based geographic add-on (uk/geo.py) is calibrated for
  constituency-sized areas (~100km², 15km "local" radius, 500-3000m add-on
  buffer). A ward is often under 2km across, so "borrow groups from a
  neighbouring area within N km" doesn't translate — this pipeline skips the
  add-on entirely and only looks at groups found directly in the ward's own
  scrape, same shape as us/pipeline.py.
- uk/pipeline.py skips a constituency entirely if its Intermediate/<code>.csv
  already exists, assuming a prior full run. That caching model doesn't fit a
  small, rerun-as-you-go ad-hoc tool, so this script has none: every run
  reprocesses everything fresh.

Input rows are expected to come from uk/generate_search_ward.py, which
encodes "<ward name> | <local authority>" into the search-targets CSV's
existing 'county' column (there's no dedicated ward column — the output
schema deliberately matches uk/generate_search.py's exactly). This script
splits that back out to group and label results per ward. The delimiter is
" | ", not a comma — ward names can themselves contain a comma (e.g. "Harden,
Goscote & Ryecroft"), which a comma-based split would cut in the wrong place.

Run from the repository root as a module:

    python -m uk.pipeline_ward --input uk/data/scraped/wards/adhoc_wards_search_targets.csv
    python -m uk.pipeline_ward --input ... --stop-before-ai-assessment
"""

import argparse
import logging
import re
import sys
from pathlib import Path

import pandas as pd

from libby_core import assessment, descriptions
from uk import data_loading, parsing, ward_geodata
from uk.generate_search import slugify
from uk.settings import WARD_DESCRIPTIONS_PATH, WARD_OUTPUT_DIR, WARD_SCRAPED_DIR

AREA_KIND = "UK electoral ward"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Same quality bar as uk/pipeline.py, but lower — a genuinely relevant
# hyperlocal ward group ("Cricklewood Residents") will naturally have far
# fewer members than a constituency-wide one. Tune with --min-members /
# --min-posts-a-month if these still feel wrong for your wards.
DEFAULT_MIN_MEMBERS = 10
DEFAULT_MIN_POSTS_A_MONTH = 5

_BUY_SELL_PATTERN = re.compile(
    r"\b(buy|sell|selling|sold|sale|for sale|marketplace|wanted|swap|swapping|"
    r"freebie|freebies|free stuff|preloved|pre-loved|second.?hand|bargain)\b",
    re.IGNORECASE,
)


def _parse_details_on_exploded(df: pd.DataFrame) -> pd.DataFrame:
    cols_to_drop = [c for c in ("public_y_n", "members", "posts_a_month") if c in df.columns]
    df = df.drop(columns=cols_to_drop)
    parsed = df["details"].apply(parsing.parse_details)
    df = pd.concat([df, parsed], axis=1)
    df["public_y_n"] = df["public_y_n"].astype("boolean").fillna(False)
    df["members"] = df["members"].astype("Int64")
    df["posts_a_month"] = df["posts_a_month"].astype("Float64").fillna(0)
    return df


def _drop_buy_sell(df: pd.DataFrame, name_col: str = "name") -> pd.DataFrame:
    mask = df[name_col].astype(str).str.contains(_BUY_SELL_PATTERN, na=False)
    dropped = int(mask.sum())
    if dropped:
        logger.info("  Dropped %d buy/sell groups", dropped)
    return df[~mask].copy()


def _aggregate_ward_groups(df_exploded: pd.DataFrame, ward_id: str) -> pd.DataFrame:
    """Aggregate exploded rows for one ward into one row per group URL."""
    coi = df_exploded[df_exploded["ward_id"] == ward_id].copy()
    agg = (
        coi.groupby("url", dropna=False)
        .apply(
            lambda g: pd.Series({
                "name": g["name"].iloc[0] if "name" in g.columns else "",
                "url": g.name,
                "public_y_n": bool(pd.to_numeric(g["public_y_n"], errors="coerce").fillna(0).max()),
                "members": pd.to_numeric(g["members"], errors="coerce").max(),
                "posts_a_month": pd.to_numeric(g["posts_a_month"], errors="coerce").max(),
            }),
            include_groups=False,
        )
        .reset_index(drop=True)
    )
    return agg


def run(
    input_path: Path,
    stop_before_ai_assessment: bool = False,
    min_members: int = DEFAULT_MIN_MEMBERS,
    min_posts_a_month: float = DEFAULT_MIN_POSTS_A_MONTH,
) -> pd.DataFrame:
    logger.info("Loading: %s", input_path)
    df_exploded = data_loading.load_new_scrape(input_path)
    if df_exploded.empty:
        logger.error("No processed/scraped rows found in %s", input_path)
        sys.exit(1)

    df_exploded = _parse_details_on_exploded(df_exploded)

    split = df_exploded["county"].apply(ward_geodata.split_county)
    df_exploded["ward_name"] = split.apply(lambda t: t[0])
    df_exploded["local_authority"] = split.apply(lambda t: t[1])
    df_exploded["ward_id"] = df_exploded.apply(
        lambda r: slugify(f"{r['ward_name']}_{r['local_authority']}"), axis=1
    )

    ward_list = df_exploded[["ward_id", "ward_name", "local_authority"]].drop_duplicates()
    logger.info("Found %d ward(s) in input", len(ward_list))

    all_final = []
    for _, w in ward_list.iterrows():
        ward_id, ward_name, local_authority = w["ward_id"], w["ward_name"], w["local_authority"]
        logger.info("Processing: %s (%s)", ward_name, local_authority or "no local authority given")

        agg = _aggregate_ward_groups(df_exploded, ward_id)
        logger.info("  %d aggregated groups", len(agg))

        combined = agg[agg.public_y_n == True].copy()  # noqa: E712
        combined = _drop_buy_sell(combined)
        combined = combined[
            combined["posts_a_month"].isna() | (combined["posts_a_month"] >= min_posts_a_month)
        ].copy()

        out_name = f"ward_groups_{slugify(ward_name)}"
        if stop_before_ai_assessment:
            out_path = WARD_OUTPUT_DIR / f"{out_name}-intermediate.csv"
            combined.to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
            logger.info("  Saved %d rows (pre-assessment) → %s", len(combined), out_path)
            continue

        if combined.empty:
            logger.info("  Nothing left to assess for %s", ward_name)
            continue

        desc = descriptions.ensure_description(
            area_id=ward_id,
            area_name=f"{ward_name}, {local_authority}" if local_authority else ward_name,
            path=WARD_DESCRIPTIONS_PATH,
            area_kind=AREA_KIND,
        )
        if not desc:
            logger.warning("No description for %s — using empty string for assessment", ward_name)

        assessed = assessment.assess_groups(df=combined, area_description=desc, area_kind=AREA_KIND)
        combined["first_assessment"] = assessed.get("first_assessment")

        final = combined[
            (combined["first_assessment"] != "No") & (combined["members"].fillna(0) >= min_members)
        ].copy()
        final = final.sort_values(by=["members", "posts_a_month"], ascending=False, na_position="last")
        final["ward_name"] = ward_name
        final["local_authority"] = local_authority

        out_path = WARD_OUTPUT_DIR / f"{out_name}.csv"
        final.to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
        logger.info("  Saved %d rows → %s", len(final), out_path)
        all_final.append(final)

    if stop_before_ai_assessment or not all_final:
        return pd.DataFrame()

    combined_out = pd.concat(all_final, axis=0, ignore_index=True).drop_duplicates(subset=["url"])
    combined_path = WARD_OUTPUT_DIR / "ward_output.csv"
    combined_out.to_csv(combined_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("Saved %d total rows → %s", len(combined_out), combined_path)
    return combined_out


def main():
    parser = argparse.ArgumentParser(description="Process a scraped ad-hoc ward CSV into a filtered, AI-assessed group list")
    default_input = WARD_SCRAPED_DIR / "adhoc_wards_search_targets.csv"
    parser.add_argument("--input", default=str(default_input), help=f"Scraped search-targets CSV (from generate_search_ward.py, after sync_scrape.sh pull) (default: {default_input})")
    parser.add_argument("--stop-before-ai-assessment", action="store_true", help="Write pre-assessment intermediate CSVs per ward and exit before AI assessment")
    parser.add_argument("--min-members", type=int, default=DEFAULT_MIN_MEMBERS, help=f"Minimum member count to keep a group (default: {DEFAULT_MIN_MEMBERS})")
    parser.add_argument("--min-posts-a-month", type=float, default=DEFAULT_MIN_POSTS_A_MONTH, help=f"Minimum posts/month to keep a group (default: {DEFAULT_MIN_POSTS_A_MONTH})")
    args = parser.parse_args()

    run(
        input_path=Path(args.input),
        stop_before_ai_assessment=args.stop_before_ai_assessment,
        min_members=args.min_members,
        min_posts_a_month=args.min_posts_a_month,
    )


if __name__ == "__main__":
    main()
