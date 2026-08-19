#!/usr/bin/env bash
# Lives on the libby device at /home/pub/libby_download/pick_next_about_target.sh.
# (Source of truth is kept here in the repo — re-deploy after editing:
#   scp remote_scripts/pick_next_about_target.sh libby:/home/pub/libby_download/
# )
#
# scrape_group_about.py equivalent of pick_next_scrape_target.sh. Scans
# every constituency/ward that has finished its main scrape (a "candidate" =
# any directory with constituencies/<slug>/data/<slug>_search_targets.csv or
# wards/<slug>/data/<slug>_search_targets.csv), finds ones with no
# about-scrape output yet (no
# about_pages/<category>/<slug>/<slug>_search_targets_about.csv), skips
# whichever slug aboud_params.json is currently pointed at (assumed to be
# mid-scrape already), and hands the oldest-finished remaining one to
# set_about_target.sh.
#
# Caveat shared with pick_next_scrape_target.sh: this is a file-existence
# check, not a completion check. Unlike the main scraper, scrape_group_about.py
# IS resumable (it tracks a per-row "processed" column and picks up where it
# left off) — so a run that got interrupted partway (e.g. by its own
# stop_hour/restart_hour overnight pause, or a crash) leaves a real, valid,
# but incomplete about-output file behind. That file's mere existence is
# enough for this script to treat the slug as "already about-scraped" and
# skip it. If you suspect a slug was left partially done, re-target it
# directly with set_about_target.sh rather than relying on this picker to
# find it again — scrape_group_about.py will pick up the unprocessed rows.
#
# Usage:
#   ./pick_next_about_target.sh             # picks and switches
#   ./pick_next_about_target.sh --dry-run   # just prints what it would pick

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/aboud_params.json"
SET_TARGET="$HERE/set_about_target.sh"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

[[ -f "$CONFIG" ]] || { echo "Error: $CONFIG not found" >&2; exit 1; }
[[ -x "$SET_TARGET" ]] || { echo "Error: $SET_TARGET not found or not executable" >&2; exit 1; }

# groups_file is "<category>/<slug>/data/<slug>_search_targets.csv" — one
# path segment deeper than set_scrape_target.sh's master_file_name, so the
# slug is the THIRD-to-last segment here, not the second-to-last.
CURRENT_SLUG="$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
parts = cfg.get('groups_file', '').split('/')
print(parts[-3] if len(parts) >= 3 else '')
")"

CANDIDATE_SLUGS=()
CANDIDATE_MTIMES=()
for data_file in "$HERE"/constituencies/*/data/*_search_targets.csv "$HERE"/wards/*/data/*_search_targets.csv; do
    [[ -f "$data_file" ]] || continue

    # data_file = .../<category>/<slug>/data/<slug>_search_targets.csv
    slug_dir="$(dirname "$(dirname "$data_file")")"   # .../<category>/<slug>
    slug="$(basename "$slug_dir")"
    category="$(basename "$(dirname "$slug_dir")")"    # constituencies | wards

    [[ "$slug" == "$CURRENT_SLUG" ]] && continue        # currently active target

    about_file="$HERE/about_pages/$category/$slug/${slug}_search_targets_about.csv"
    [[ -f "$about_file" ]] && continue                  # already about-scraped (see caveat above)

    mtime="$(stat -c '%Y' "$data_file" 2>/dev/null || stat -f '%m' "$data_file")"
    CANDIDATE_SLUGS+=("$slug")
    CANDIDATE_MTIMES+=("$mtime")
done

if [[ ${#CANDIDATE_SLUGS[@]} -eq 0 ]]; then
    echo "Nothing left to about-scrape (excluding current target '$CURRENT_SLUG')."
    exit 0
fi

echo "Not yet about-scraped: ${CANDIDATE_SLUGS[*]}"

# Oldest-finished-scrape first, so about-pages work through in roughly the
# order the underlying group data became available.
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
