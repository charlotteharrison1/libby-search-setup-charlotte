#!/usr/bin/env python3
"""Generate the UK search targets: for each constituency, ask the LLM for the
most popular place names, then explode them into the master scrape file.

This is the FIRST stage of the UK workflow — it decides *what to search for*.
The output (one row per constituency + place name, with an empty ``groups``
column) is what the Facebook scraper consumes; the scraper fills in ``groups``
and sets ``processed=True``, and only then does ``pipeline.py`` process it.

    generate_search.py  →  [external scrape]  →  pipeline.py

Run from the repository root as a module:

    python -m uk.generate_search                         # all constituencies
    python -m uk.generate_search --constituency Midlovian
    python -m uk.generate_search --output uk/data/new_targets.csv

By default it writes the master scrape file; to avoid clobbering scraped data it
refuses to overwrite a file that already contains a populated ``groups`` column
unless ``--force`` is given.
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
from uk.settings import CONSTITUENCIES_PATH, NEW_SCRAPE_PATH, SEARCH_TARGETS_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ":online" bolts a web-search plugin onto the base model, same as
# uk/generate_search_ward.py already uses — answers grounded in a live
# lookup rather than the model's parametric memory alone.
DEFAULT_MODEL = "openai/gpt-5:online"
DEFAULT_MAX_TOKENS = 20_000
RESPONSE_COLUMN = "list_of_towns_and_cities"


def build_prompt(row: pd.Series) -> str:
    return f"""
    You are an expert on UK places and communities.
    Your task is to list up to 25 place names for the UK parliamentary constituency: {row['PCON24NM']}

    These place names will be used to search Facebook for local community groups, so they must be names that local residents actually use to identify their area. Include a mix of:
    - Neighbourhoods, villages, towns, and city localities within the constituency
    - Well-known parks, commons, or green spaces that give their name to an area (e.g. "Clapham Common", "Hampstead Heath")
    - Landmarks or institutions that define a local community (e.g. a famous church, high street, or market)

    Rules:
    - For London constituencies: never include "London" on its own. Use specific locality names (e.g. "Finchley", "Golders Green", "Temple Fortune").
    - If a place name is not unique in the UK, qualify it (e.g. "Farringdon London", "Newport Shropshire").
    - Prioritise names people actually use day-to-day over official or administrative names.
    - Do not include the constituency name itself as a place.

    Respond and only respond with a JSON list of objects with "place", "county",
    "type", and "confidence" keys. "type" is a short category label (e.g.
    "locality", "park", "highstreet", "place_of_worship", "other_landmark" —
    use whatever short label best fits); "confidence" is "high", "medium", or
    "low" for how certain you are the name is real and locally used:

    [{{"place": "place1", "county": "county1", "type": "locality", "confidence": "high"}}, ...]
    """


def _parse_place_list(value) -> list[dict]:
    """Parse the LLM's response into a list of dicts with 'place', 'county',
    'type', and 'confidence' keys.

    type/confidence are metadata-only (same free-text tags the ward path
    records as target_type): a row is never dropped for missing or
    unrecognised values — they just default to "" — so tagging cannot change
    which places are generated. Only an empty 'place' drops a row, as before."""
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
            if not isinstance(parsed, list) or not parsed:
                continue
            # New format: list of dicts with place/county(/type/confidence) keys
            if isinstance(parsed[0], dict):
                items = []
                for p in parsed:
                    place = str(p.get("place", "")).strip()
                    if not place:
                        continue
                    confidence = str(p.get("confidence", "")).strip().lower()
                    items.append({
                        "place": place,
                        "county": str(p.get("county", "")).strip(),
                        "type": str(p.get("type", "")).strip(),
                        "confidence": confidence if confidence in ("high", "medium", "low") else "",
                    })
                return items
            # Old format: plain list of strings — carry forward without county
            return [
                {"place": str(p).strip(), "county": "", "type": "", "confidence": ""}
                for p in parsed if str(p).strip()
            ]
        except (ValueError, SyntaxError):
            continue
    return []


def slugify(name: str) -> str:
    """Filesystem/remote-path-safe slug for a constituency name. Single source
    of truth — ``sync_scrape.sh`` shells out to this so push/pull always agree
    with the filename this module writes."""
    slug = name.lower().replace(" ", "_").replace("&", "and")
    return re.sub(r"[^a-z0-9_]", "", slug)


def _would_clobber_scraped_data(path: Path) -> bool:
    """True if *path* exists and already has a non-empty ``groups`` column."""
    if not path.exists():
        return False
    try:
        existing = pd.read_csv(path)
    except Exception:
        return False
    return "groups" in existing.columns and existing["groups"].notna().any()


def _output_path_for(constituency_name: str | None, explicit: str | None) -> Path:
    """Resolve the output path: explicit > constituency-named > master default."""
    if explicit:
        return Path(explicit)
    if constituency_name:
        return SEARCH_TARGETS_DIR / f"{slugify(constituency_name)}_search_targets.csv"
    return NEW_SCRAPE_PATH


def run(
    constituency_name: str | None = None,
    output_path: Path = NEW_SCRAPE_PATH,
    model: str = DEFAULT_MODEL,
    force: bool = False,
) -> pd.DataFrame:
    if not force and Path(output_path) == NEW_SCRAPE_PATH and _would_clobber_scraped_data(output_path):
        logger.error(
            "%s already contains scraped 'groups' data. Refusing to overwrite; "
            "pass --force or --output to write elsewhere.", output_path,
        )
        sys.exit(1)

    df = pd.read_csv(CONSTITUENCIES_PATH)
    if constituency_name:
        df = df[df["PCON24NM"] == constituency_name].copy()
        if df.empty:
            logger.error("Constituency '%s' not found in %s", constituency_name, CONSTITUENCIES_PATH)
            sys.exit(1)
    logger.info("Generating place names for %d constituencies (model=%s)…", len(df), model)

    df = ai.iterate_df_rows(
        df,
        get_prompt=build_prompt,
        response_column=RESPONSE_COLUMN,
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
    )

    df["_parsed"] = df[RESPONSE_COLUMN].apply(_parse_place_list)
    n_failed = int((df["_parsed"].str.len() == 0).sum())
    if n_failed:
        logger.warning("%d constituencies produced no parseable place list", n_failed)

    exploded = df.explode("_parsed").reset_index(drop=True)
    exploded = exploded[exploded["_parsed"].apply(lambda x: isinstance(x, dict))].copy()
    exploded["place_name"] = exploded["_parsed"].apply(lambda x: x["place"])
    exploded["county"] = exploded["_parsed"].apply(lambda x: x["county"])
    # Same ride-along metadata columns the ward path writes (see
    # generate_search_ward.py _build_row): the scraper/sync tooling reads
    # specific columns by name and tolerates unknown ones. target_confidence
    # is recorded here rather than filtered on (the ward path's
    # --min-confidence gate is deliberately NOT applied — tagging must not
    # change what gets generated).
    exploded["target_type"] = exploded["_parsed"].apply(lambda x: x.get("type", ""))
    exploded["target_confidence"] = exploded["_parsed"].apply(lambda x: x.get("confidence", ""))
    exploded["target_source"] = "llm"
    exploded = exploded.drop(columns=["_parsed"], errors="ignore")
    exploded = exploded[exploded["place_name"] != ""].copy()

    exploded["search_string"] = exploded["place_name"] + ", " + exploded["county"]
    exploded["scroll"] = 2
    exploded["processed"] = False
    exploded["groups"] = ""

    exploded.to_csv(output_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info(
        "Wrote %d search-target rows across %d constituencies → %s",
        len(exploded), exploded["PCON24NM"].nunique(), output_path,
    )
    return exploded


def main():
    parser = argparse.ArgumentParser(description="Generate UK constituency search targets")
    parser.add_argument("--constituency", default=None, help="Generate for a single constituency (PCON24NM)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: auto-named from constituency, or master file for full runs)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model for place-name generation")
    parser.add_argument("--force", action="store_true", help="Overwrite even if the file has scraped 'groups' data")
    parser.add_argument("--print-slug", default=None, metavar="NAME", help="Print the filename slug for NAME and exit (used by sync_scrape.sh)")
    args = parser.parse_args()

    if args.print_slug is not None:
        print(slugify(args.print_slug))
        return

    run(
        constituency_name=args.constituency,
        output_path=_output_path_for(args.constituency, args.output),
        model=args.model,
        force=args.force,
    )


if __name__ == "__main__":
    main()
