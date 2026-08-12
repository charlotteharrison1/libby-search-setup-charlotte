#!/usr/bin/env bash
# Lives on the libby device at /home/pub/libby_download/pick_next_scrape_target.sh.
# (Source of truth is kept here in the repo — re-deploy after editing:
# )
#
# Scans every pushed constituency/ward folder (a "pushed folder" = any
# directory <slug>/ containing <slug>/<slug>_search_targets.csv) under
# constituencies/<slug>/ and wards/<slug>/, finds ones with no scraped
# output yet (no <slug>/data/<slug>_search_targets.csv), skips whichever
# slug clacton.json is currently pointed at (assumed to be mid-scrape
# already), and hands the oldest-pushed remaining one to
# set_scrape_target.sh.
#
# Usage:
#   ./pick_next_scrape_target.sh             # picks and switches
#   ./pick_next_scrape_target.sh --dry-run   # just prints what it would pick

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/clacton.json"
SET_TARGET="$HERE/set_scrape_target.sh"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

[[ -f "$CONFIG" ]] || { echo "Error: $CONFIG not found" >&2; exit 1; }
[[ -x "$SET_TARGET" ]] || { echo "Error: $SET_TARGET not found or not executable" >&2; exit 1; }

# The slug is always the immediate parent directory of the CSV, regardless of
# whether master_file_name is "slug/slug_....csv" (ward) or
# "constituencies/slug/slug_....csv" (constituency) — so take the
# second-to-last path segment, not the first.
CURRENT_SLUG="$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
parts = cfg.get('master_file_name', '').split('/')
print(parts[-2] if len(parts) >= 2 else '')
")"

CANDIDATE_SLUGS=()
CANDIDATE_MTIMES=()
for dir in "$HERE"/constituencies/*/ "$HERE"/wards/*/; do
    slug="$(basename "$dir")"
    master="${dir}${slug}_search_targets.csv"
    [[ -f "$master" ]] || continue                     # not a pushed constituency/ward folder
    [[ "$slug" == "$CURRENT_SLUG" ]] && continue        # currently active target
    scraped="${dir}data/${slug}_search_targets.csv"
    [[ -f "$scraped" ]] && continue                     # already scraped

    mtime="$(stat -c '%Y' "$master" 2>/dev/null || stat -f '%m' "$master")"
    CANDIDATE_SLUGS+=("$slug")
    CANDIDATE_MTIMES+=("$mtime")
done

if [[ ${#CANDIDATE_SLUGS[@]} -eq 0 ]]; then
    echo "Nothing left to scrape (excluding current target '$CURRENT_SLUG')."
    exit 0
fi

echo "Not yet scraped: ${CANDIDATE_SLUGS[*]}"

# Oldest-pushed first, so constituencies work through in roughly the order
# they were prepped.
BEST_IDX=0
for i in "${!CANDIDATE_MTIMES[@]}"; do
    if [[ "${CANDIDATE_MTIMES[$i]}" -lt "${CANDIDATE_MTIMES[$BEST_IDX]}" ]]; then
        BEST_IDX=$i
    fi
done
NEXT_SLUG="${CANDIDATE_SLUGS[$BEST_IDX]}"

echo "Next: $NEXT_SLUG"

if [[ "$DRY_RUN" -eq 1 ]]; then
    exit 0
fi

"$SET_TARGET" "$NEXT_SLUG"
