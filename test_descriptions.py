"""Quick test: generate and cache a constituency description without needing scrape data.

Usage:
    python test_descriptions.py
    python test_descriptions.py --constituency "Finchley and Golders Green"
"""

import argparse
import logging

import pandas as pd

from libby_core import descriptions
from uk.settings import CONSTITUENCIES_PATH, DESCRIPTIONS_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--constituency", default="Finchley and Golders Green")
    args = parser.parse_args()

    df = pd.read_csv(CONSTITUENCIES_PATH)
    match = df[df["PCON24NM"] == args.constituency]
    if match.empty:
        logger.error("Constituency '%s' not found in %s", args.constituency, CONSTITUENCIES_PATH)
        return

    row = match.iloc[0]
    area_id = row["PCON24CD"]
    area_name = row["PCON24NM"]

    logger.info("Fetching description for %s (%s)", area_name, area_id)
    desc = descriptions.ensure_description(
        area_id=area_id,
        area_name=area_name,
        path=DESCRIPTIONS_PATH,
        area_kind="UK parliament constituency",
        id_col="PCON24CD",
        name_col="PCON24NM",
    )

    print("\n--- Description ---")
    print(desc)
    print(f"\nCached at: {DESCRIPTIONS_PATH}")


if __name__ == "__main__":
    main()
