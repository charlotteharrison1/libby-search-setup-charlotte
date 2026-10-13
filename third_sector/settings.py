"""Third-sector (topic-based) pipeline paths. Mirrors uk/settings.py's layout,
but keyed by topic ("UK environmental groups") instead of geography."""

from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent

DATA_DIR = _THIS_DIR / "data"
SEARCH_TARGETS_DIR = DATA_DIR / "search_targets"
SCRAPED_DIR = DATA_DIR / "scraped"
OUTPUT_DIR = _THIS_DIR / "output"
GROUP_LOG_PATH = OUTPUT_DIR / "group_log.csv"

# Committed (not under data/ or output/): topics + prompts are source you edit.
PROMPTS_PATH = _THIS_DIR / "prompts.json"

for _d in (SEARCH_TARGETS_DIR, SCRAPED_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
