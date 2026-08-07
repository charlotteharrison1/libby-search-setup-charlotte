#!/usr/bin/env python3
"""One-off: generate search targets for a handful of specific electoral wards,
instead of a full constituency.

This is NOT wired into batch_pipeline.sh or the documented constituency
workflow — it's a bespoke tool for running a few named wards by hand. Input is
a small hand-written CSV of (ward, constituency, local authority); for each
ward it asks a web-search-grounded LLM for highstreets, notable residential
roads, places of worship, parks, and schools, then writes rows in the exact
same schema uk/generate_search.py produces — so the rest of the pipeline
(sync_scrape.sh push, the external scraper, uk.pipeline --input ...) needs no
changes at all.

    python -m uk.generate_search_ward --wards-file uk/data/reference/adhoc_wards.csv

See uk/data/reference/adhoc_wards.csv.example for the input format.
"""

import argparse
import ast
import json
import logging
import re
import sys
from pathlib import Path

import pandas as pd

from libby_core import ai
from uk.settings import CONSTITUENCIES_PATH, SEARCH_TARGETS_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Web-search-grounded model (OpenRouter's ":online" suffix bolts a search
# plugin onto the base model) — the whole point is to answer from a live
# lookup of the ward, not the model's parametric memory.
DEFAULT_MODEL = "openai/gpt-5:online"
DEFAULT_MAX_TOKENS = 20_000  # GPT-5 spends tokens on hidden reasoning + the
# :online search round trip before any visible content — generate_search.py
# uses the same budget for the same model; too low and message.content comes
# back None (looks like a crash, is actually silent truncation).
DEFAULT_OUTPUT = SEARCH_TARGETS_DIR / "adhoc_wards_search_targets.csv"

VALID_TYPES = {"highstreet", "residential_road", "place_of_worship", "park", "school", "other_landmark"}
SCROLL_BY_TYPE = {"highstreet": 3}  # everything else falls back to DEFAULT_SCROLL
DEFAULT_SCROLL = 2
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def build_prompt(ward_name: str, constituency_name: str, local_authority: str) -> str:
    where = f'the electoral ward "{ward_name}"'
    if local_authority:
        where += f", in {local_authority}"
    where += f", part of the UK parliamentary constituency of {constituency_name}"

    return f"""
    You are researching {where}.

    Search for real, current information about this specific ward. List places
    that local residents would plausibly search for on Facebook to find a
    community group, in these categories:

    - High streets or shopping streets (the main commercial street(s) in the ward)
    - Named residential roads or streets that are locally well-known (not every
      street — only ones a resident might use as a neighbourhood identifier)
    - Places of worship of any faith (churches, mosques, temples, synagogues, etc.)
    - Parks or other named green spaces
    - Schools (state or independent)
    - Anything else locally distinctive that residents might identify their
      area by or search for on Facebook — a notable hotel or pub, a piece of
      public art or statue, a market, a community centre, a well-known local
      landmark or building. Do not force an answer here if nothing stands
      out — only include something genuinely notable, not a generic filler.

    Rules:
    - Only include real places you can verify exist in this ward. Do not invent
      plausible-sounding names.
    - If you are not fully certain a place is correct, still include it but mark
      it "low" confidence rather than omitting it.
    - Use the names local residents actually use, not official/administrative
      names where they differ.
    - Do not include the ward or constituency name itself as an item.

    Respond and only respond with a JSON list of objects with "name", "type" (one of
    "highstreet", "residential_road", "place_of_worship", "park", "school",
    "other_landmark"), and "confidence" ("high", "medium", or "low") keys:

    [{{"name": "...", "type": "...", "confidence": "..."}}, ...]
    """


def _parse_ward_items(value: str) -> list[dict]:
    """Parse the LLM's JSON reply into a list of validated item dicts.
    Tolerates a ```json fenced reply and drops malformed/unknown-type rows."""
    if not isinstance(value, str) or not value.strip():
        return []
    text = re.sub(r"^```(?:json)?|```$", "", value.strip(), flags=re.MULTILINE).strip()

    parsed = None
    for parser in (json.loads, ast.literal_eval):
        try:
            candidate = parser(text)
            if isinstance(candidate, list):
                parsed = candidate
                break
        except (ValueError, SyntaxError):
            continue
    if not parsed:
        return []

    items = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        item_type = str(item.get("type", "")).strip()
        confidence = str(item.get("confidence", "")).strip().lower()
        if not name or item_type not in VALID_TYPES or confidence not in CONFIDENCE_RANK:
            continue
        items.append({"name": name, "type": item_type, "confidence": confidence})
    return items


def _would_clobber_scraped_data(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        existing = pd.read_csv(path)
    except Exception:
        return False
    return "groups" in existing.columns and existing["groups"].notna().any()


def run(
    wards_file: Path,
    output_path: Path = DEFAULT_OUTPUT,
    model: str = DEFAULT_MODEL,
    min_confidence: str = "low",
    force: bool = False,
) -> pd.DataFrame:
    if not force and _would_clobber_scraped_data(output_path):
        logger.error(
            "%s already contains scraped 'groups' data. Refusing to overwrite; "
            "pass --force or --output to write elsewhere.", output_path,
        )
        sys.exit(1)

    wards_df = pd.read_csv(wards_file)
    for col in ("ward_name", "constituency_name"):
        if col not in wards_df.columns:
            logger.error("%s missing required column '%s'", wards_file, col)
            sys.exit(1)
    if "local_authority" not in wards_df.columns:
        wards_df["local_authority"] = ""

    constituencies_df = pd.read_csv(CONSTITUENCIES_PATH)
    min_rank = CONFIDENCE_RANK[min_confidence]

    all_rows = []
    for _, ward in wards_df.iterrows():
        ward_name = str(ward["ward_name"]).strip()
        constituency_name = str(ward["constituency_name"]).strip()
        local_authority = str(ward.get("local_authority", "") or "").strip()

        match = constituencies_df[constituencies_df["PCON24NM"] == constituency_name]
        if match.empty:
            logger.error(
                "Skipping '%s': constituency '%s' not found in %s",
                ward_name, constituency_name, CONSTITUENCIES_PATH,
            )
            continue
        constituency_row = match.iloc[0].to_dict()

        logger.info("Querying: %s (%s)", ward_name, constituency_name)
        prompt = build_prompt(ward_name, constituency_name, local_authority)
        try:
            reply = ai.get_llm_text_response(prompt, model=model, max_tokens=DEFAULT_MAX_TOKENS)
        except Exception as e:
            logger.error("LLM call failed for '%s': %s", ward_name, e)
            continue

        items = _parse_ward_items(reply)
        if not items:
            logger.warning("No parseable items for '%s'", ward_name)
            continue

        county = f"{ward_name}, {local_authority}" if local_authority else ward_name

        for item in items:
            rank = CONFIDENCE_RANK[item["confidence"]]
            flag = "" if rank >= min_rank else "  [dropped: below min-confidence]"
            logger.info("  [%s/%s] %s%s", item["type"], item["confidence"], item["name"], flag)
            if rank < min_rank:
                continue

            row = dict(constituency_row)
            row["place_name"] = item["name"]
            row["county"] = county
            row["search_string"] = f"{item['name']}, {county}"
            row["scroll"] = SCROLL_BY_TYPE.get(item["type"], DEFAULT_SCROLL)
            row["processed"] = False
            row["groups"] = ""
            all_rows.append(row)

    if not all_rows:
        logger.error("No search targets generated — check wards file and constituency names")
        sys.exit(1)

    result = pd.DataFrame(all_rows)
    result.to_csv(output_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info(
        "Wrote %d search-target rows across %d wards → %s",
        len(result), wards_df["ward_name"].nunique(), output_path,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Generate search targets for a handful of named wards")
    parser.add_argument("--wards-file", required=True, help="CSV with ward_name, constituency_name, [local_authority] columns")
    parser.add_argument("--output", default=None, help=f"Output CSV path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model (default: web-search-grounded)")
    parser.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low", help="Drop items below this confidence (default: low, i.e. keep everything)")
    parser.add_argument("--force", action="store_true", help="Overwrite even if the output already has scraped 'groups' data")
    args = parser.parse_args()

    run(
        wards_file=Path(args.wards_file),
        output_path=Path(args.output) if args.output else DEFAULT_OUTPUT,
        model=args.model,
        min_confidence=args.min_confidence,
        force=args.force,
    )


if __name__ == "__main__":
    main()
