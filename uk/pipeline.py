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
import shutil
import sys
from pathlib import Path

import pandas as pd

from libby_core import assessment, descriptions
from uk import about_context, data_loading, geo, local_about_scraper, parsing
from uk.generate_search import slugify
from uk.settings import (
    ABOUT_PAGES_DIR,
    CLACTON_INPUTS_DIR,
    DESCRIPTIONS_PATH,
    DENSITIES_PATH,
    GEOJSON_PATH,
    GROUP_LOG_PATH,
    GROUP_OVERRIDES_PATH,
    INTERMEDIATE_DIR,
    NEW_SCRAPE_PATH,
    OUTPUT_DIR,
    PCON_MAPPING_PATH,
    PREVIOUS_SCRAPE_PATH,
    REDO_GROUPS_PATH,
    SCRAPED_DIR,
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
                # _combine's keep_cols, so outputs are unchanged.
                "target_types": sorted(set(g["target_type"].dropna()) - {""}) if "target_type" in g.columns else [],
                "target_sources": sorted(set(g["target_source"].dropna()) - {""}) if "target_source" in g.columns else [],
            }),
            include_groups=False,
        )
        .reset_index(drop=True)
    )
    return agg


def _combine(
    new_groups: pd.DataFrame,
    addon_groups: pd.DataFrame,
    pcon_map_df: pd.DataFrame,
) -> pd.DataFrame:
    """Combine new-scrape groups with add-on groups and merge constituency
    names. Drops no rows — the public/dedupe/locality filters are applied
    separately in run() so each can be individually logged to the group
    log (see _dropped_rows)."""
    keep_cols = ["PCON24CD", "name", "url", "public_y_n", "members", "posts_a_month", "locality", "locality_name"]

    parts = [new_groups[[c for c in keep_cols if c in new_groups.columns]]]
    if not addon_groups.empty:
        parts.append(addon_groups[[c for c in keep_cols if c in addon_groups.columns]])

    combined = pd.concat(parts, axis=0, ignore_index=True)
    combined = combined.sort_values(
        by=["PCON24CD", "members", "posts_a_month"], ascending=False
    )

    # Merge PCON24NM
    pcon_unique = pcon_map_df.drop_duplicates(subset=["PCON24CD"])
    combined = combined.merge(
        pcon_unique[["PCON24CD", "PCON24NM"]], on="PCON24CD", how="left"
    )
    return combined


def reconstruct_constituency_candidates(pcon24nm: str, input_path: Path | None = None) -> pd.DataFrame:
    """Rebuild the full pre-filter candidate pool for one constituency,
    straight from its own scraped file — no LLM calls, no re-scraping,
    read-only (never touches NEW_SCRAPE_PATH/redo_groups.csv the way a real
    run() does). Used by uk/recover_group.py to pull a specific rejected
    group's full row back out after the fact — nothing this granular
    survives past a normal run (group_log.csv is deliberately lean; see its
    own module comment), so recovery re-derives it instead of storing it.

    Raises FileNotFoundError if there's no scraped file for this
    constituency, or ValueError if `pcon24nm` isn't a real constituency
    name — both meant to surface as a clear CLI error, not a stack trace
    the caller has to interpret.
    """
    if input_path is None:
        input_path = SCRAPED_DIR / f"{slugify(pcon24nm)}_search_targets.csv"
    if not input_path.exists():
        raise FileNotFoundError(f"No scraped file for {pcon24nm!r} at {input_path}")

    df_new_exploded = data_loading.load_new_scrape(input_path)
    df_new_exploded = _parse_details_on_exploded(df_new_exploded)

    if PCON_MAPPING_PATH.exists():
        pcon_map_df = data_loading.load_pcon_mapping()
    else:
        pcon_map_df = df_new_exploded[["PCON24CD", "PCON24NM"]].drop_duplicates().reset_index(drop=True)

    match = pcon_map_df[pcon_map_df["PCON24NM"] == pcon24nm]
    if match.empty:
        raise ValueError(f"Constituency {pcon24nm!r} not found in PCON mapping")
    pcon_codes = set(match["PCON24CD"].unique())

    new_groups = _aggregate_new_groups(df_new_exploded, pcon_codes)
    new_groups_c = new_groups[new_groups["PCON24CD"].isin(pcon_codes)].copy()

    geo_available = GEOJSON_PATH.exists() and DENSITIES_PATH.exists() and PREVIOUS_SCRAPE_PATH.exists()
    if geo_available:
        addon_groups = geo.compute_geographic_addon(
            df_previous=data_loading.load_previous_scrape(),
            df_new_exploded=df_new_exploded,
            gdf_pcon=data_loading.load_constituency_boundaries(),
            densities_df=data_loading.load_densities(),
            constituency_codes=pcon_codes,
            global_min_add_on=GLOBAL_MIN_ADD_ON,
            global_max_add_on=GLOBAL_MAX_ADD_ON,
        )
    else:
        addon_groups = pd.DataFrame()

    return _combine(new_groups_c, addon_groups, pcon_map_df).reset_index(drop=True)


