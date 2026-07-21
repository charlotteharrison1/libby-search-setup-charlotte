"""UK pipeline configuration: data/output paths and processing constants.

The shared OpenRouter API key comes from ``libby_core.settings`` (root .env).
"""

from pathlib import Path

from libby_core.settings import OPEN_ROUTER_KEY  # noqa: F401  (re-exported for convenience)

_THIS_DIR = Path(__file__).resolve().parent

DATA_DIR: Path = _THIS_DIR / "data"
REFERENCE_DIR: Path = DATA_DIR / "reference"
SEARCH_TARGETS_DIR: Path = DATA_DIR / "search_targets"
OVERTURE_DIR: Path = _THIS_DIR / "overture-outputs"
SCRAPED_DIR: Path = DATA_DIR / "scraped"
OUTPUT_DIR: Path = _THIS_DIR / "output"
INTERMEDIATE_DIR: Path = OUTPUT_DIR / "intermediate"

OUTPUT_DIR.mkdir(exist_ok=True)
INTERMEDIATE_DIR.mkdir(exist_ok=True)
SEARCH_TARGETS_DIR.mkdir(exist_ok=True)
SCRAPED_DIR.mkdir(exist_ok=True)
OVERTURE_DIR.mkdir(exist_ok=True)

# Reference files — place these in uk/data/reference/ and do not modify them.
CONSTITUENCIES_PATH = REFERENCE_DIR / "constituencies_2024.csv"
PREVIOUS_SCRAPE_PATH = REFERENCE_DIR / "libby_list_groups_by_constituency.csv"
PCON_MAPPING_PATH = REFERENCE_DIR / "Westminster_PCON_(2010)_to_future_Westminster_PCON_(2024)_Lookup_in_the_UK_(V2).csv"
GEOJSON_PATH = REFERENCE_DIR / "Westminster_Parliamentary_Constituencies_July_2024_Boundaries_UK_BFC_5018004800687358456.geojson"
DENSITIES_PATH = REFERENCE_DIR / "parliament_con_data_inc_densities_2025.csv"

# Working files written and read by the pipeline.
REDO_GROUPS_PATH = SCRAPED_DIR / "redo_groups.csv"
NEW_SCRAPE_PATH = SCRAPED_DIR / "master_constituency_place_data_file.csv"
DESCRIPTIONS_PATH = DATA_DIR / "descriptions.csv"
