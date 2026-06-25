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
    python -m uk.generate_search --constituency Aldershot
    python -m uk.generate_search --output uk/data/new_targets.csv

By default it writes the master scrape file; to avoid clobbering scraped data it
refuses to overwrite a file that already contains a populated ``groups`` column
unless ``--force`` is given.
"""

import argparse
import ast
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from libby_core import ai
from uk.settings import CONSTITUENCIES_PATH, NEW_SCRAPE_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_MODEL = "openai/gpt-5"
DEFAULT_MAX_TOKENS = 20_000
RESPONSE_COLUMN = "list_of_towns_and_cities"


def build_prompt(row: pd.Series) -> str:
    return f"""
    You are an expert on the UK parliamentary constituencies.
    You are given a constituency name.
    You are to list up to 5 of the most popularly used placenames (at least towns, city or city localities) which are in or are partly within the constituency.
    For example, for a constituency such as Hallam you would respond with "Sheffield".
    Within London, don't respond with "London", instead respond with a list of the localities within London which are in the constituency.
    If a place is not uniquely named in the uk and other places have similar names, add a broader location to the place name (e.g., "Faringdon London" or "Faringdon Oxfordshire")
    The constituency name is: {row['PCON24NM']}

    Respond and only respond with a list of places with the following format:

    ["place1","place2","place3",...]
    """


def _parse_place_list(value) -> list[str]:
    """Parse the LLM's response into a list of place-name strings."""
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        except (ValueError, SyntaxError):
            continue
    return []


def _would_clobber_scraped_data(path: Path) -> bool:
    """True if *path* exists and already has a non-empty ``groups`` column."""
    if not path.exists():
        return False
    try:
        existing = pd.read_csv(path)
    except Exception:
        return False
    return "groups" in existing.columns and existing["groups"].notna().any()


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

    df["place_name"] = df[RESPONSE_COLUMN].apply(_parse_place_list)
    n_failed = int((df["place_name"].str.len() == 0).sum())
    if n_failed:
        logger.warning("%d constituencies produced no parseable place list", n_failed)

    exploded = df.explode("place_name").reset_index(drop=True)
    exploded = exploded[exploded["place_name"].notna() & (exploded["place_name"] != "")].copy()

    # Mark rows as not-yet-scraped so the scraper knows what to do.
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
    parser.add_argument("--output", default=str(NEW_SCRAPE_PATH), help="Output CSV path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model for place-name generation")
    parser.add_argument("--force", action="store_true", help="Overwrite even if the file has scraped 'groups' data")
    args = parser.parse_args()

    run(
        constituency_name=args.constituency,
        output_path=Path(args.output),
        model=args.model,
        force=args.force,
    )


if __name__ == "__main__":
    main()
