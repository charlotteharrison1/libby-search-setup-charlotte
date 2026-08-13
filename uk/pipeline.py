#!/usr/bin/env python3
"""Main pipeline: combine scrape data, compute geographic add-ons, assess
groups via AI, and produce a final filtered output.

Run from the repository root as a module:

Full run (all constituencies):
    python -m uk.pipeline

Test run (single constituency by name):
    python -m uk.pipeline --constituency "Sittingbourne and Sheppey"
"""

import argparse
import ast
import logging
import re
import sys
from pathlib import Path

import pandas as pd

from libby_core import assessment, descriptions
from uk import data_loading, geo, parsing
from uk.settings import (
    DESCRIPTIONS_PATH,
    DENSITIES_PATH,
    GEOJSON_PATH,
    INTERMEDIATE_DIR,
    NEW_SCRAPE_PATH,
    OUTPUT_DIR,
    PCON_MAPPING_PATH,
    PREVIOUS_SCRAPE_PATH,
    REDO_GROUPS_PATH,
)

# Area metadata used by the shared description + assessment engines.
AREA_KIND = "UK parliament constituency"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

GLOBAL_MIN_ADD_ON = 500
GLOBAL_MAX_ADD_ON = 3000


# ── helpers ────────────────────────────────────────────────────────────────


def _count_groups(groups_series: pd.Series) -> pd.Series:
    """Parse `groups` column (JSON/list of dicts) and return count per row.
    Missing/empty/invalid values yield 0."""
    def parse(x):
        if pd.isna(x) or (isinstance(x, str) and not x.strip()):
            return 0
        try:
            val = ast.literal_eval(x)
            return len(val) if isinstance(val, list) else 0
        except (ValueError, SyntaxError):
            return 0
    return groups_series.apply(parse)


