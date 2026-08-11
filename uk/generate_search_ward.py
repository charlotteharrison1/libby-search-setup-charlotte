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

A rerun skips any ward already present in the output file (no per-ward LLM
re-spend just because you added a new ward to wards_file) — pass --force to
regenerate everyone regardless.

    python -m uk.generate_search_ward
    python -m uk.generate_search_ward --wards-file uk/data/search_targets/adhoc_wards.csv

See uk/data/search_targets/adhoc_wards.csv.example for the input format.
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
from uk import ward_geodata
from uk.settings import CONSTITUENCIES_PATH, DEFAULT_WARDS_FILE, WARD_BOUNDARIES_PATH, WARD_SEARCH_TARGETS_DIR

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
DEFAULT_OUTPUT = WARD_SEARCH_TARGETS_DIR / "adhoc_wards_search_targets.csv"

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


def _load_existing_rows_by_ward(path: Path) -> dict[tuple[str, str], list[dict]]:
    """Group output_path's existing rows by (ward_name, local_authority),
    casefolded, so a rerun can skip regenerating a ward already in the file.
    Empty dict if the file doesn't exist yet or has no 'county' column."""
    if not Path(path).exists():
        return {}
    try:
        existing = pd.read_csv(path)
    except Exception:
        return {}
    if "county" not in existing.columns:
        return {}

    by_ward: dict[tuple[str, str], list[dict]] = {}
    for county, group in existing.groupby("county"):
        ward_name, local_authority = ward_geodata.split_county(county)
        key = (ward_name.casefold(), local_authority.casefold())
        by_ward[key] = group.to_dict("records")
    return by_ward


def _merge_items(geo_items: list[dict], llm_items: list[dict]) -> list[dict]:
    """Union geodata + LLM items, preferring the geodata (verified, "high"
    confidence) version when both found the same name."""
    geo_names = {g["name"].casefold() for g in geo_items}
    return geo_items + [item for item in llm_items if item["name"].casefold() not in geo_names]


def run(
    wards_file: Path,
    output_path: Path = DEFAULT_OUTPUT,
    model: str = DEFAULT_MODEL,
    min_confidence: str = "low",
    force: bool = False,
    boundaries_path: Path | None = WARD_BOUNDARIES_PATH,
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

    boundaries_gdf = None
    if boundaries_path:
        if Path(boundaries_path).exists():
            boundaries_gdf = ward_geodata.load_ward_boundaries(boundaries_path)
        else:
            logger.warning("Ward boundaries file not found at %s — skipping geodata, LLM-only", boundaries_path)

    # Skip wards already present in output_path from a previous run — no
    # per-ward memory otherwise, so appending new wards to wards_file would
    # silently regenerate (and re-spend LLM calls on) every ward already done.
    existing_by_ward = {} if force else _load_existing_rows_by_ward(output_path)

    all_rows = []
    for _, ward in wards_df.iterrows():
        ward_name = str(ward["ward_name"]).strip()
        constituency_name = str(ward["constituency_name"]).strip()
        local_authority = str(ward.get("local_authority", "") or "").strip()

        existing_key = (ward_name.casefold(), local_authority.casefold())
        if existing_key in existing_by_ward:
            logger.info("Skipping (already generated): %s — pass --force to regenerate", ward_name)
            all_rows.extend(existing_by_ward[existing_key])
            continue

        match = constituencies_df[constituencies_df["PCON24NM"] == constituency_name]
        if match.empty:
            logger.error(
                "Skipping '%s': constituency '%s' not found in %s",
                ward_name, constituency_name, CONSTITUENCIES_PATH,
            )
            continue
        constituency_row = match.iloc[0].to_dict()

        geo_items = []
        if boundaries_gdf is not None:
            geometry = ward_geodata.find_ward_geometry(boundaries_gdf, ward_name, local_authority)
            if geometry is not None:
                logger.info("Querying Overpass for: %s", ward_name)
                geo_items = ward_geodata.query_overpass_within(geometry)

        logger.info("Querying LLM for: %s (%s)", ward_name, constituency_name)
        prompt = build_prompt(ward_name, constituency_name, local_authority)
        try:
            reply = ai.get_llm_text_response(prompt, model=model, max_tokens=DEFAULT_MAX_TOKENS)
            llm_items = _parse_ward_items(reply)
        except Exception as e:
            logger.error("LLM call failed for '%s': %s", ward_name, e)
            llm_items = []

        items = _merge_items(geo_items, llm_items)
        if not items:
            logger.warning("No items found for '%s' (geodata or LLM)", ward_name)
            continue

        county = ward_geodata.encode_county(ward_name, local_authority)

        for item in items:
            rank = CONFIDENCE_RANK[item["confidence"]]
            flag = "" if rank >= min_rank else "  [dropped: below min-confidence]"
            source = "geo" if item in geo_items else "llm"
            logger.info("  [%s/%s/%s] %s%s", source, item["type"], item["confidence"], item["name"], flag)
            if rank < min_rank:
                continue

            row = dict(constituency_row)
            row["place_name"] = item["name"]
            row["county"] = county
            # Human-readable comma form here — only the 'county' field above
            # needs the safe " | " delimiter, since it's the one parsed back
            # apart later; this is just literal search text.
            row["search_string"] = f"{item['name']}, {ward_name}" + (f", {local_authority}" if local_authority else "")
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
    parser.add_argument("--wards-file", default=str(DEFAULT_WARDS_FILE), help=f"CSV with ward_name, constituency_name, [local_authority] columns (default: {DEFAULT_WARDS_FILE})")
    parser.add_argument("--output", default=None, help=f"Output CSV path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model (default: web-search-grounded)")
    parser.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low", help="Drop items below this confidence (default: low, i.e. keep everything)")
    parser.add_argument("--force", action="store_true", help="Regenerate every ward (ignoring the already-generated skip) and overwrite even if the output already has scraped 'groups' data")
    parser.add_argument("--boundaries", default=str(WARD_BOUNDARIES_PATH), help=f"Ward boundary shapefile path (default: {WARD_BOUNDARIES_PATH})")
    parser.add_argument("--skip-geodata", action="store_true", help="Skip the boundary/Overpass lookup, LLM only")
    args = parser.parse_args()

    run(
        wards_file=Path(args.wards_file),
        output_path=Path(args.output) if args.output else DEFAULT_OUTPUT,
        model=args.model,
        min_confidence=args.min_confidence,
        force=args.force,
        boundaries_path=None if args.skip_geodata else Path(args.boundaries),
    )


if __name__ == "__main__":
    main()
