#!/usr/bin/env bash
# Lives on the libby device at /home/pub/libby_download/set_about_target.sh.
# (Source of truth is kept here in the repo — re-deploy after editing:
#   scp remote_scripts/set_about_target.sh libby:/home/pub/libby_download/
# )
#
# scrape_group_about.py equivalent of set_scrape_target.sh: rewrites
# aboud_params.json's groups_file / output_directory to point at a given
# constituency's or ward's ALREADY-SCRAPED search-targets file, instead of
# hand-editing the JSON before every about-scrape run. Only those two
# fields are touched — every other field (chrome paths, sleep timing, stop
# hour, ...) is left alone, same as set_scrape_target.sh.
#
# Important difference from set_scrape_target.sh: that script points at the
# *master* targets file (<slug>_search_targets.csv, the file pushed before
# scraping). This one points at the *scraped* file one level deeper
# (data/<slug>_search_targets.csv) — scrape_group_about.py reads the
# "groups" column that script.py's scrape fills in, so the target has to be
# the output, not the input.
#
# Constituencies live nested under constituencies/<slug>/data/; wards under
# wards/<slug>/data/ — this checks constituencies/<slug>/ first, then
# wards/<slug>/.
#
# Usage (run from /home/pub/libby_download, or anywhere — it locates
# aboud_params.json next to this script):
#   ./set_about_target.sh lincoln
#   ./set_about_target.sh --force some_new_slug   # skip the "file exists" check

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/aboud_params.json"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
    shift
fi

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 [--force] <slug>" >&2
    echo "  e.g.: $0 lincoln" >&2
    exit 1
fi
SLUG="$1"

[[ -f "$CONFIG" ]] || { echo "Error: $CONFIG not found" >&2; exit 1; }

if [[ -d "$HERE/constituencies/$SLUG" ]]; then
    REL_DIR="constituencies/$SLUG"
elif [[ -d "$HERE/wards/$SLUG" ]]; then
    REL_DIR="wards/$SLUG"
elif [[ "$FORCE" -eq 1 ]]; then
    # --force with no folder yet: default to wards/, same rationale as
    # set_scrape_target.sh (ad-hoc one-off targets are the more common case
    # for something forced before it's been set up).
    REL_DIR="wards/$SLUG"
else
    REL_DIR="$SLUG"
fi

GROUPS_FILE="$HERE/$REL_DIR/data/${SLUG}_search_targets.csv"
if [[ "$FORCE" -eq 0 && ! -f "$GROUPS_FILE" ]]; then
    echo "Error: $GROUPS_FILE not found — has this slug finished its main scrape yet?" >&2
    echo "(pass --force to point aboud_params.json at it anyway)" >&2
    exit 1
fi

python3 - "$CONFIG" "$SLUG" "$REL_DIR" <<'PY'
import json
import sys

path, slug, rel_dir = sys.argv[1], sys.argv[2], sys.argv[3]

with open(path) as f:
    cfg = json.load(f)

cfg["groups_file"] = f"{rel_dir}/data/{slug}_search_targets.csv"
cfg["output_directory"] = f"about_pages/{rel_dir}"

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY

echo "aboud_params.json now targets: $SLUG ($REL_DIR)"
cat "$CONFIG"
