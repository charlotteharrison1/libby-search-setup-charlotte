"""US pipeline configuration.

A "district" here is a US congressional district. Each district has its own
folder of input data under ``us/data/<district_id>/`` containing the scraped
``*_places.csv`` and ``*_localities.csv`` files.

Change ``DISTRICT_ID`` / ``DISTRICT_NAME`` (or set them from the CLI in
pipeline.py) to process a different district.

The shared OpenRouter API key comes from ``libby_core.settings`` (root .env).
"""

from pathlib import Path

from libby_core.settings import OPEN_ROUTER_KEY  # noqa: F401  (re-exported for convenience)

_THIS_DIR = Path(__file__).resolve().parent

# --- District being processed -------------------------------------------------
DISTRICT_ID = "il-14"
DISTRICT_NAME = "Illinois 14th Congressional District"
AREA_KIND = "US congressional district"

# --- Paths --------------------------------------------------------------------
DATA_DIR: Path = _THIS_DIR / "data"
OUTPUT_DIR: Path = _THIS_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def district_dir(district_id: str = DISTRICT_ID) -> Path:
    return DATA_DIR / district_id


def places_path(district_id: str = DISTRICT_ID) -> Path:
    return district_dir(district_id) / f"{district_id}_places.csv"


def localities_path(district_id: str = DISTRICT_ID) -> Path:
    return district_dir(district_id) / f"{district_id}_localities.csv"


def output_path(district_id: str = DISTRICT_ID) -> Path:
    return OUTPUT_DIR / f"{district_id}_libby_list.csv"


# Description cache (shared across districts, keyed by district id).
DESCRIPTIONS_PATH = DATA_DIR / "district_descriptions.csv"

# --- Filtering thresholds (mirror the UK pipeline) ----------------------------
MIN_MEMBERS = 100