# ── group log ─────────────────────────────────────────────────────────────
# One shared ledger for every group ever considered, across both pipelines:
# was it accepted into a final groups_*.csv, and why (or why not). Replaces
# the old per-constituency discarded/<code>.csv, the combined discarded.csv/
# discarded_<Name>.csv, and the ward side's separate discarded.csv — all
# consolidated into GROUP_LOG_PATH, upserted per area (see
# _upsert_group_log) so reprocessing an area replaces its rows rather than
# duplicating or leaving stale copies behind.
GROUP_LOG_COLUMNS = [
    "group", "group_url", "area_type", "area_name", "accepted", "stage", "reason",
    "members", "posts_a_month",
]

# Every non-"kept" stage a row can be dropped at, grouped for the
# control centre's "Auto-filtered vs AI-eliminated" split — every stage
# here is a deterministic, rule-based cut; "ai_assessment" (not in this
# set) is the only stage where a judgment call, not a fixed rule, decided.
AUTO_FILTER_STAGES = {
    "public_filter", "dedupe", "locality_filter", "buy_sell",
    "activity_filter", "members_filter", "church_heuristic",
}


def _apply_ai_verdict(
    before: pd.DataFrame,
    context_column: str | None,
    about_reassessed_ids: set,
) -> tuple[pd.DataFrame, dict]:
    """Apply the final AI-verdict filter — shared by uk.pipeline and
    uk.pipeline_ward, since the rule is identical for both.

    Drops "No" as always. Also drops "Unsure" specifically when real
    About-page context was actually used to reach that verdict (cached from
    an earlier scrape, or freshly scraped via --about this run) — the extra
    evidence had its chance to confirm relevance and didn't, so it's treated
    as a reject rather than a keep. An "Unsure" with no About-context
    available at all (nothing to check it against) is kept, same as before
    this rule existed — there's no stronger signal to base a rejection on.

    Returns (surviving_df, reason_by_row_id) — reason_by_row_id only has
    entries for dropped rows, ready to hand straight to _dropped_rows.
    """
    verdict = before["first_assessment"].astype(str).str.strip().str.casefold()
    is_no = verdict == "no"
    is_unsure = verdict == "unsure"
    had_context = (
        before[context_column].notna()
        if context_column and context_column in before.columns
        else pd.Series(False, index=before.index)
    )
    was_reassessed = before["_row_id"].isin(about_reassessed_ids)
    drop_mask = is_no | (is_unsure & had_context)
    after = before[~drop_mask].copy()

    def _reason(no, unsure_with_context, reassessed):
        if no:
            return (
                'AI assessed group as not relevant to the area ("No"), after a local --about re-check'
                if reassessed else
                'AI assessed group as not relevant to the area ("No")'
            )
        # unsure_with_context
        return (
            'AI still assessed group as only possibly relevant ("Unsure") after a local '
            '--about re-check — rejected, fresh About-context didn\'t confirm it'
            if reassessed else
            'AI still assessed group as only possibly relevant ("Unsure") with existing '
            'About-page context available — rejected, About-context didn\'t confirm it'
        )

    reason_by_row_id = {
        rid: _reason(no, unsure_ctx, reassessed)
        for rid, no, unsure_ctx, reassessed in zip(
            before.loc[drop_mask, "_row_id"], is_no[drop_mask],
            (is_unsure & had_context)[drop_mask], was_reassessed[drop_mask],
        )
    }
    return after, reason_by_row_id


