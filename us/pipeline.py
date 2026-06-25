#!/usr/bin/env python3
"""US pipeline: parse scraped Facebook groups for a congressional district,
aggregate to one row per group, assess relevance via AI, and write a final
filtered list.

Run from the repository root as a module:

    python -m us.pipeline
    python -m us.pipeline --district il-14 --district-name "Illinois 14th Congressional District"
    python -m us.pipeline --stop-before-ai-assessment

Inputs (under us/data/<district>/):
    <district>_places.csv      scraped groups keyed by place name
    <district>_localities.csv  scraped groups keyed by locality

Both must contain a ``groups`` column (a string-encoded list of group dicts).
"""

import argparse
import logging

import pandas as pd

from libby_core import assessment, descriptions
from libby_core.parse_groups import explode_groups
from us import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _explode_source(source_df: pd.DataFrame, source_type: str, source_column: str) -> pd.DataFrame:
    """Explode one scrape source (places or localities) into one row per group.

    Keeps a ``source_type`` / ``source_value`` pair recording where each group
    was found, and normalises the URL.
    """
    keep_cols = ["groups"]
    for col in ("count", "scroll"):
        if col in source_df.columns:
            keep_cols.append(col)

    src = source_df[keep_cols].copy()
    src["source_type"] = source_type
    src["source_value"] = source_df[source_column]

    exploded = explode_groups(src).drop(columns=["groups", "groups_list"], errors="ignore")
    exploded["url"] = exploded["url"].str.strip().str.rstrip("/") + "/"
    return exploded


def load_and_aggregate(district_id: str) -> pd.DataFrame:
    """Load the places + localities scrapes and aggregate to one row per group URL."""
    places = pd.read_csv(settings.places_path(district_id))
    localities = pd.read_csv(settings.localities_path(district_id))

    place_groups = _explode_source(places, "place", "name")
    loc_col = "locality" if "locality" in localities.columns else "name"
    location_groups = _explode_source(localities, "location", loc_col)

    all_groups = pd.concat([place_groups, location_groups], ignore_index=True)

    df = (
        all_groups.groupby("url", as_index=False)
        .agg(
            name=("name", "first"),
            details=("details", "first"),
            privacy=("privacy", "first"),
            member_count=("member_count", "max"),
            posts_per_day=("posts_per_day", "max"),
            source_types=("source_type", lambda s: sorted(set(s.dropna()))),
            source_values=("source_value", lambda s: sorted(set(s.dropna()))),
        )
        .sort_values(
            ["name", "url"],
            key=lambda col: col.str.lower() if col.dtype == "object" else col,
        )
        .reset_index(drop=True)
    )
    logger.info("Aggregated %d unique groups for %s", len(df), district_id)
    return df


def run(
    district_id: str = settings.DISTRICT_ID,
    district_name: str = settings.DISTRICT_NAME,
    stop_before_ai_assessment: bool = False,
) -> pd.DataFrame:
    df = load_and_aggregate(district_id)

    # Public groups only, mirroring the UK pipeline.
    df = df[df["privacy"] == "Public"].copy()
    logger.info("  %d public groups", len(df))

    if stop_before_ai_assessment:
        out = settings.OUTPUT_DIR / f"{district_id}_pre_assessment.csv"
        df.to_csv(out, index=False, encoding="utf-8", errors="surrogatepass")
        logger.info("Saved %d rows (pre-assessment) → %s", len(df), out)
        return df

    desc = descriptions.ensure_description(
        area_id=district_id,
        area_name=district_name,
        path=settings.DESCRIPTIONS_PATH,
        area_kind=settings.AREA_KIND,
        id_col="district_id",
        name_col="district_name",
    )
    if not desc:
        logger.warning("No description for %s — assessing with empty description", district_name)

    df["first_assessment"] = None
    if not df.empty:
        assessed = assessment.assess_groups(
            df=df,
            area_description=desc,
            area_kind=settings.AREA_KIND,
        )
        if "first_assessment" in assessed.columns:
            df["first_assessment"] = assessed["first_assessment"].values

    final_df = df[
        (df["first_assessment"] != "No") & (df["member_count"] > settings.MIN_MEMBERS)
    ].copy()
    final_df = final_df.sort_values("member_count", ascending=False, na_position="last")

    out = settings.output_path(district_id)
    final_df.to_csv(out, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("Saved %d rows → %s", len(final_df), out)
    return final_df


def main():
    parser = argparse.ArgumentParser(description="US Libby List pipeline")
    parser.add_argument("--district", default=settings.DISTRICT_ID, help="District id (folder under us/data/)")
    parser.add_argument("--district-name", default=settings.DISTRICT_NAME, help="Human-readable district name")
    parser.add_argument(
        "--stop-before-ai-assessment",
        action="store_true",
        help="Aggregate + filter to public, then write a pre-assessment CSV and exit.",
    )
    args = parser.parse_args()

    run(
        district_id=args.district,
        district_name=args.district_name,
        stop_before_ai_assessment=args.stop_before_ai_assessment,
    )


if __name__ == "__main__":
    main()
