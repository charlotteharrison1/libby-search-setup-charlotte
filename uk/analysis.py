"""Analysis helpers for the control centre's "Analysis Centre" tab —
cross-referencing group_log.csv against external reference data to answer
questions like "what kinds of areas produce the most private-group
eliminations." Read-only and exploratory: this never feeds back into or
changes what uk.pipeline/uk.pipeline_ward actually do, unlike
uk.recover_group.

First analysis: private-group elimination rate by rural/urban
classification, using mySociety's UK Composite Urban Rural Classification
(uk/data/reference/rural_index.json — see its own "resources" for the
underlying mySociety dataset).
"""

import json
import re
import unicodedata

import pandas as pd

from uk.settings import GROUP_LOG_PATH, RURAL_INDEX_PATH

# Cached at first use, not import time — RURAL_INDEX_PATH is a 20MB+ JSON
# file (the composite_ruc resource alone covers all ~42,600 UK LSOAs, far
# more granular than anything used here) and doesn't change during a
# session, so there's no reason to re-parse it on every request.
_RESOURCES_CACHE: dict[str, pd.DataFrame] | None = None

# la_ruc's official-name values carry a council-type prefix/suffix
# ("London Borough of Barnet", "Bolsover District Council") that our own
# ward scrapes' free-text local_authority strings never do (just "Barnet",
# "Bolsover") — stripped here so the two sides can actually match. Verified
# against all 397 real entries: produces 397 distinct normalized names, no
# collisions.
_LA_PREFIXES = ("london borough of ", "royal borough of ", "county borough of ", "city of ", "borough of ")
_LA_SUFFIXES = (
    " metropolitan borough council", " london borough council", " county borough council",
    " royal borough council", " borough council", " district council", " city council",
    " county council", " council",
)


def _normalize_la_name(name: str) -> str:
    n = re.sub(r"\s+", " ", str(name).strip().lower())
    for p in _LA_PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
            break
    for s in _LA_SUFFIXES:
        if n.endswith(s):
            n = n[: -len(s)]
            break
    return n.strip()