def _load_overrides_by_area(area_type: str) -> dict[str, dict[str, set[str]]]:
    """area_name -> {"include": {urls}, "exclude": {urls}} for the given
    area_type — "include" from uk/recover_group.py, "exclude" from
    uk/remove_group.py. Loaded once per run, same pattern as the
    About-context cache. Rows recorded before the "decision" column
    existed default to "include" — every override before uk/remove_group.py
    existed was inherently a recovery."""
    if not GROUP_OVERRIDES_PATH.exists():
        return {}
    try:
        odf = pd.read_csv(GROUP_OVERRIDES_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass")
    except Exception:
        logger.warning("Could not read %s — proceeding without group overrides", GROUP_OVERRIDES_PATH)
        return {}
    odf = odf[odf.get("area_type") == area_type].copy()
    if "decision" not in odf.columns:
        odf["decision"] = "include"
    else:
        odf["decision"] = odf["decision"].fillna("").replace("", "include")
    return {
        name: {
            "include": set(group.loc[group["decision"] == "include", "group_url"]),
            "exclude": set(group.loc[group["decision"] == "exclude", "group_url"]),
        }
        for name, group in odf.groupby("area_name")
    }


def _missing_override_rows(
    final: pd.DataFrame,
    candidate_pool: pd.DataFrame,
    override_urls: set[str],
    pcon24nm: str | None = None,
) -> pd.DataFrame:
    """Return the rows for any manually-recovered url (see
    uk/recover_group.py) in `override_urls` not already present in `final`,
    pulled back from `candidate_pool` — that area's own full pre-filter
    candidate set, already in memory from earlier in this same run (see
    run()/`_process_file`'s new_groups_c/agg) — so a real future reprocess
    doesn't silently drop a recovered group again.

    Reindexed to `final`'s exact columns first, so a candidate pool's extra
    columns (e.g. target_types/target_sources) never leak into the written
    output and corrupt its schema. Deliberately returned separately rather
    than pre-merged into `final`, so the caller can give these rows their
    own group_log reason instead of running them through _kept_rows's
    verdict-based reasoning (which doesn't apply to a manual override).

    `pcon24nm`, when given (constituency callers only), overwrites the
    recovered row's PCON24NM — needed there because `candidate_pool` might
    not have it yet at this point. Ward callers must leave this as None:
    a ward row's PCON24NM is its *parent constituency*'s name, not the
    ward's own — `candidate_pool` (agg) already carries the correct value
    through untouched, and overwriting it here would corrupt that field.
    """
    if not override_urls:
        return final.iloc[0:0]
    missing = override_urls - set(final["url"])
    if not missing:
        return final.iloc[0:0]
    recovered = candidate_pool[candidate_pool["url"].isin(missing)].copy()
    if recovered.empty:
        return final.iloc[0:0]
    recovered = recovered.reindex(columns=final.columns)
    if pcon24nm is not None:
        recovered["PCON24NM"] = pcon24nm
    recovered["first_assessment"] = "Manually recovered"
    return recovered


def _recovered_ledger_rows(recovered: pd.DataFrame, area_type: str, area_name: str) -> list[dict]:
    """group_log records (accepted="Y", stage="recovered") for rows added
    by _missing_override_rows — see uk/recover_group.py for how a group
    gets into group_overrides.csv in the first place."""
    return [
        {
            "group": r.get("name"),
            "group_url": r.get("url"),
            "area_type": area_type,
            "area_name": area_name,
            "accepted": "Y",
            "stage": "recovered",
            "reason": "Manually recovered — see uk/output/group_overrides.csv",
            "members": r.get("members"),
            "posts_a_month": r.get("posts_a_month"),
        }
        for _, r in recovered.iterrows()
    ]


def _apply_exclusions(final: pd.DataFrame, exclude_urls: set[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split `final` into (kept, excluded) by exclude_urls — the
    manually-removed groups (see uk/remove_group.py) that must stay
    removed even though normal filtering would otherwise have accepted
    them this run. The two output sets are disjoint by construction from
    _load_overrides_by_area (a later uk.recover_group call for the same
    url replaces an earlier uk.remove_group one, and vice versa), so this
    never needs to reconcile the same url appearing in both."""
    if not exclude_urls:
        return final, final.iloc[0:0]
    mask = final["url"].isin(exclude_urls)
    return final[~mask].copy(), final[mask].copy()


def _excluded_ledger_rows(excluded: pd.DataFrame, area_type: str, area_name: str) -> list[dict]:
    """group_log records (accepted="N", stage="manually_excluded") for
    rows dropped by _apply_exclusions — see uk/remove_group.py for how a
    group gets into group_overrides.csv as an exclusion."""
    return [
        {
            "group": r.get("name"),
            "group_url": r.get("url"),
            "area_type": area_type,
            "area_name": area_name,
            "accepted": "N",
            "stage": "manually_excluded",
            "reason": "Manually removed — see uk/output/group_overrides.csv",
            "members": r.get("members"),
            "posts_a_month": r.get("posts_a_month"),
        }
        for _, r in excluded.iterrows()
    ]


def _dropped_rows(
    before: pd.DataFrame,
    after: pd.DataFrame,
    area_type: str,
    area_name: str,
    stage: str,
    reason: str | dict,
) -> list[dict]:
    """Diff `before`/`after` by row identity and return one group_log
    record (accepted="N") per row dropped at this stage.

    `reason` is either one string used for every dropped row, or a dict
    mapping `_row_id` -> a per-row reason string, for the one stage
    (AI assessment) where the cause genuinely differs row to row — a "No"
    reached only after a local --about re-check needs to say so, unlike a
    confident first-pass "No".

    Diffs on a `_row_id` column (assigned once per area, before any
    filtering) rather than `url`, because a dedupe step can drop a row
    whose url survives via a different row — a url-set diff would miss that
    drop entirely.
    """
    if before.empty or "_row_id" not in before.columns:
        return []
    dropped_ids = set(before["_row_id"]) - set(after["_row_id"])
    if not dropped_ids:
        return []
    dropped = before[before["_row_id"].isin(dropped_ids)]
    records = []
    for _, r in dropped.iterrows():
        row_reason = reason.get(r["_row_id"], "") if isinstance(reason, dict) else reason
        records.append({
            "group": r.get("name"),
            "group_url": r.get("url"),
            "area_type": area_type,
            "area_name": area_name,
            "accepted": "N",
            "stage": stage,
            "reason": row_reason,
            "members": r.get("members"),
            "posts_a_month": r.get("posts_a_month"),
        })
    return records


def _kept_rows(
    final: pd.DataFrame,
    area_type: str,
    area_name: str,
    about_reassessed_ids: set,
) -> list[dict]:
    """One group_log record (accepted="Y") per group that survived every
    filter, with a reason built from its final AI verdict — noting when
    that verdict came from a local --about re-check rather than the
    first-pass assessment, and flagging the rare case where the AI call
    itself never returned a verdict at all (libby_core.ai.iterate_df_rows
    leaves a failed row's response blank rather than retrying or dropping
    it, so it would otherwise survive silently with no explanation).

    A kept "Unsure" here can only mean no About-context was ever available
    for it — an Unsure verdict reached WITH real About-context (cached or
    freshly --about-scraped) is now rejected upstream, before `final` is
    even built (see run()'s ai_assessment stage)."""
    records = []
    for _, r in final.iterrows():
        verdict = r.get("first_assessment")
        reassessed = r.get("_row_id") in about_reassessed_ids
        if pd.isna(verdict) or not str(verdict).strip():
            reason = "AI assessment never returned a verdict (LLM call error) — kept by default"
        elif str(verdict).strip().casefold() == "yes":
            reason = 'AI assessed group as relevant to the area ("Yes")'
            if reassessed:
                reason += ", confirmed after a local --about re-check"
        else:
            reason = 'AI assessed group as possibly relevant to the area ("Unsure") — no About-context was available to check it against'
        records.append({
            "group": r.get("name"),
            "group_url": r.get("url"),
            "area_type": area_type,
            "area_name": area_name,
            "accepted": "Y",
            "stage": "kept",
            "reason": reason,
            "members": r.get("members"),
            "posts_a_month": r.get("posts_a_month"),
        })
    return records


def _upsert_group_log(area_type: str, area_name: str, rows: list[dict]) -> None:
    """Replace this area's rows in GROUP_LOG_PATH with `rows` — same
    replace-per-area semantics as _update_targeting_master, so reprocessing
    an area never duplicates or leaves stale entries behind."""
    new_rows = pd.DataFrame(rows, columns=GROUP_LOG_COLUMNS)
    if GROUP_LOG_PATH.exists():
        # encoding_errors="surrogatepass" matches how this file is written
        # (see uk.settings's note on why not encoding="latin-1").
        existing = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
        existing = existing[
            ~((existing["area_type"] == area_type) & (existing["area_name"] == area_name))
        ]
        combined_log = pd.concat([existing, new_rows], ignore_index=True)
    else:
        combined_log = new_rows
    # Fixed column order/set regardless of what an older-schema `existing`
    # file did or didn't have (e.g. rows written before the "stage" column
    # existed) — reindex adds any missing column as blank rather than
    # leaving concat's column order to drift.
    combined_log = combined_log.reindex(columns=GROUP_LOG_COLUMNS, fill_value="")
    combined_log.to_csv(GROUP_LOG_PATH, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("  Group log: %d row(s) for %s → %s", len(new_rows), area_name, GROUP_LOG_PATH)


_BUY_SELL_PATTERN = re.compile(
    r"\b(buy|sell|selling|sold|sale|for sale|marketplace|wanted|swap|swapping|"
    r"freebie|freebies|free stuff|preloved|pre-loved|second.?hand|bargain)\b",
    re.IGNORECASE,
)


def _targeting_breakdown(pcon24nm: str, final: pd.DataFrame, new_groups: pd.DataFrame) -> pd.DataFrame:
    """Per-group targeting report (same shape as pipeline_ward.py's
    targeting_breakdown_{ward}.csv): one row per final surviving group, with
    the target types/sources that found it '; '-joined. The tag sets live on
    the aggregated new-scrape groups (they're dropped by _combine's
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


def _update_targeting_master(pcon24nm: str, breakdown: pd.DataFrame) -> None:
    """Upsert this constituency's rows into the cumulative targeting master
    (uk/output/targeting_master.csv): one row per final group with its
    '; '-joined tags/sources. Replace-per-area semantics — a rerun deletes
    the constituency's old rows first, so the master is always the latest
    result per area, never duplicated. Geo add-on borrows (locality A/L/R)
    were never found by a tagged search, so their source is labelled
    'geo add on' rather than left blank."""
    master_path = OUTPUT_DIR / "targeting_master.csv"
    rows = pd.DataFrame({
        "name": breakdown["name"].astype(str),
        "url": breakdown["url"].astype(str),
        "constituency": pcon24nm,
        "tags": breakdown["target_types"],
        "sources": breakdown["target_sources"],
    })
    if "locality" in breakdown.columns:
        addon = (breakdown["locality"].astype(str) != "C") & (rows["sources"] == "")
        rows.loc[addon.values, "sources"] = "geo add on"

    if master_path.exists():
        # encoding_errors="surrogatepass" matches how this file is written
        # (see uk.settings's note on why not encoding="latin-1").
        master = pd.read_csv(master_path, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
        master = master[master["constituency"] != pcon24nm]
        master = pd.concat([master, rows], ignore_index=True)
    else:
        master = rows
    master.to_csv(master_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("  Updated targeting master (%d rows for %s) → %s", len(rows), pcon24nm, master_path)


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
    use_context: bool = True,
    use_about: bool = False,
    about_limit: int = local_about_scraper.DEFAULT_MAX_GROUPS,
    force: bool = False,
):
    # --about implies --context: there's no reason to scrape fresh About
    # text and then not use it.
    use_context = use_context or use_about

    # ── Phase 1: One-time setup ─────────────────────────────────────────────
    if input_path:
        logger.info("Using input file: %s", input_path)
    else:
        _merge_redo_groups_into_master()
    logger.info("Loading data …")
    df_new_exploded = data_loading.load_new_scrape(input_path or NEW_SCRAPE_PATH)

    # Loaded once for the whole run, not per constituency — see
    # uk.about_context.load_global_about_context for why a single global,
    # URL-keyed cache (unioning every about_pages CSV ever pulled, for any
    # constituency or ward) is preferred over one file per area. --about
    # grows this same dict in place as it scrapes, so later areas in a
    # multi-area run benefit from earlier areas' local scrapes too.
    about_lookup = about_context.load_global_about_context(ABOUT_PAGES_DIR) if use_context else {}

    # Manually-recovered groups (see uk/recover_group.py) — re-applied per
    # constituency at the end of its processing below, so a real reprocess
    # doesn't silently drop something you deliberately overrode.
    overrides_by_constituency = _load_overrides_by_area("constituency")

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

    # Read once, not per-constituency: which constituencies already have
    # group_log.csv rows, for the "already done, but predates this log"
    # backfill hint below.
    logged_constituencies: set[str] = set()
    if GROUP_LOG_PATH.exists():
        try:
            log_df = pd.read_csv(
                GROUP_LOG_PATH, usecols=["area_type", "area_name"], dtype=str,
                encoding="utf-8", encoding_errors="surrogatepass",
            )
            logged_constituencies = set(log_df.loc[log_df["area_type"] == "constituency", "area_name"])
        except Exception:
            logger.warning("Could not read %s to check for already-logged constituencies", GROUP_LOG_PATH)

    # ── Phase 2: Per-constituency loop (run-once, skip if done) ─────────────
    for _, row in constituency_list.iterrows():
        pcon24cd = row["PCON24CD"]
        pcon24nm = row["PCON24NM"]
        intermediate_path = INTERMEDIATE_DIR / f"{pcon24cd}.csv"

        if not stop_before_ai_assessment and not force and intermediate_path.exists():
            logger.info("Skipping (already done): %s", pcon24nm)
            if pcon24nm not in logged_constituencies:
                logger.info(
                    "  (no group_log.csv rows for %s — processed before this log "
                    "existed; pass --force to reprocess and backfill it, or "
                    "delete intermediate/%s.csv yourself)", pcon24nm, pcon24cd,
                )
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

        # _row_id identifies each physical row through every filter below
        # (independent of the pandas index), so a dedupe drop can be
        # attributed even though its url survives via a different row. See
        # _dropped_rows.
        combined = _combine(new_groups_c, addon_groups, pcon_map_df).reset_index(drop=True)
        combined["_row_id"] = combined.index
        ledger: list[dict] = []

        before = combined
        combined = combined[combined.public_y_n == True].copy()  # noqa: E712
        ledger += _dropped_rows(
            before, combined, "constituency", pcon24nm, "public_filter",
            "Not marked public (private, or public/private unknown)",
        )

        before = combined
        combined = combined.drop_duplicates(subset=["url"])
        ledger += _dropped_rows(
            before, combined, "constituency", pcon24nm, "dedupe",
            "Duplicate group url (kept the higher-ranked occurrence)",
        )

        before = combined
        combined = combined[combined["locality"] != "X"]
        ledger += _dropped_rows(
            before, combined, "constituency", pcon24nm, "locality_filter",
            "Locality classified as national/too broad (X)",
        )

        before = combined
        combined = _drop_buy_sell(combined)
        ledger += _dropped_rows(
            before, combined, "constituency", pcon24nm, "buy_sell",
            "Group name matched buy/sell/marketplace pattern",
        )

        before = combined
        combined = combined[
            combined["posts_a_month"].isna() | (combined["posts_a_month"] >= 10)
        ].copy()
        ledger += _dropped_rows(
            before, combined, "constituency", pcon24nm, "activity_filter",
            "Posts per month below threshold (< 10)",
        )

        if stop_before_ai_assessment:
            out_path = OUTPUT_DIR / f"{pcon24nm}-intermediate.csv"
            combined.drop(columns=["_row_id"]).to_csv(
                out_path,
                index=False,
                encoding="utf-8",
                errors="surrogatepass",
            )
            logger.info(
                "Saved %d rows (pre-assessment) → %s", len(combined), out_path
            )
            # Only drop records exist this early (no AI verdict yet, so no
            # "accepted" rows) — upserted anyway so a partial/test run still
            # leaves a trace, same as before.
            _upsert_group_log("constituency", pcon24nm, ledger)
            return combined.drop(columns=["_row_id"])

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

        context_column = None
        if about_lookup:
            combined["about_context"] = combined["url"].map(about_lookup)
            n_matched = combined["about_context"].notna().sum()
            logger.info(
                "  %d/%d groups matched About context for %s",
                n_matched, len(combined), pcon24nm,
            )
            context_column = "about_context"

        if not combined.empty:
            assessed = assessment.assess_groups(
                df=combined,
                area_description=desc,
                area_kind=AREA_KIND,
                context_column=context_column,
            )
            if "first_assessment" in assessed.columns:
                combined["first_assessment"] = assessed["first_assessment"].values

        # Row ids reassessed via a local --about re-check this run — tracked
        # separately from `combined["first_assessment"]` because the ledger
        # needs to say *why* a verdict is what it is, not just what it is
        # (see _dropped_rows/_kept_rows).
        about_reassessed_ids: set = set()

        # ── --about: escalate on uncertainty, inline, this same run ──────────
        # Scrape only groups pass-1 couldn't resolve (Unsure) and that
        # don't already have About text from somewhere else — small,
        # targeted, and using a local Selenium session (your own machine,
        # your own Facebook login) rather than a separate trip to libby.
        # See uk/local_about_scraper.py's module docstring for the full
        # design rationale and the one-time setup it requires.
        if use_about and not combined.empty:
            is_unsure = combined["first_assessment"].astype(str).str.strip().str.casefold() == "unsure"
            has_context = (
                combined[context_column].notna()
                if context_column and context_column in combined.columns
                else pd.Series(False, index=combined.index)
            )
            to_scrape = combined[is_unsure & ~has_context]

            if to_scrape.empty:
                logger.info("  --about: nothing left to scrape for %s (no uncached Unsure groups)", pcon24nm)
            else:
                urls = to_scrape["url"].tolist()
                names = dict(zip(to_scrape["url"], to_scrape["name"]))
                logger.info(
                    "  --about: %d Unsure, uncached group(s) to scrape locally for %s",
                    len(urls), pcon24nm,
                )
                try:
                    scraped_df = local_about_scraper.scrape_about_locally(
                        urls, name_by_url=names, max_groups=about_limit,
                    )
                except (local_about_scraper.LocalAboutScraperUnavailable, RuntimeError) as e:
                    logger.warning("  --about unavailable this run (%s) — continuing without it", e)
                    scraped_df = pd.DataFrame()

                if not scraped_df.empty:
                    scraped_path = local_about_scraper.write_local_about(
                        scraped_df, slugify(pcon24nm), out_dir=ABOUT_PAGES_DIR,
                    )
                    new_context = about_context.load_about_context(scraped_path)
                    about_lookup.update(new_context)  # shared for the rest of this run too

                    if "about_context" not in combined.columns:
                        combined["about_context"] = pd.NA
                    newly_matched = combined["url"].isin(new_context)
                    combined.loc[newly_matched, "about_context"] = (
                        combined.loc[newly_matched, "url"].map(new_context)
                    )
                    context_column = "about_context"

                    reassess_rows = combined[newly_matched]
                    logger.info(
                        "  Re-assessing %d group(s) with fresh About context for %s",
                        len(reassess_rows), pcon24nm,
                    )
                    reassessed = assessment.assess_groups(
                        df=reassess_rows,
                        area_description=desc,
                        area_kind=AREA_KIND,
                        context_column=context_column,
                    )
                    combined.loc[newly_matched, "first_assessment"] = reassessed["first_assessment"].values
                    about_reassessed_ids |= set(combined.loc[newly_matched, "_row_id"])

        before = combined
        final_c, ai_reason = _apply_ai_verdict(before, context_column, about_reassessed_ids)
        ledger += _dropped_rows(before, final_c, "constituency", pcon24nm, "ai_assessment", ai_reason)

        before = final_c
        final_c = final_c[final_c["members"] > 50].copy()
        ledger += _dropped_rows(
            before, final_c, "constituency", pcon24nm, "members_filter", "Member count <= 50",
        )

        before = final_c
        final_c = _drop_unsure_churches(final_c, pcon24nm)
        ledger += _dropped_rows(
            before, final_c, "constituency", pcon24nm, "church_heuristic",
            'Assessed "Unsure" + church-pattern name, no area-name match',
        )

        ledger += _kept_rows(final_c, "constituency", pcon24nm, about_reassessed_ids)

        area_overrides = overrides_by_constituency.get(pcon24nm, {"include": set(), "exclude": set()})

        recovered = _missing_override_rows(final_c, new_groups_c, area_overrides["include"], pcon24nm=pcon24nm)
        if not recovered.empty:
            logger.info("  Re-applied %d manually-recovered group(s) for %s", len(recovered), pcon24nm)
            ledger += _recovered_ledger_rows(recovered, "constituency", pcon24nm)
            final_c = pd.concat([final_c, recovered], ignore_index=True)

        final_c, excluded = _apply_exclusions(final_c, area_overrides["exclude"])
        if not excluded.empty:
            logger.info("  Re-applied %d manually-removed group(s) for %s", len(excluded), pcon24nm)
            ledger += _excluded_ledger_rows(excluded, "constituency", pcon24nm)

        _upsert_group_log("constituency", pcon24nm, ledger)

        # about_context (when --context matched something) is prompt input
        # only — the raw About-page text has no place in the shipped output
        # schema, so it's dropped here rather than carried into groups_*.csv.
        final_c = final_c.drop(columns=["_row_id", "about_context"], errors="ignore")
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
            _update_targeting_master(pcon24nm, breakdown)

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
            part = pd.read_csv(p, encoding="utf-8", encoding_errors="surrogatepass", on_bad_lines="skip")
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

        # Stage the finished result straight into Clacton-etc/inputs/, same
        # as batch_pipeline.sh's "sync" mode already does for the CLI
        # workflow — a single-constituency run is "done" once this lands
        # here, no separate manual move needed.
        CLACTON_INPUTS_DIR.mkdir(parents=True, exist_ok=True)
        staged_path = CLACTON_INPUTS_DIR / run_path.name
        shutil.move(str(run_path), str(staged_path))
        logger.info("Moved final result → %s", staged_path)
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
    parser.add_argument(
        "--no-context",
        action="store_true",
        help=(
            "Skip loading the global About-context cache (on by default — see "
            "--context's old help text below for what it does). Only useful "
            "for a quick name-only assessment; leaving About-context on is "
            "free (no scraping, no extra LLM cost) so there's no real reason "
            "to normally disable it."
        ),
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help=(
            "On by default — this flag is now a no-op kept for old scripts/"
            "muscle memory, use --no-context to opt out instead. Loads a "
            "global, URL-keyed cache of every group's About-page text from "
            "every *_about.csv found anywhere under uk/data/about_pages/ (any "
            "constituency or ward's about-scrape helps every other area, not "
            "just its own) and passes a matched group's text to the LLM as "
            "extra context for the relevance assessment."
        ),
    )
    parser.add_argument(
        "--about",
        action="store_true",
        help=(
            "Implies --context. For groups still 'Unsure' after the first "
            "assessment pass and not already in the About cache, About-scrape "
            "them inline using a LOCAL Chrome session (this machine, your own "
            "Facebook login — see uk/local_about_scraper.py) and re-assess "
            "just those groups. One-time setup: pip install selenium, then "
            "`python -m uk.local_about_scraper --login`."
        ),
    )
    parser.add_argument(
        "--about-limit",
        type=int,
        default=local_about_scraper.DEFAULT_MAX_GROUPS,
        help=f"Max groups to About-scrape locally per area with --about (default: {local_about_scraper.DEFAULT_MAX_GROUPS})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reprocess a constituency even if intermediate/<code>.csv already "
            "exists (normally skipped). Useful for backfilling group_log.csv "
            "for constituencies processed before it existed, or after "
            "changing filtering logic."
        ),
    )
    args = parser.parse_args()

    if args.stop_before_ai_assessment and not args.constituency:
        logger.error("--stop-before-ai-assessment requires --constituency to be set")
        sys.exit(1)

    run(
        constituency_name=args.constituency,
        stop_before_ai_assessment=args.stop_before_ai_assessment,
        input_path=Path(args.input) if args.input else None,
        use_context=not args.no_context,
        use_about=args.about,
        about_limit=args.about_limit,
        force=args.force,
    )


if __name__ == "__main__":
    main()
