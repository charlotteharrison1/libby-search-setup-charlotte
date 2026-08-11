"""UK pipeline configuration: data/output paths and processing constants.

The shared OpenRouter API key comes from ``libby_core.settings`` (root .env).
"""

from pathlib import Path

from libby_core.settings import OPEN_ROUTER_KEY  # noqa: F401  (re-exported for convenience)

_THIS_DIR = Path(__file__).resolve().parent

DATA_DIR: Path = _THIS_DIR / "data"
REFERENCE_DIR: Path = DATA_DIR / "reference"
WARD_REFERENCE_DIR: Path = REFERENCE_DIR / "wards"
SEARCH_TARGETS_DIR: Path = DATA_DIR / "search_targets"
WARD_SEARCH_TARGETS_DIR: Path = SEARCH_TARGETS_DIR / "wards"
OVERTURE_DIR: Path = _THIS_DIR / "overture-outputs"
SCRAPED_DIR: Path = DATA_DIR / "scraped"
WARD_SCRAPED_DIR: Path = SCRAPED_DIR / "wards"
OUTPUT_DIR: Path = _THIS_DIR / "output"
INTERMEDIATE_DIR: Path = OUTPUT_DIR / "intermediate"
WARD_OUTPUT_DIR: Path = OUTPUT_DIR / "wards"

OUTPUT_DIR.mkdir(exist_ok=True)
INTERMEDIATE_DIR.mkdir(exist_ok=True)
SEARCH_TARGETS_DIR.mkdir(exist_ok=True)
WARD_SEARCH_TARGETS_DIR.mkdir(exist_ok=True)
SCRAPED_DIR.mkdir(exist_ok=True)
WARD_SCRAPED_DIR.mkdir(exist_ok=True)
OVERTURE_DIR.mkdir(exist_ok=True)
WARD_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
WARD_OUTPUT_DIR.mkdir(exist_ok=True)

# Reference files — place these in uk/data/reference/ and do not modify them.
CONSTITUENCIES_PATH = REFERENCE_DIR / "constituencies_2024.csv"
WARD_BOUNDARIES_PATH = WARD_REFERENCE_DIR / "WD_MAY_2026_UK_BFE.shp"
PREVIOUS_SCRAPE_PATH = REFERENCE_DIR / "libby_list_groups_by_constituency.csv"
PCON_MAPPING_PATH = REFERENCE_DIR / "Westminster_PCON_(2010)_to_future_Westminster_PCON_(2024)_Lookup_in_the_UK_(V2).csv"
GEOJSON_PATH = REFERENCE_DIR / "Westminster_Parliamentary_Constituencies_July_2024_Boundaries_UK_BFC_5018004800687358456.geojson"
DENSITIES_PATH = REFERENCE_DIR / "parliament_con_data_inc_densities_2025.csv"

# Hand-maintained lists of what to run — edited by you, unlike the reference
# files above. Both constituency batch lists (e.g. nathan_targets.txt, for
# batch_pipeline.sh --file) and the ward input list live in search_targets/.
DEFAULT_WARDS_FILE = SEARCH_TARGETS_DIR / "adhoc_wards.csv"

# Working files written and read by the pipeline.
REDO_GROUPS_PATH = SCRAPED_DIR / "redo_groups.csv"
NEW_SCRAPE_PATH = SCRAPED_DIR / "master_constituency_place_data_file.csv"
DESCRIPTIONS_PATH = DATA_DIR / "descriptions.csv"
WARD_DESCRIPTIONS_PATH = DATA_DIR / "ward_descriptions.csv"
