#!/usr/bin/env python3
"""One-off: generate search targets for a handful of specific electoral wards,
instead of a full constituency.

This is NOT wired into batch_pipeline.sh or the documented constituency
workflow — it's a bespoke tool for running a few named wards by hand. Input is
a small hand-written CSV of (ward, constituency, local authority). Two
sources combine per ward, split by what each is actually good at:

- ward_geodata.py queries OpenStreetMap (via Overpass) for highstreets and
  the biggest residential roads (length-filtered — real, verified data, but
  no sense of "would someone name a Facebook group after this street").
- A web-search-grounded LLM is asked only for colloquial/informal area names
  and landmarks people might personally anchor a community group around —
  the judgment call OSM tags can't make.

Each ward gets its OWN search-targets file — one row per Overpass/LLM item,
one file per ward, named the same way generate_search.py names a
constituency's file (slugify(ward_name)). This mirrors the constituency
pattern deliberately: sync_scrape.sh pushes each ward to its own remote
folder, so pushing a newly-added ward never touches (and can't silently
overwrite the scraped progress of) a ward already pushed and scraped — which
a single shared file/folder for all wards could not guarantee.

A rerun skips any ward whose file already exists (no per-ward LLM re-spend
just because you added a new ward to wards_file) — pass --force to
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
from pathlib import Path

import pandas as pd

from libby_core import ai
from uk import ward_geodata
from uk.generate_search import slugify
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

# The LLM is only asked for these — "highstreet"/"residential_road" are
# geo-sourced only now (ward_geodata.py), not requested from the LLM, though
# still valid on rows the geodata step produces.
VALID_TYPES = {"colloquial_name", "place_of_worship", "park", "school", "other_landmark"}
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

    We are trying to find real, existing Facebook community groups for this
    ward. Facebook groups get built around things people anchor their sense
    of place to — not every street or building has one, only places that
    give an area its identity. Roads and shopping streets are covered
    separately; focus only on these two categories:

    - Colloquial or informal names for this ward or parts of it — nicknames,
      sub-areas, or names locals actually use day-to-day that might not
      appear on an official map. Skip this if the ward has no informal name
      distinct from its official one.
    - Landmarks residents might genuinely identify with or name a community
      group after — a place of worship, a school, a park or green space, a
      community centre, a notable piece of public art, a well-known local
      building. The test is "would someone anchor a Facebook group around
      this," not "is this a real building in the ward." Do not force an
      answer if nothing stands out — only include something genuinely
      notable, not generic filler.

    Rules:
    - Only include real places/names you can verify exist in this ward. Do
      not invent plausible-sounding ones.
    - If you are not fully certain something is correct, still include it but
      mark it "low" confidence rather than omitting it.
    - Use the names local residents actually use, not official/administrative
      names where they differ.
    - Do not include the ward or constituency name itself as an item.

    Respond and only respond with a JSON list of objects with "name", "type" (one of
    "colloquial_name", "place_of_worship", "park", "school",
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


def _merge_items(geo_items: list[dict], llm_items: list[dict]) -> list[dict]:
    """Union geodata + LLM items, preferring the geodata (verified, "high"
    confidence) version when both found the same name."""
    geo_names = {g["name"].casefold() for g in geo_items}
    return geo_items + [item for item in llm_items if item["name"].casefold() not in geo_names]


def run(
    wards_file: Path,
    model: str = DEFAULT_MODEL,
    min_confidence: str = "low",
    force: bool = False,
    boundaries_path: Path | None = WARD_BOUNDARIES_PATH,
) -> dict[str, list[str]]:
    """Generate one search-targets file per ward. Returns
    {"written": [...], "skipped": [...], "failed": [...]} ward names."""
    wards_df = pd.read_csv(wards_file)
    wards_df.columns = wards_df.columns.str.strip()  # tolerate "name, other" headers
    for col in ("ward_name", "constituency_name"):
        if col not in wards_df.columns:
            logger.error("%s missing required column '%s'", wards_file, col)
            return {"written": [], "skipped": [], "failed": []}
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

    written, skipped, failed = [], [], []

    for _, ward in wards_df.iterrows():
        ward_name = str(ward["ward_name"]).strip()
        constituency_name = str(ward["constituency_name"]).strip()
        local_authority = str(ward.get("local_authority", "") or "").strip()

        ward_slug = slugify(ward_name)
        output_path = WARD_SEARCH_TARGETS_DIR / f"{ward_slug}_search_targets.csv"

        if not force and output_path.exists():
            logger.info("Skipping (already generated): %s — pass --force to regenerate", ward_name)
            skipped.append(ward_name)
            continue

        if force and _would_clobber_scraped_data(output_path):
            logger.error(
                "%s already contains scraped 'groups' data — refusing to overwrite %s. "
                "Pull results first if you need them, or delete the file to force regeneration.",
                ward_name, output_path,
            )
            failed.append(ward_name)
            continue

        match = constituencies_df[constituencies_df["PCON24NM"] == constituency_name]
        if match.empty:
            logger.error(
                "Skipping '%s': constituency '%s' not found in %s",
                ward_name, constituency_name, CONSTITUENCIES_PATH,
            )
            failed.append(ward_name)
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
            logger.warning("No items found for '%s' (geodata or LLM) — nothing written", ward_name)
            failed.append(ward_name)
            continue

        county = ward_geodata.encode_county(ward_name, local_authority)

        rows = []
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
            rows.append(row)

        if not rows:
            logger.warning("Everything for '%s' fell below --min-confidence — nothing written", ward_name)
            failed.append(ward_name)
            continue

        result = pd.DataFrame(rows)
        result.to_csv(output_path, index=False, encoding="utf-8", errors="surrogatepass")
        logger.info("  Wrote %d search-target rows for %s → %s", len(result), ward_name, output_path)
        written.append(ward_name)

    logger.info(
        "Done: %d written, %d skipped (already generated), %d failed",
        len(written), len(skipped), len(failed),
    )
    return {"written": written, "skipped": skipped, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description="Generate one search-targets file per ward, for a handful of named wards")
    parser.add_argument("--wards-file", default=str(DEFAULT_WARDS_FILE), help=f"CSV with ward_name, constituency_name, [local_authority] columns (default: {DEFAULT_WARDS_FILE})")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model (default: web-search-grounded)")
    parser.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low", help="Drop items below this confidence (default: low, i.e. keep everything)")
    parser.add_argument("--force", action="store_true", help="Regenerate every ward (ignoring the already-generated skip) and overwrite even if a ward's file already has scraped 'groups' data")
    parser.add_argument("--boundaries", default=str(WARD_BOUNDARIES_PATH), help=f"Ward boundary shapefile path (default: {WARD_BOUNDARIES_PATH})")
    parser.add_argument("--skip-geodata", action="store_true", help="Skip the boundary/Overpass lookup, LLM only")
    args = parser.parse_args()

    run(
        wards_file=Path(args.wards_file),
        model=args.model,
        min_confidence=args.min_confidence,
        force=args.force,
        boundaries_path=None if args.skip_geodata else Path(args.boundaries),
    )


if __name__ == "__main__":
    main()
