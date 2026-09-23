#!/usr/bin/env python3
"""Resolve which UK parliamentary constituencies fall within a custom,
informal region ("North London", "Greater Manchester") — no boundary file,
just a web-search-grounded LLM asked which real constituencies belong, with
its answer validated against the actual reference list so a hallucinated
name can never silently become a "member" of a region.

Resolved once per region and cached to CUSTOM_REGION_MEMBERSHIP_PATH (one
row per area_name/constituency pair) — every later uk.pipeline_custom run
reuses the cached list for free. Pass force=True to re-resolve.

    python -m uk.region_membership --area "North London"
    python -m uk.region_membership --area "North London" --force
"""

import argparse
import json
import logging
import re
from pathlib import Path

import pandas as pd

from libby_core import ai
from uk.settings import CONSTITUENCIES_PATH, CUSTOM_REGION_MEMBERSHIP_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ":online" bolts a web-search plugin onto the base model — same rationale
# as generate_search.py/generate_search_ward.py: membership of a fuzzy,
# informal region is a real-world lookup, not something to answer from the
# model's parametric memory alone.
DEFAULT_MODEL = "openai/gpt-5:online"
DEFAULT_MAX_TOKENS = 8_000


def build_prompt(area_name: str) -> str:
    return f"""
    You are an expert on UK Westminster parliamentary constituencies (2024
    boundaries) and how people informally group them into larger areas.

    List every UK parliamentary constituency that a well-informed local
    resident would say falls within, or substantially overlaps, the informal
    region "{area_name}".

    Rules:
    - Only include real constituencies using their official 2024 boundary
      review name (e.g. "Finchley and Golders Green", not "Finchley").
    - Do not invent or guess a plausible-sounding name — only include one you
      can verify is a real, current constituency.
    - Err on the side of the commonly understood extent of the region, not a
      maximal or minimal interpretation.

    Respond and only respond with a JSON list of constituency name strings:

    ["constituency name 1", "constituency name 2", ...]
    """


def _parse_constituency_list(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _load_cache() -> pd.DataFrame:
    if not CUSTOM_REGION_MEMBERSHIP_PATH.exists():
        return pd.DataFrame(columns=["area_name", "PCON24CD", "PCON24NM"])
    return pd.read_csv(CUSTOM_REGION_MEMBERSHIP_PATH, dtype=str).fillna("")


def _save_cache(df: pd.DataFrame) -> None:
    df.to_csv(CUSTOM_REGION_MEMBERSHIP_PATH, index=False)


def resolve_membership(area_name: str, force: bool = False, model: str = DEFAULT_MODEL) -> list[str]:
    """Return the list of member constituency names (PCON24NM) for
    `area_name`, from cache unless `force` (or nothing cached yet)."""
    cache = _load_cache()

    if not force:
        cached = cache[cache["area_name"] == area_name]
        if not cached.empty:
            return sorted(cached["PCON24NM"].unique().tolist())

    constituencies_df = pd.read_csv(CONSTITUENCIES_PATH)
    by_name = {name.strip().casefold(): name for name in constituencies_df["PCON24NM"].dropna()}
    code_by_name = dict(zip(constituencies_df["PCON24NM"], constituencies_df["PCON24CD"]))

    logger.info("Resolving constituency membership for region: %s", area_name)
    prompt = build_prompt(area_name)
    try:
        reply = ai.get_llm_text_response(prompt, model=model, max_tokens=DEFAULT_MAX_TOKENS)
    except Exception as e:
        logger.error("LLM call failed for region '%s': %s", area_name, e)
        return []

    candidates = _parse_constituency_list(reply)
    matched, unmatched = [], []
    for name in candidates:
        real_name = by_name.get(name.strip().casefold())
        (matched if real_name else unmatched).append(real_name or name)

    if unmatched:
        logger.warning(
            "Dropped %d name(s) for '%s' not found in %s: %s",
            len(unmatched), area_name, CONSTITUENCIES_PATH, unmatched,
        )
    if not matched:
        logger.error("No valid constituencies resolved for region '%s' — nothing cached", area_name)
        return []

    matched = sorted(set(matched))
    logger.info("Resolved %d constituencies for '%s': %s", len(matched), area_name, matched)

    cache = cache[cache["area_name"] != area_name]
    new_rows = pd.DataFrame({
        "area_name": area_name,
        "PCON24CD": [code_by_name.get(n, "") for n in matched],
        "PCON24NM": matched,
    })
    cache = pd.concat([cache, new_rows], ignore_index=True)
    _save_cache(cache)
    logger.info("Cached membership for '%s' → %s", area_name, CUSTOM_REGION_MEMBERSHIP_PATH)

    return matched


def known_areas() -> list[str]:
    """Every custom area with a cached membership resolution — used by the
    control centre to list custom areas it already knows about."""
    cache = _load_cache()
    if cache.empty:
        return []
    return sorted(cache["area_name"].unique().tolist())


def main():
    parser = argparse.ArgumentParser(description="Resolve which constituencies belong to a custom region")
    parser.add_argument("--area", required=True, help='Region name, e.g. "North London"')
    parser.add_argument("--force", action="store_true", help="Re-resolve even if already cached")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model for the membership lookup")
    args = parser.parse_args()

    members = resolve_membership(args.area, force=args.force, model=args.model)
    if members:
        print(f"{args.area}: {len(members)} constituencies")
        for name in members:
            print(f"  - {name}")
    else:
        print(f"{args.area}: no constituencies resolved")


if __name__ == "__main__":
    main()
