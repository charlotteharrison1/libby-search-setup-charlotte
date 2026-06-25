"""UK pipeline configuration: data/output paths and processing constants.

The shared OpenRouter API key comes from ``libby_core.settings`` (root .env).
"""

from pathlib import Path

from libby_core.settings import OPEN_ROUTER_KEY  # noqa: F401  (re-exported for convenience)

_THIS_DIR = Path(__file__).resolve().parent

DATA_DIR: Path = _THIS_DIR / "data"
OUTPUT_DIR: Path = _THIS_DIR / "output"
INTERMEDIATE_DIR: Path = OUTPUT_DIR / "Intermediate"

OUTPUT_DIR.mkdir(exist_ok=True)
INTERMEDIATE_DIR.mkdir(exist_ok=True)

# Input data files (place these in uk/data/ — see uk/README.md).
# Source list of constituencies (FID, PCON24CD, PCON24NM, LONG, LAT, …) used by
# generate_search.py to produce the search-target master file.
CONSTITUENCIES_PATH = DATA_DIR / "constituencies_2024.csv"
# Master scrape file: written (pre-scrape, empty groups) by generate_search.py,
# then filled in by the scraper, then read by pipeline.py.
NEW_SCRAPE_PATH = DATA_DIR / "master_constituency_place_data_file.csv"
REDO_GROUPS_PATH = DATA_DIR / "redo_groups.csv"
PREVIOUS_SCRAPE_PATH = DATA_DIR / "libby_list_groups_by_constituency.csv"
PCON_MAPPING_PATH = DATA_DIR / "Westminster_PCON_(2010)_to_future_Westminster_PCON_(2024)_Lookup_in_the_UK_(V2).csv"
GEOJSON_PATH = DATA_DIR / "Westminster_Parliamentary_Constituencies_July_2024_Boundaries_UK_BFC_5018004800687358456.geojson"
DENSITIES_PATH = DATA_DIR / "parliament_con_data_inc_densities_2025.csv"
DESCRIPTIONS_PATH = DATA_DIR / "constituency_descriptions.csv"
