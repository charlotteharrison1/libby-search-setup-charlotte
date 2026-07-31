#!/usr/bin/env bash
# Lives on the libby device at /home/pub/libby_download/set_scrape_target.sh.
# (Source of truth is kept here in the repo — re-deploy after editing:
#   scp remote_scripts/set_scrape_target.sh libby:/home/pub/libby_download/
# )
#
# Rewrites clacton.json's master_file_name / output_directory to point at a
# given constituency's pushed search-targets file, instead of hand-editing
# the JSON before every scrape. Only those two fields are touched — every
# other field in clacton.json, and the scraper itself, is left alone.
#
# Usage (run from /home/pub/libby_download, or anywhere — it locates
# clacton.json next to this script):
#   ./set_scrape_target.sh bolsover
#   ./set_scrape_target.sh --force some_new_slug   # skip the "file exists" check

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/clacton.json"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
    shift
fi

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 [--force] <slug>" >&2
    echo "  e.g.: $0 bolsover" >&2
    exit 1
fi
SLUG="$1"

[[ -f "$CONFIG" ]] || { echo "Error: $CONFIG not found" >&2; exit 1; }

MASTER_FILE="$HERE/$SLUG/${SLUG}_search_targets.csv"
if [[ "$FORCE" -eq 0 && ! -f "$MASTER_FILE" ]]; then
    echo "Error: $MASTER_FILE not found — has it been pushed for this slug yet?" >&2
    echo "(pass --force to point clacton.json at it anyway)" >&2
    exit 1
fi

python3 - "$CONFIG" "$SLUG" <<'PY'
import json
import sys

path, slug = sys.argv[1], sys.argv[2]

with open(path) as f:
    cfg = json.load(f)

cfg["master_file_name"] = f"{slug}/{slug}_search_targets.csv"
cfg["output_directory"] = f"{slug}/data/"

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY

echo "clacton.json now targets: $SLUG"
cat "$CONFIG"
