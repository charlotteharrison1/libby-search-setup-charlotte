#!/usr/bin/env python3
"""Custom-region pipeline: process an informal, multi-constituency area
("North London", "Greater Manchester") with no boundary file and no scrape
of its own.

Companion to uk/pipeline.py and uk/pipeline_ward.py, but for regions defined
purely as a set of constituencies (uk/region_membership.py resolves and
caches which ones via a web-search-grounded LLM lookup — see that module).
Deliberately NOT a new scrape target: a region reuses each member
constituency's own already-scraped data via
uk.pipeline.reconstruct_constituency_candidates (read-only, no re-scrape, no
extra LLM cost for that step), so there is nothing to generate/push/pull for
a region itself — only "run pipeline" applies.

No locality filter: uk.pipeline.run() filters `combined["locality"] != "X"`,
but "X" never actually appears there — _aggregate_new_groups hardcodes every
directly-found group's locality to "C" and the geo add-on hardcodes its own
to "A" (the L/R/X classification in uk/geo.py is used only internally, to
pick which neighbouring groups qualify as add-on donors). That filter is a
no-op in both existing pipelines already, not something this module needs to
special-case.

The real safety net against "too broad for this region" is the AI
assessment stage (see CUSTOM_AREA_PROMPT_TEMPLATE below) — it judges against
the full composite region description (uk/region_context.py), which grows
with the region, so "too broad" tracks the region's own actual scale.

Run from the repository root as a module:

    python -m uk.pipeline_custom --area "North London"
    python -m uk.pipeline_custom --area "North London" --about
    python -m uk.pipeline_custom --area "North London" --force-membership
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path

import pandas as pd

from libby_core import assessment
from uk import about_context, local_about_scraper, region_context, region_membership
from uk.pipeline import (
    _apply_ai_verdict,
    _apply_exclusions,
    _drop_buy_sell,
    _drop_unsure_churches,
    _dropped_rows,
    _excluded_ledger_rows,
    _kept_rows,
    _load_overrides_by_area,
    _missing_override_rows,
    _recovered_ledger_rows,
    _upsert_group_log,
    reconstruct_constituency_candidates,
)
from uk.settings import ABOUT_PAGES_DIR, CLACTON_INPUTS_DIR, CUSTOM_OUTPUT_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

AREA_TYPE = "custom"
AREA_KIND = region_context.REGION_AREA_KIND

# Same quality bar as uk/pipeline.py's constituency defaults — a custom
# region is constituency-scale or larger, not hyperlocal like a ward.
DEFAULT_MIN_MEMBERS = 50
DEFAULT_MIN_POSTS_A_MONTH = 10

# Exact column set uk/pipeline.py's constituency output writes — same
# rationale as uk/pipeline_ward.py's FINAL_COLUMNS: matching it exactly
# means groups_{Region}.csv drops straight into Clacton-etc/groups/ with no
# reformatting. locality/PCON24NM here are each row's OWN originating
# constituency (or its add-on donor), not the region's — same convention
# uk.pipeline._missing_override_rows already documents for ward rows.
FINAL_COLUMNS = [
    "PCON24CD", "name", "url", "public_y_n", "members", "posts_a_month",
    "locality", "locality_name", "PCON24NM", "first_assessment",
]

# Same as libby_core.assessment.DEFAULT_PROMPT_TEMPLATE, with only the
# illustrative "too broad" example swapped for one at region scale — the
# rest of the rule (judge against {area_description}/{area_kind} as given)
# is unchanged, so this is a strictly regional-flavoured variant, not a
# looser one. Passed explicitly to assess_groups() via its own
# prompt_template parameter; libby_core/assessment.py itself is untouched.
CUSTOM_AREA_PROMPT_TEMPLATE = (
    "You are assessing whether a Facebook group is genuinely local to a specific area. "
    "The area is: <area>{area_description}</area> ({area_kind}). "
    "The Facebook group name is: '{group_name}'. "
    "{about_context}"
    "Answer 'Yes' only if the group is clearly local to that specific area — "
    "for example a neighbourhood group, local events, local sport, local services, or local community "
    "anywhere within the region described above. "
    "Answer 'No' if the group covers a broader area than the region described "
    "(e.g. a UK-wide or nationwide group), or a different area entirely "
    "(e.g. a group for a part of the country not covered by this region). "
    "Answer 'No' if the group is linked to a different area entirely, or is international. "
    "Answer 'Unsure' if the group name gives ambiguous signals about its geographic scope. "
    "When in doubt, prefer 'Unsure' over 'No' — these results undergo human review. "
    "Only answer with the single word: 'Yes', 'Unsure', or 'No'."
)

def _pool_member_candidates(area_name: str, member_constituencies: list[str]) -> pd.DataFrame:
    """Rebuild the full pre-filter candidate pool for a region by pooling
    each member constituency's own reconstruct_constituency_candidates()
    result — no re-scrape, no LLM calls for this step. A constituency with
    no scraped file yet is skipped (logged), not fatal — a region can still
    run partially, same tolerance uk.generate_search_ward.py's --geodata-only
    already applies to ("resume, don't require everything at once")."""
    parts = []
    for pcon24nm in member_constituencies:
        try:
            candidates = reconstruct_constituency_candidates(pcon24nm)
        except FileNotFoundError:
            logger.warning(
                "  %s: no scraped data yet for member constituency '%s' — skipping it this run",
                area_name, pcon24nm,
            )
            continue
        if not candidates.empty:
            parts.append(candidates)
    if not parts:
        return pd.DataFrame()
    pooled = pd.concat(parts, axis=0, ignore_index=True)
    # Each member's own candidates arrive pre-sorted (by members/posts —
    # see uk.pipeline._combine), but concatenating several already-sorted
    # blocks does NOT produce a globally-sorted frame. A group found via
    # more than one member constituency (its own scrape, or another
    # member's geo add-on) needs a real global sort here so the later
    # `drop_duplicates(subset=["url"])` in run() actually keeps the
    # higher-ranked occurrence, as its ledger reason claims, rather than
    # whichever member happened to come first in the loop above.
    return pooled.sort_values(by=["members", "posts_a_month"], ascending=False, na_position="last")


def reconstruct_custom_candidates(area_name: str) -> pd.DataFrame:
    """Rebuild the full pre-filter candidate pool for one custom region,
    straight from each member constituency's own scraped file — no LLM
    calls, no re-scraping, read-only. Same rationale/use as
    uk.pipeline.reconstruct_constituency_candidates and
    uk.pipeline_ward.reconstruct_ward_candidates: uk/recover_group.py uses
    this to pull a specific rejected group's full row back out after the
    fact.

    Raises RuntimeError if the region has no cached membership yet (nothing
    to recover into — run uk.pipeline_custom for it at least once first)."""
    members = region_membership.resolve_membership(area_name, force=False)
    if not members:
        raise RuntimeError(
            f"No cached constituency membership for region {area_name!r} — "
            "run uk.pipeline_custom for it at least once before recovering a group"
        )
    return _pool_member_candidates(area_name, members).reset_index(drop=True)


def run(
    area_names: list[str],
    force_membership: bool = False,
    force_overview: bool = False,
    stop_before_ai_assessment: bool = False,
    min_members: int = DEFAULT_MIN_MEMBERS,
    min_posts_a_month: float = DEFAULT_MIN_POSTS_A_MONTH,
    use_context: bool = True,
    use_about: bool = False,
    about_limit: int = local_about_scraper.DEFAULT_MAX_GROUPS,
) -> pd.DataFrame:
    use_context = use_context or use_about

    about_lookup = about_context.load_global_about_context(ABOUT_PAGES_DIR) if use_context else {}
    overrides_by_area = _load_overrides_by_area(AREA_TYPE)

    all_final = []
    for area_name in area_names:
        logger.info("Processing custom region: %s", area_name)

        members = region_membership.resolve_membership(area_name, force=force_membership)
        if not members:
            logger.error("  No constituencies resolved for '%s' — skipping", area_name)
            continue
        logger.info("  %d member constituencies: %s", len(members), members)

        agg = _pool_member_candidates(area_name, members)
        if agg.empty:
            logger.warning("  No candidate groups found for '%s' (no member constituency scraped yet)", area_name)
            _upsert_group_log(AREA_TYPE, area_name, [])
            continue
        logger.info("  %d pooled candidate groups (pre-filter, pre-dedupe)", len(agg))

        agg = agg.reset_index(drop=True)
        agg["_row_id"] = agg.index
        ledger: list[dict] = []

        before = agg
        combined = agg[agg.public_y_n == True].copy()  # noqa: E712
        ledger += _dropped_rows(
            before, combined, AREA_TYPE, area_name, "public_filter",
            "Not marked public (private, or public/private unknown)",
        )

        before = combined
        combined = combined.drop_duplicates(subset=["url"])
        ledger += _dropped_rows(
            before, combined, AREA_TYPE, area_name, "dedupe",
            "Duplicate group url across member constituencies (kept the higher-ranked occurrence)",
        )

        before = combined
        combined = _drop_buy_sell(combined)
        ledger += _dropped_rows(
            before, combined, AREA_TYPE, area_name, "buy_sell",
            "Group name matched buy/sell/marketplace pattern",
        )

        before = combined
        combined = combined[
            combined["posts_a_month"].isna() | (combined["posts_a_month"] >= min_posts_a_month)
        ].copy()
        ledger += _dropped_rows(
            before, combined, AREA_TYPE, area_name, "activity_filter",
            f"Posts per month below threshold (< {min_posts_a_month})",
        )

        if stop_before_ai_assessment:
            out_path = CUSTOM_OUTPUT_DIR / f"{area_name}-intermediate.csv"
            combined.drop(columns=["_row_id"]).to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
            logger.info("  Saved %d rows (pre-assessment) → %s", len(combined), out_path)
            _upsert_group_log(AREA_TYPE, area_name, ledger)
            continue

        if combined.empty:
            logger.info("  Nothing left to assess for %s", area_name)
            _upsert_group_log(AREA_TYPE, area_name, ledger)
            continue

        desc = region_context.build_composite_description(area_name, members, force_overview=force_overview)
        if not desc:
            logger.warning("  No composite description for %s — using empty string for assessment", area_name)

        context_column = None
        if about_lookup:
            combined["about_context"] = combined["url"].map(about_lookup)
            n_matched = combined["about_context"].notna().sum()
            logger.info("  %d/%d groups matched About context for %s", n_matched, len(combined), area_name)
            context_column = "about_context"

        assessed = assessment.assess_groups(
            df=combined, area_description=desc, area_kind=AREA_KIND,
            context_column=context_column, prompt_template=CUSTOM_AREA_PROMPT_TEMPLATE,
        )
        combined["first_assessment"] = assessed.get("first_assessment")

        about_reassessed_ids: set = set()

        # ── --about: escalate on uncertainty, inline, this same run ──────────
        # Identical mechanism to uk/pipeline.py's and uk/pipeline_ward.py's own
        # block — see uk/local_about_scraper.py's module docstring.
        if use_about and not combined.empty:
            is_unsure = combined["first_assessment"].astype(str).str.strip().str.casefold() == "unsure"
            has_context = (
                combined[context_column].notna()
                if context_column and context_column in combined.columns
                else pd.Series(False, index=combined.index)
            )
            to_scrape = combined[is_unsure & ~has_context]

            if to_scrape.empty:
                logger.info("  --about: nothing left to scrape for %s (no uncached Unsure groups)", area_name)
            else:
                urls = to_scrape["url"].tolist()
                names = dict(zip(to_scrape["url"], to_scrape["name"]))
                logger.info("  --about: %d Unsure, uncached group(s) to scrape locally for %s", len(urls), area_name)
                try:
                    scraped_df = local_about_scraper.scrape_about_locally(
                        urls, name_by_url=names, max_groups=about_limit,
                    )
                except (local_about_scraper.LocalAboutScraperUnavailable, RuntimeError) as e:
                    logger.warning("  --about unavailable this run (%s) — continuing without it", e)
                    scraped_df = pd.DataFrame()

                if not scraped_df.empty:
                    scraped_path = local_about_scraper.write_local_about(
                        scraped_df, area_name.lower().replace(" ", "_"), out_dir=ABOUT_PAGES_DIR,
                    )
                    new_context = about_context.load_about_context(scraped_path)
                    about_lookup.update(new_context)

                    if "about_context" not in combined.columns:
                        combined["about_context"] = pd.NA
                    newly_matched = combined["url"].isin(new_context)
                    combined.loc[newly_matched, "about_context"] = combined.loc[newly_matched, "url"].map(new_context)
                    context_column = "about_context"

                    reassess_rows = combined[newly_matched]
                    logger.info("  Re-assessing %d group(s) with fresh About context for %s", len(reassess_rows), area_name)
                    reassessed = assessment.assess_groups(
                        df=reassess_rows, area_description=desc, area_kind=AREA_KIND,
                        context_column=context_column, prompt_template=CUSTOM_AREA_PROMPT_TEMPLATE,
                    )
                    combined.loc[newly_matched, "first_assessment"] = reassessed["first_assessment"].values
                    about_reassessed_ids |= set(combined.loc[newly_matched, "_row_id"])

        before = combined
        final, ai_reason = _apply_ai_verdict(before, context_column, about_reassessed_ids)
        ledger += _dropped_rows(before, final, AREA_TYPE, area_name, "ai_assessment", ai_reason)

        before = final
        final = final[final["members"].fillna(0) > min_members].copy()
        ledger += _dropped_rows(
            before, final, AREA_TYPE, area_name, "members_filter", f"Member count <= {min_members}",
        )

        before = final
        final = _drop_unsure_churches(final, area_name)
        ledger += _dropped_rows(
            before, final, AREA_TYPE, area_name, "church_heuristic",
            'Assessed "Unsure" + church-pattern name, no area-name match',
        )

        ledger += _kept_rows(final, AREA_TYPE, area_name, about_reassessed_ids)

        area_overrides = overrides_by_area.get(area_name, {"include": set(), "exclude": set()})

        recovered = _missing_override_rows(final, agg, area_overrides["include"])
        if not recovered.empty:
            logger.info("  Re-applied %d manually-recovered group(s) for %s", len(recovered), area_name)
            ledger += _recovered_ledger_rows(recovered, AREA_TYPE, area_name)
            final = pd.concat([final, recovered], ignore_index=True)

        final, excluded = _apply_exclusions(final, area_overrides["exclude"])
        if not excluded.empty:
            logger.info("  Re-applied %d manually-removed group(s) for %s", len(excluded), area_name)
            ledger += _excluded_ledger_rows(excluded, AREA_TYPE, area_name)

        _upsert_group_log(AREA_TYPE, area_name, ledger)

        final = final.drop(columns=["_row_id", "about_context"], errors="ignore")
        final = final.sort_values(by=["members", "posts_a_month"], ascending=False, na_position="last")
        final = final.reindex(columns=FINAL_COLUMNS)

        out_path = CUSTOM_OUTPUT_DIR / f"groups_{area_name}.csv"
        final.to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
        logger.info("  Saved %d rows → %s", len(final), out_path)

        inputs_custom_dir = CLACTON_INPUTS_DIR / "custom"
        inputs_custom_dir.mkdir(parents=True, exist_ok=True)
        staged_path = inputs_custom_dir / out_path.name
        shutil.move(str(out_path), str(staged_path))
        logger.info("  Moved final result → %s", staged_path)

        all_final.append(final)

    if not all_final:
        return pd.DataFrame()

    combined_out = pd.concat(all_final, axis=0, ignore_index=True).drop_duplicates(subset=["url"])
    combined_path = CUSTOM_OUTPUT_DIR / "custom_output.csv"
    combined_out.to_csv(combined_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("Saved %d total rows → %s", len(combined_out), combined_path)
    return combined_out


def main():
    parser = argparse.ArgumentParser(description="Process one or more custom, multi-constituency regions")
    parser.add_argument("--area", action="append", dest="areas", required=True, help='Region name, e.g. "North London" — repeatable')
    parser.add_argument("--force-membership", action="store_true", help="Re-resolve region→constituency membership via the LLM even if already cached")
    parser.add_argument("--force-overview", action="store_true", help="Regenerate the region's own cached overview description")
    parser.add_argument("--stop-before-ai-assessment", action="store_true", help="Write pre-assessment intermediate CSVs and exit before AI assessment")
    parser.add_argument("--min-members", type=int, default=DEFAULT_MIN_MEMBERS, help=f"Minimum member count to keep a group (default: {DEFAULT_MIN_MEMBERS})")
    parser.add_argument("--min-posts-a-month", type=float, default=DEFAULT_MIN_POSTS_A_MONTH, help=f"Minimum posts/month to keep a group (default: {DEFAULT_MIN_POSTS_A_MONTH})")
    parser.add_argument("--no-context", action="store_true", help="Skip loading the global About-context cache (on by default)")
    parser.add_argument("--context", action="store_true", help="No-op, on by default — use --no-context to opt out")
    parser.add_argument("--about", action="store_true", help="Implies --context. About-scrape locally whatever is still 'Unsure' and uncached, then re-assess just those groups")
    parser.add_argument("--about-limit", type=int, default=local_about_scraper.DEFAULT_MAX_GROUPS, help=f"Max groups to About-scrape locally per region with --about (default: {local_about_scraper.DEFAULT_MAX_GROUPS})")
    args = parser.parse_args()

    result = run(
        area_names=args.areas,
        force_membership=args.force_membership,
        force_overview=args.force_overview,
        stop_before_ai_assessment=args.stop_before_ai_assessment,
        min_members=args.min_members,
        min_posts_a_month=args.min_posts_a_month,
        use_context=not args.no_context,
        use_about=args.about,
        about_limit=args.about_limit,
    )
    if result.empty and not args.stop_before_ai_assessment:
        sys.exit(1)


if __name__ == "__main__":
    main()