def _merge_redo_groups_into_master() -> None:
    """If redo_groups.csv exists, load it and the master file; for rows matched
    by (FID, place_name) where redo has more groups than master, overwrite
    master with redo. Save the updated master back to disk."""
    if not REDO_GROUPS_PATH.exists():
        logger.info("No redo groups file at %s — skipping merge", REDO_GROUPS_PATH)
        return

    master = pd.read_csv(NEW_SCRAPE_PATH)
    redo = pd.read_csv(REDO_GROUPS_PATH)

    if "groups" not in master.columns or "groups" not in redo.columns:
        logger.warning("Missing 'groups' column in master or redo — skipping merge")
        return

    master["_key"] = master["FID"].astype(str) + "\0" + master["place_name"].astype(str)
    redo["_key"] = redo["FID"].astype(str) + "\0" + redo["place_name"].astype(str)
    master["_n_groups"] = _count_groups(master["groups"])
    redo["_n_groups"] = _count_groups(redo["groups"])

    master_by_key = master.set_index("_key")
    redo_by_key = redo.set_index("_key")
    keys_to_update = [
        k for k in redo_by_key.index
        if k in master_by_key.index
        and redo_by_key.at[k, "_n_groups"] > master_by_key.at[k, "_n_groups"]
    ]

    for k in keys_to_update:
        redo_row = redo_by_key.loc[k].drop(["_key", "_n_groups"], errors="ignore")
        master_by_key.loc[k] = redo_row

    master_out = master_by_key.reset_index()
    master_out = master_out.drop(columns=["_key", "_n_groups"], errors="ignore")
    master_out.to_csv(NEW_SCRAPE_PATH, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("Merged redo groups into master: %d rows updated → %s", len(keys_to_update), NEW_SCRAPE_PATH)


def _parse_details_on_exploded(df: pd.DataFrame) -> pd.DataFrame:
    """Apply detail parsing (members / posts / public) to the exploded
    new-scrape DataFrame."""
    cols_to_drop = [c for c in ("public_y_n", "members", "posts_a_month") if c in df.columns]
    df = df.drop(columns=cols_to_drop)

    parsed = df["details"].apply(parsing.parse_details)
    df = pd.concat([df, parsed], axis=1)

    df["public_y_n"] = df["public_y_n"].astype("boolean").fillna(False)
    df["members"] = df["members"].astype("Int64")
    df["posts_a_month"] = df["posts_a_month"].astype("Float64").fillna(0)
    return df


def _aggregate_new_groups(df_exploded: pd.DataFrame, pcon_codes: set[str]) -> pd.DataFrame:
    """Aggregate the exploded new-scrape rows into one row per group per
    constituency."""
    coi = df_exploded[df_exploded.PCON24CD.isin(pcon_codes)].copy()

    agg = (
        coi.groupby("url", dropna=False)
        .apply(
            lambda g: pd.Series({
                "PCON24CD": (
                    g["PCON24CD"].mode().iloc[0]
                    if not g["PCON24CD"].mode().empty
                    else g["PCON24CD"].iloc[0]
                ),
                "name": g["name"].iloc[0] if "name" in g.columns else "",
                "url": g.name,
                "public_y_n": bool(pd.to_numeric(g["public_y_n"], errors="coerce").fillna(0).max()),
                "members": pd.to_numeric(g["members"], errors="coerce").max(),
                "posts_a_month": pd.to_numeric(g["posts_a_month"], errors="coerce").max(),
                "locality": "C",
                "locality_name": "",
                # Same overlap-honest set semantics as pipeline_ward.py: a
                # group found by two target types counts under both. Guarded —
                # older master files predate these columns — and "" (untagged
                # rows) is excluded. Logged per constituency, then dropped by
                # _combine_and_filter's keep_cols, so outputs are unchanged.
                "target_types": sorted(set(g["target_type"].dropna()) - {""}) if "target_type" in g.columns else [],
                "target_sources": sorted(set(g["target_source"].dropna()) - {""}) if "target_source" in g.columns else [],
            }),
            include_groups=False,
        )
        .reset_index(drop=True)
    )
    return agg


def _combine_and_filter(
    new_groups: pd.DataFrame,
    addon_groups: pd.DataFrame,
    pcon_map_df: pd.DataFrame,
) -> pd.DataFrame:
    """Combine new-scrape groups with add-on groups, filter to public, dedupe,
    and merge constituency names."""
    keep_cols = ["PCON24CD", "name", "url", "public_y_n", "members", "posts_a_month", "locality", "locality_name"]

    parts = [new_groups[[c for c in keep_cols if c in new_groups.columns]]]
    if not addon_groups.empty:
        parts.append(addon_groups[[c for c in keep_cols if c in addon_groups.columns]])

    combined = pd.concat(parts, axis=0, ignore_index=True)

    # Public only
    combined = combined[combined.public_y_n == True].copy()  # noqa: E712

    combined = combined.sort_values(
        by=["PCON24CD", "members", "posts_a_month"], ascending=False
    )

    # Merge PCON24NM
    pcon_unique = pcon_map_df.drop_duplicates(subset=["PCON24CD"])
    combined = combined.merge(
        pcon_unique[["PCON24CD", "PCON24NM"]], on="PCON24CD", how="left"
    )

    combined = combined.drop_duplicates(subset=["url"])
    combined = combined[combined["locality"] != "X"]
    return combined


_BUY_SELL_PATTERN = re.compile(
    r"\b(buy|sell|selling|sold|sale|for sale|marketplace|wanted|swap|swapping|"
    r"freebie|freebies|free stuff|preloved|pre-loved|second.?hand|bargain)\b",
    re.IGNORECASE,
)


def _targeting_breakdown(pcon24nm: str, final: pd.DataFrame, new_groups: pd.DataFrame) -> pd.DataFrame:
    """Per-group targeting report (same shape as pipeline_ward.py's
    targeting_breakdown_{ward}.csv): one row per final surviving group, with
    the target types/sources that found it '; '-joined. The tag sets live on
    the aggregated new-scrape groups (they're dropped by _combine_and_filter's
    keep_cols), so they're merged back onto the final groups by url here. Geo
    add-on groups were never searched for directly and get blank tags — the
    locality column (A/L/R vs C) already identifies them. Also logs a count
    rollup. Returns an empty frame — write nothing — when no group carries a
    tag (master files predating target_type/target_source)."""
    if "target_types" not in new_groups.columns or final.empty:
        return pd.DataFrame()

    merged = final.merge(new_groups[["url", "target_types", "target_sources"]], on="url", how="left")
    for col in ("target_types", "target_sources"):
        merged[col] = merged[col].apply(lambda v: v if isinstance(v, list) else [])
    if merged["target_types"].str.len().sum() == 0 and merged["target_sources"].str.len().sum() == 0:
        return pd.DataFrame()

    rows = []
    for col, label in (("target_types", "type"), ("target_sources", "source")):
        counts = merged[col].explode().dropna().value_counts()
        for value, count in counts.items():
            rows.append((label, value, int(count)))
    if rows:
        logger.info("  Targeting breakdown for %s (%d final groups; overlaps allowed):", pcon24nm, len(merged))
        for label, value, count in sorted(rows, key=lambda r: (r[0], -r[2])):
            logger.info("    %-6s %-18s %d", label, value, count)

    keep = [c for c in ("name", "url", "members", "posts_a_month", "locality") if c in merged.columns]
    table = merged[keep].copy()
    for col in ("target_types", "target_sources"):
        table[col] = merged[col].apply("; ".join)
    return table.sort_values("members", ascending=False, na_position="last")


def _drop_buy_sell(df: pd.DataFrame, name_col: str = "name") -> pd.DataFrame:
    mask = df[name_col].astype(str).str.contains(_BUY_SELL_PATTERN, na=False)
    dropped = int(mask.sum())
    if dropped:
        logger.info("  Dropped %d buy/sell groups", dropped)
    return df[~mask].copy()


# Church-keyword groups the assessor rated "Unsure" are overwhelmingly
# Facebook keyword-match artifacts: any search string containing "Church"
# (e.g. a "Church End" locality) pulls in huge unrelated church groups,
# which then survive because the final filter keeps everything != "No".
# Name-based heuristic, same mechanism as _BUY_SELL_PATTERN — mirrored in
# uk/pipeline_ward.py.
_CHURCH_PATTERN = re.compile(
    r"\b(?:church|chapel|ministry|ministries|parish|cathedral|catholic|"
    r"gospel|worship|prayer|christian)\b",
    re.IGNORECASE,
)


def _drop_unsure_churches(df: pd.DataFrame, area_name: str, name_col: str = "name") -> pd.DataFrame:
    """Drop groups assessed "Unsure" whose name matches _CHURCH_PATTERN —
    but spare any that mention a distinctive word of the area's own name
    ("Finchley Baptist Church" stays for Finchley and Golders Green, an
    unrelated megachurch goes). "Yes"-assessed church groups are never
    touched. Every dropped name is logged so the heuristic can be audited."""
    if "first_assessment" not in df.columns or df.empty:
        return df
    # Distinctive area words: 4+ letters and not themselves church words.
    area_tokens = {
        t for t in re.findall(r"[a-z]+", str(area_name).casefold())
        if len(t) >= 4 and not _CHURCH_PATTERN.search(t)
    }
    names = df[name_col].astype(str)
    unsure = df["first_assessment"].astype(str).str.strip().str.casefold() == "unsure"
    churchy = names.str.contains(_CHURCH_PATTERN, na=False)
    mentions_area = names.apply(lambda n: any(t in n.casefold() for t in area_tokens))
    mask = unsure & churchy & ~mentions_area
    if mask.any():
        logger.info("  Dropped %d Unsure church-name group(s):", int(mask.sum()))
        for n in names[mask]:
            logger.info("    %s", n)
    return df[~mask].copy()


# ── main ───────────────────────────────────────────────────────────────────

def run(
    constituency_name: str | None = None,
    stop_before_ai_assessment: bool = False,
    input_path: Path | None = None,
):
    # ── Phase 1: One-time setup ─────────────────────────────────────────────
    if input_path:
        logger.info("Using input file: %s", input_path)
    else:
        _merge_redo_groups_into_master()
    logger.info("Loading data …")
    df_new_exploded = data_loading.load_new_scrape(input_path or NEW_SCRAPE_PATH)

    # PCON mapping — fall back to scrape data if file is missing
    if PCON_MAPPING_PATH.exists():
        pcon_map_df = data_loading.load_pcon_mapping()
    else:
        logger.info("PCON mapping file not found — deriving from scrape data")
        pcon_map_df = df_new_exploded[["PCON24CD", "PCON24NM"]].drop_duplicates().reset_index(drop=True)

    # Geo add-on files — all optional; skip the add-on if any are missing
    geo_available = GEOJSON_PATH.exists() and DENSITIES_PATH.exists() and PREVIOUS_SCRAPE_PATH.exists()
    if geo_available:
        df_previous = data_loading.load_previous_scrape()
        densities_df = data_loading.load_densities()
        gdf_pcon = data_loading.load_constituency_boundaries()
    else:
        logger.info("Geo add-on files not found — skipping geographic add-on")
        df_previous = pd.DataFrame()
        densities_df = None
        gdf_pcon = None

    all_codes = set(df_new_exploded["PCON24CD"].dropna().unique())

    if constituency_name:
        match = pcon_map_df[pcon_map_df["PCON24NM"] == constituency_name]
        if match.empty:
            logger.error("Constituency '%s' not found in PCON mapping", constituency_name)
            sys.exit(1)
        pcon_codes = set(match["PCON24CD"].unique())
        logger.info("Test run: %s → %s", constituency_name, pcon_codes)
    else:
        pcon_codes = all_codes
        logger.info("Full run: %d constituencies", len(pcon_codes))

    logger.info("Parsing group details …")
    df_new_exploded = _parse_details_on_exploded(df_new_exploded)

    logger.info("Aggregating new-scrape groups …")
    new_groups = _aggregate_new_groups(df_new_exploded, pcon_codes)
    logger.info("  %d aggregated new-scrape groups", len(new_groups))

    # List of (pcon24cd, pcon24nm) to process
    constituency_list = (
        pcon_map_df[pcon_map_df["PCON24CD"].isin(pcon_codes)]
        [["PCON24CD", "PCON24NM"]]
        .drop_duplicates()
        .sort_values("PCON24NM")
    )

    # ── Phase 2: Per-constituency loop (run-once, skip if done) ─────────────
    for _, row in constituency_list.iterrows():
        pcon24cd = row["PCON24CD"]
        pcon24nm = row["PCON24NM"]
        intermediate_path = INTERMEDIATE_DIR / f"{pcon24cd}.csv"

        if not stop_before_ai_assessment and intermediate_path.exists():
            logger.info("Skipping (already done): %s", pcon24nm)
            continue

        logger.info("Processing: %s", pcon24nm)

        new_groups_c = new_groups[new_groups["PCON24CD"] == pcon24cd].copy()
        if geo_available:
            addon_groups = geo.compute_geographic_addon(
                df_previous=df_previous,
                df_new_exploded=df_new_exploded,
                gdf_pcon=gdf_pcon,
                densities_df=densities_df,
                constituency_codes={pcon24cd},
                global_min_add_on=GLOBAL_MIN_ADD_ON,
                global_max_add_on=GLOBAL_MAX_ADD_ON,
            )
        else:
            addon_groups = pd.DataFrame()

        combined = _combine_and_filter(new_groups_c, addon_groups, pcon_map_df)
        combined = _drop_buy_sell(combined)
        combined = combined[
            combined["posts_a_month"].isna() | (combined["posts_a_month"] >= 10)
        ].copy()

        if stop_before_ai_assessment:
            out_path = OUTPUT_DIR / f"{pcon24nm}-intermediate.csv"
            combined.to_csv(
                out_path,
                index=False,
                encoding="utf-8",
                errors="surrogatepass",
            )
            logger.info(
                "Saved %d rows (pre-assessment) → %s", len(combined), out_path
            )
            return combined

        desc = descriptions.ensure_description(
            area_id=pcon24cd,
            area_name=pcon24nm,
            path=DESCRIPTIONS_PATH,
            area_kind=AREA_KIND,
            id_col="PCON24CD",
            name_col="PCON24NM",
        )
        if not desc:
            logger.warning("No description for %s — using empty string for assessment", pcon24nm)

        if "first_assessment" not in combined.columns:
            combined["first_assessment"] = None

        if not combined.empty:
            assessed = assessment.assess_groups(
                df=combined,
                area_description=desc,
                area_kind=AREA_KIND,
            )
            if "first_assessment" in assessed.columns:
                combined["first_assessment"] = assessed["first_assessment"].values

        final_c = combined[
            (combined["first_assessment"] != "No") & (combined["members"] > 50)
        ].copy()
        final_c = _drop_unsure_churches(final_c, pcon24nm)
        final_c = final_c.sort_values(
            by=["PCON24CD", "members", "posts_a_month"],
            ascending=False,
            na_position="last",
        )

        breakdown = _targeting_breakdown(pcon24nm, final_c, new_groups_c)
        if not breakdown.empty:
            breakdown_path = OUTPUT_DIR / f"targeting_breakdown_{pcon24nm}.csv"
            breakdown.to_csv(breakdown_path, index=False, encoding="utf-8", errors="surrogatepass")
            logger.info("  Saved targeting breakdown → %s", breakdown_path)

        final_c.to_csv(
            intermediate_path,
            index=False,
            encoding="utf-8",
            errors="surrogatepass",
        )
        logger.info("  Saved %d rows → %s", len(final_c), intermediate_path)

    # ── Phase 3: Build final output from all Intermediate files ─────────────
    if constituency_name:
        # Only include Intermediate files for the requested constituency codes
        allowed_codes = {str(code) for code in pcon_codes}
        intermediate_files = sorted(
            p for p in INTERMEDIATE_DIR.glob("*.csv") if p.stem in allowed_codes
        )
    else:
        intermediate_files = sorted(INTERMEDIATE_DIR.glob("*.csv"))
    if not intermediate_files:
        logger.warning("No Intermediate CSVs found; writing empty output")
        final_df = pd.DataFrame()
    else:
        parts = []
        for p in intermediate_files:
            part = pd.read_csv(p, encoding="latin-1", on_bad_lines="skip")
            parts.append(part)
            # Save a named file per constituency
            if "PCON24NM" in part.columns and not part.empty:
                name = part["PCON24NM"].iloc[0]
                named_path = OUTPUT_DIR / f"groups_{name}.csv"
                part.to_csv(named_path, index=False, encoding="utf-8", errors="surrogatepass")
                logger.info("  Saved %d rows → %s", len(part), named_path)

        final_df = pd.concat(parts, axis=0, ignore_index=True)
        final_df = final_df.sort_values(
            by=["PCON24CD", "members", "posts_a_month"],
            ascending=False,
            na_position="last",
        )
        final_df = final_df.drop_duplicates(subset=["url"])

    if constituency_name:
        run_path = OUTPUT_DIR / f"groups_{constituency_name}.csv"
        final_df.to_csv(run_path, index=False, encoding="utf-8", errors="surrogatepass")
        logger.info("Saved %d rows → %s", len(final_df), run_path)
    else:
        out_path = OUTPUT_DIR / "output.csv"
        final_df.to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
        logger.info("Saved %d rows → %s", len(final_df), out_path)

    if not final_df.empty:
        summary = (
            final_df.groupby("PCON24NM")
            .agg(
                num_groups=("url", "count"),
                members_sum=("members", "sum"),
                members_mean=("members", "mean"),
                posts_sum=("posts_a_month", "sum"),
            )
            .reset_index()
            .sort_values("num_groups", ascending=False)
        )
        logger.info("Summary:\n%s", summary.head(30).to_string(index=False))

    return final_df


def main():
    parser = argparse.ArgumentParser(description="Prepare Libby List pipeline")
    parser.add_argument(
        "--constituency",
        type=str,
        default=None,
        help="Run for a single constituency (by PCON24NM). Writes only {name}-run.csv (does not touch output.csv).",
    )
    parser.add_argument(
        "--stop-before-ai-assessment",
        action="store_true",
        help="Run up to combining groups, then write {constituency}-intermediate.csv and exit before AI assessment.",
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Path to a scraped CSV file to process directly, instead of the master file.",
    )
    args = parser.parse_args()

    if args.stop_before_ai_assessment and not args.constituency:
        logger.error("--stop-before-ai-assessment requires --constituency to be set")
        sys.exit(1)

    run(
        constituency_name=args.constituency,
        stop_before_ai_assessment=args.stop_before_ai_assessment,
        input_path=Path(args.input) if args.input else None,
    )


if __name__ == "__main__":
    main()
