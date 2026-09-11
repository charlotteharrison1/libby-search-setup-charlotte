"""UK pipeline configuration: data/output paths and processing constants.

The shared OpenRouter API key comes from ``libby_core.settings`` (root .env).
"""

from pathlib import Path

import pandas as pd

from libby_core.settings import OPEN_ROUTER_KEY  # noqa: F401  (re-exported for convenience)

# Group/area names scraped from Facebook occasionally contain unpaired
# UTF-16 surrogates (mangled emoji) — several files in this codebase are
# written with encoding="utf-8", errors="surrogatepass" to preserve them
# rather than crash. Reading such a file back needs the matching
# encoding_errors="surrogatepass" — but pandas 3.x's default PyArrow-backed
# string dtype cannot represent a lone surrogate at all and raises
# UnicodeEncodeError on it, which is why every surrogatepass read in this
# codebase used encoding="latin-1" as a non-raising workaround instead.
# That "worked" (never raised) but silently mojibakes every ordinary
# multi-byte UTF-8 character (e.g. any em dash in group_log.csv's reason
# text) on every read+rewrite cycle — confirmed actively corrupting
# uk/output/group_log.csv. Disabling PyArrow-backed strings here, once, at
# import time (uk.settings is imported by everything that touches these
# files) is what actually makes a correct, non-lossy
# encoding="utf-8"/encoding_errors="surrogatepass" read possible.
pd.set_option("future.infer_string", False)

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
ABOUT_PAGES_DIR: Path = DATA_DIR / "about_pages"
WARD_ABOUT_PAGES_DIR: Path = ABOUT_PAGES_DIR / "wards"

OUTPUT_DIR.mkdir(exist_ok=True)
INTERMEDIATE_DIR.mkdir(exist_ok=True)
SEARCH_TARGETS_DIR.mkdir(exist_ok=True)
WARD_SEARCH_TARGETS_DIR.mkdir(exist_ok=True)
SCRAPED_DIR.mkdir(exist_ok=True)
WARD_SCRAPED_DIR.mkdir(exist_ok=True)
OVERTURE_DIR.mkdir(exist_ok=True)
WARD_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
WARD_OUTPUT_DIR.mkdir(exist_ok=True)
ABOUT_PAGES_DIR.mkdir(exist_ok=True)
WARD_ABOUT_PAGES_DIR.mkdir(parents=True, exist_ok=True)

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

# One log, every group, every area, both pipelines: for each group ever
# considered, whether it was accepted into a final groups_*.csv and why (or
# why not — including whether that verdict came from a local --about
# re-check). Upserted per area as each is (re)processed — see
# uk.pipeline._upsert_group_log — so a constituency's or ward's rows are
# replaced wholesale on reprocessing, never duplicated or left stale.
GROUP_LOG_PATH = OUTPUT_DIR / "group_log.csv"

# Durable record of manually-recovered groups (see uk/recover_group.py) —
# checked once per run at the top of run(), the same way the About-context
# cache is, and re-applied per area at the end of that area's processing so
# a real future reprocess doesn't silently drop a recovered group again.
GROUP_OVERRIDES_PATH = OUTPUT_DIR / "group_overrides.csv"

# uk.queue_unsure_for_about's output: a groups_file-shaped CSV of every
# still-Unsure, not-yet-About-scraped group across every groups_*.csv found,
# ready to hand to libby_download's scrape_group_about.py. Regenerated fresh
# each run, not accumulated — see uk/queue_unsure_for_about.py.
UNSURE_QUEUE_PATH = OUTPUT_DIR / "unsure_queue.csv"

# uk/local_about_scraper.py: --about drives a local, already-logged-in
# Chrome session (your own machine, your own Facebook login — see that
# module's docstring for why) to About-scrape just the Unsure/uncached
# groups from the current run, inline, no separate trip to libby. Both are
# machine-local and gitignored (LOCAL_CHROME_PROFILE_PATH lives under
# uk/data/, already entirely ignored); LIBBY_DOWNLOAD_PATH is overridable
# via the LIBBY_DOWNLOAD_PATH env var for a machine where the sibling repo
# isn't checked out at this default path.
import os

LOCAL_CHROME_PROFILE_PATH: Path = DATA_DIR / "local_chrome_profile"
LIBBY_DOWNLOAD_PATH: Path = Path(
    os.environ.get("LIBBY_DOWNLOAD_PATH", "~/vs_code/libby_download/libby_download")
).expanduser()

# Where a finished run's groups_<Name>.csv gets staged for data_collection.py
# (billable) to eventually pick up — same directory batch_pipeline.sh's
# "sync" mode and control_centre/status.py's "staged" check already use.
# Wards land in CLACTON_INPUTS_DIR / "wards", constituencies flat, matching
# control_centre/status.py's existing scan of both locations.
CLACTON_INPUTS_DIR: Path = Path(
    os.environ.get("CLACTON_INPUTS_DIR", "/Users/charlotte/vs_code/Clacton-etc/inputs")
).expanduser()

# Where a staged result graduates to once someone deliberately promotes it
# (still a manual step — see batch_pipeline.sh's docstring) — what
# data_collection.py (billable) actually reads. Flat for both area types
# (mixes constituency + ward groups_*.csv together), matching
# control_centre/status.py's existing "promoted" scan.
CLACTON_GROUPS_DIR: Path = CLACTON_INPUTS_DIR.parent / "groups"