def _load_resources() -> dict[str, pd.DataFrame]:
    global _RESOURCES_CACHE
    if _RESOURCES_CACHE is None:
        with open(RURAL_INDEX_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _RESOURCES_CACHE = {r["name"]: pd.DataFrame(r["data"]) for r in data["resources"]}
    return _RESOURCES_CACHE


def _normalize_pcon_name(name: str) -> str:
    # Strips diacritics only ("Glyndŵr" -> "Glyndwr") — the one known gap
    # between this resource's Welsh-language spelling and
    # uk.settings.CONSTITUENCIES_PATH's plain-ASCII one for the same seat.
    decomposed = unicodedata.normalize("NFKD", str(name))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _constituency_ruc_lookup() -> dict[str, str]:
    """constituency-name -> RUC label ("Urban", "Rural", "Urban with rural
    areas", "Sparse and rural"). pcon_2025_ruc (not the older, similarly
    named pcon_ruc resource) — pcon_ruc predates the 2023 boundary review,
    so 233 of the current 650 constituencies (anything new or renamed at
    that review, e.g. "Cardiff East", "West Bromwich") simply aren't in it
    at all. pcon_2025_ruc matches all but one of the current 650 exactly;
    the last (a Welsh-diacritic spelling difference) is covered by
    normalizing both sides' names."""
    pcon = _load_resources()["pcon_2025_ruc"]
    return {_normalize_pcon_name(name): label for name, label in zip(pcon["constituency-name"], pcon["label"])}


def _local_authority_ruc_lookup() -> dict[str, str]:
    """normalized local authority name -> RUC label."""
    la = _load_resources()["la_ruc"]
    return {_normalize_la_name(name): label for name, label in zip(la["official-name"], la["ruc-cluster-label"])}


_WARD_AREA_NAME_RE = re.compile(r"^(.*) \(([^)]*)\)$")


def _resolve_ruc_label(area_type: str, area_name: str, pcon_lookup: dict, la_lookup: dict) -> str | None:
    if area_type == "constituency":
        return pcon_lookup.get(_normalize_pcon_name(area_name))
    # ward: area_name is "<ward name> (<local authority>)" — see
    # uk.pipeline_ward._ward_area_name. No ward-level classification exists
    # in the rural index, so the parent local authority's is used instead.
    m = _WARD_AREA_NAME_RE.match(area_name)
    if not m:
        return None
    return la_lookup.get(_normalize_la_name(m.group(2)))


def private_groups_by_area_type() -> dict:
    """For every group_log.csv row, resolve its area to a RUC label and
    compute, per label: how many groups were ever considered, how many
    were eliminated specifically for being private (stage == "public_filter"
    — not marked public), and the resulting rate. The rate (not just the
    raw count) is what actually answers "what kinds of areas have the most
    private groups" fairly — a raw count would just track search volume
    (urban areas are searched far more) rather than anything about the
    areas themselves.

    Areas that can't be matched to a RUC label (unrecognised local
    authority name, malformed area_name, etc.) are excluded from the
    aggregation but listed explicitly in "unmatched_areas" — silently
    dropping them without saying so would undermine the accuracy this is
    supposed to provide.
    """
    if not GROUP_LOG_PATH.exists():
        return {"rows": [], "unmatched_areas": [], "total_areas_matched": 0, "total_areas_unmatched": 0}

    log = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    if log.empty:
        return {"rows": [], "unmatched_areas": [], "total_areas_matched": 0, "total_areas_unmatched": 0}

    pcon_lookup = _constituency_ruc_lookup()
    la_lookup = _local_authority_ruc_lookup()

    log["ruc_label"] = [
        _resolve_ruc_label(at, an, pcon_lookup, la_lookup)
        for at, an in zip(log["area_type"], log["area_name"])
    ]

    areas = log[["area_type", "area_name", "ruc_label"]].drop_duplicates()
    unmatched_areas = sorted(
        f"{r.area_name} ({r.area_type})" for r in areas[areas["ruc_label"].isna()].itertuples()
    )

    matched = log.dropna(subset=["ruc_label"])
    if matched.empty:
        return {
            "rows": [], "unmatched_areas": unmatched_areas,
            "total_areas_matched": 0, "total_areas_unmatched": len(unmatched_areas),
        }

    is_private = matched["stage"] == "public_filter"
    summary = (
        matched.assign(is_private=is_private)
        .groupby("ruc_label")
        .agg(total_groups=("group_url", "count"), private_groups=("is_private", "sum"))
        .reset_index()
        .rename(columns={"ruc_label": "area_type_label"})
    )
    summary["private_groups"] = summary["private_groups"].astype(int)
    summary["private_rate"] = (summary["private_groups"] / summary["total_groups"]).round(4)
    summary = summary.sort_values("private_rate", ascending=False)

    return {
        "rows": summary.to_dict(orient="records"),
        "unmatched_areas": unmatched_areas,
        "total_areas_matched": int(areas["ruc_label"].notna().sum()),
        "total_areas_unmatched": len(unmatched_areas),
    }


def group_stats_by_area_type() -> dict:
    """For every *accepted* group_log.csv row, resolve its area to a RUC
    label and compute, per label: how many areas, how many groups, groups
    per area, and average/median member count and posting activity.

    Scoped to accepted groups only, unlike private_groups_by_area_type —
    "how many groups does an area have" and "how active are its groups"
    are naturally about the groups that made the final list, not every
    candidate considered along the way. `members`/`posts_a_month` are only
    populated for group_log.csv rows written after that schema addition —
    an older, not-yet-reprocessed area's rows read back as blank/NaN and
    are excluded from the member/activity averages (but still counted
    towards num_groups) rather than silently skewing them towards zero.
    """
    empty = {"rows": [], "rows_by_unit": [], "unmatched_areas": [], "total_areas_matched": 0, "total_areas_unmatched": 0}
    if not GROUP_LOG_PATH.exists():
        return empty

    log = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    if log.empty:
        return empty

    pcon_lookup = _constituency_ruc_lookup()
    la_lookup = _local_authority_ruc_lookup()

    log["ruc_label"] = [
        _resolve_ruc_label(at, an, pcon_lookup, la_lookup)
        for at, an in zip(log["area_type"], log["area_name"])
    ]

    areas = log[["area_type", "area_name", "ruc_label"]].drop_duplicates()
    unmatched_areas = sorted(
        f"{r.area_name} ({r.area_type})" for r in areas[areas["ruc_label"].isna()].itertuples()
    )

    matched = log[(log["accepted"] == "Y") & log["ruc_label"].notna()].copy()
    if matched.empty:
        return {
            **empty, "unmatched_areas": unmatched_areas,
            "total_areas_matched": int(areas["ruc_label"].notna().sum()),
            "total_areas_unmatched": len(unmatched_areas),
        }

    matched["members"] = pd.to_numeric(matched["members"], errors="coerce")
    matched["posts_a_month"] = pd.to_numeric(matched["posts_a_month"], errors="coerce")
    matched["area_key"] = matched["area_type"] + "|" + matched["area_name"]

    def _aggregate(group_cols: list[str]) -> pd.DataFrame:
        out = (
            matched.groupby(group_cols)
            .agg(
                num_areas=("area_key", "nunique"),
                num_groups=("group_url", "count"),
                avg_members=("members", "mean"),
                median_members=("members", "median"),
                avg_posts_a_month=("posts_a_month", "mean"),
                median_posts_a_month=("posts_a_month", "median"),
            )
            .reset_index()
            .rename(columns={"ruc_label": "area_type_label"})
        )
        out["avg_groups_per_area"] = (out["num_groups"] / out["num_areas"]).round(2)
        for col in ("avg_members", "median_members", "avg_posts_a_month", "median_posts_a_month"):
            out[col] = out[col].round(1)
        return out.sort_values("num_groups", ascending=False)

    summary = _aggregate(["ruc_label"])

    # Same aggregation, but also split by area_type (constituency vs ward) —
    # "avg_groups_per_area" and "avg_posts_a_month" are only comparable
    # across RUC labels when the "area" unit is the same size. A ward is a
    # small fraction of a constituency, so an RUC label with wards mixed in
    # (currently only "Urban" has any) reads artificially lower on both
    # figures than one made up entirely of constituencies — not because it
    # has fewer/less-active groups, but because its average includes a
    # smaller unit. rows_by_unit keeps constituency and ward separate so
    # nothing gets silently blended across an unequal geography; "rows"
    # above stays as the original blended-by-label view for the sections
    # that already use it.
    by_unit = _aggregate(["ruc_label", "area_type"])

    return {
        # to_json (not to_dict) so a NaN average (a label with no groups
        # carrying numeric member/activity data at all) serializes as valid
        # JSON null, not a bare NaN token json.dumps would choke a strict
        # parser on.
        "rows": json.loads(summary.to_json(orient="records")),
        "rows_by_unit": json.loads(by_unit.to_json(orient="records")),
        "unmatched_areas": unmatched_areas,
        "total_areas_matched": int(areas["ruc_label"].notna().sum()),
        "total_areas_unmatched": len(unmatched_areas),
    }


def accepted_group_points() -> dict:
    """One point per accepted group with both members and posts_a_month
    recorded — for a scatter plot (members vs. posting activity), colored
    by rural/urban classification. Unlike group_stats_by_area_type's
    averages, a group missing either value just can't be plotted at all
    (not "counts as zero") — excluded from "points" and counted in
    "excluded_no_data" instead, so the chart doesn't quietly imply data
    that was never actually recorded.
    """
    empty = {
        "points": [], "excluded_no_data": 0,
        "unmatched_areas": [], "total_areas_matched": 0, "total_areas_unmatched": 0,
    }
    if not GROUP_LOG_PATH.exists():
        return empty

    log = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    if log.empty:
        return empty

    pcon_lookup = _constituency_ruc_lookup()
    la_lookup = _local_authority_ruc_lookup()

    log["ruc_label"] = [
        _resolve_ruc_label(at, an, pcon_lookup, la_lookup)
        for at, an in zip(log["area_type"], log["area_name"])
    ]

    areas = log[["area_type", "area_name", "ruc_label"]].drop_duplicates()
    unmatched_areas = sorted(
        f"{r.area_name} ({r.area_type})" for r in areas[areas["ruc_label"].isna()].itertuples()
    )

    matched = log[(log["accepted"] == "Y") & log["ruc_label"].notna()].copy()
    matched["members"] = pd.to_numeric(matched["members"], errors="coerce")
    matched["posts_a_month"] = pd.to_numeric(matched["posts_a_month"], errors="coerce")

    plottable = matched.dropna(subset=["members", "posts_a_month"])
    excluded_no_data = len(matched) - len(plottable)

    points = [
        {
            "group": r.group,
            "area_name": r.area_name,
            "area_type_label": r.ruc_label,
            "members": r.members,
            "posts_a_month": r.posts_a_month,
        }
        for r in plottable.itertuples()
    ]

    return {
        "points": points,
        "excluded_no_data": int(excluded_no_data),
        "unmatched_areas": unmatched_areas,
        "total_areas_matched": int(areas["ruc_label"].notna().sum()),
        "total_areas_unmatched": len(unmatched_areas),
    }


def accepted_averages_by_area() -> dict:
    """One point per *area* (not per group, and not per RUC label — see
    accepted_group_points and group_stats_by_area_type for those) — each
    area's own average member count and average posting activity across
    its accepted groups, labeled by rural/urban classification. Answers
    "which areas have unusually large/active groups", at area granularity,
    rather than "what's the spread within a rural/urban category" (the
    other scatter) or "what's the category-wide average" (the table).

    An area contributes a point only if at least one of its accepted
    groups has both members and posts_a_month recorded — same "can't
    average what was never measured" reasoning as accepted_group_points.
    """
    empty = {"areas": [], "unmatched_areas": [], "total_areas_matched": 0, "total_areas_unmatched": 0}
    if not GROUP_LOG_PATH.exists():
        return empty

    log = pd.read_csv(GROUP_LOG_PATH, dtype=str, encoding="utf-8", encoding_errors="surrogatepass").fillna("")
    if log.empty:
        return empty

    pcon_lookup = _constituency_ruc_lookup()
    la_lookup = _local_authority_ruc_lookup()

    log["ruc_label"] = [
        _resolve_ruc_label(at, an, pcon_lookup, la_lookup)
        for at, an in zip(log["area_type"], log["area_name"])
    ]

    areas_all = log[["area_type", "area_name", "ruc_label"]].drop_duplicates()
    unmatched_areas = sorted(
        f"{r.area_name} ({r.area_type})" for r in areas_all[areas_all["ruc_label"].isna()].itertuples()
    )

    matched = log[(log["accepted"] == "Y") & log["ruc_label"].notna()].copy()
    matched["members"] = pd.to_numeric(matched["members"], errors="coerce")
    matched["posts_a_month"] = pd.to_numeric(matched["posts_a_month"], errors="coerce")
    plottable = matched.dropna(subset=["members", "posts_a_month"])

    if plottable.empty:
        return {
            "areas": [], "unmatched_areas": unmatched_areas,
            "total_areas_matched": int(areas_all["ruc_label"].notna().sum()),
            "total_areas_unmatched": len(unmatched_areas),
        }

    grouped = (
        plottable.groupby(["area_type", "area_name", "ruc_label"])
        .agg(
            num_groups=("group_url", "count"),
            avg_members=("members", "mean"),
            avg_posts_a_month=("posts_a_month", "mean"),
        )
        .reset_index()
        .rename(columns={"ruc_label": "area_type_label"})
    )
    grouped["avg_members"] = grouped["avg_members"].round(1)
    grouped["avg_posts_a_month"] = grouped["avg_posts_a_month"].round(1)

    return {
        "areas": json.loads(grouped.to_json(orient="records")),
        "unmatched_areas": unmatched_areas,
        "total_areas_matched": int(areas_all["ruc_label"].notna().sum()),
        "total_areas_unmatched": len(unmatched_areas),
    }
