#!/usr/bin/env bash
# Ward equivalent of `batch_pipeline.sh sync`: pulls every ward's scraped
# data from libby, reprocesses it through uk.pipeline_ward, and stages the
# finished groups_{ward}.csv files into Clacton-etc/inputs/wards/ — same
# "safe staging area, not the billable groups/ folder" pattern
# batch_pipeline.sh sync already uses for constituencies.
#
# Structurally simpler than batch_pipeline.sh sync: that one takes an
# explicit list of constituency names and loops uk.pipeline once per name.
# uk.pipeline_ward already processes every scraped ward file it finds in one
# call (that's how it was built — see its own module docstring), so there's
# no per-name list to pass here; this always operates on whatever's
# currently pushed.
#
# Usage:
#   ./sync_wards.sh            # pull + process + stage, skipping if nothing changed
#   ./sync_wards.sh --force    # reprocess even if the scraped data is unchanged since last sync

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SYNC_SCRAPE="$REPO_ROOT/sync_scrape.sh"
WARD_SCRAPED_DIR="$REPO_ROOT/uk/data/scraped/wards"
WARD_OUTPUT_DIR="$REPO_ROOT/uk/output/wards"
MANIFEST="$WARD_SCRAPED_DIR/.sync_manifest"
INPUTS_WARDS_DIR="${CLACTON_INPUTS_DIR:-/Users/charlotte/vs_code/Clacton-etc/inputs}/wards"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

echo "=== pulling all ward data ==="
"$SYNC_SCRAPE" pull --wards
# Not fatal if some wards aren't scraped yet — sync_scrape.sh already reports
# per-ward success/failure; whatever did pull is what gets processed below.

# Combined hash of every currently-scraped ward file. uk.pipeline_ward has no
# per-ward resumability of its own (every run reprocesses everything fresh,
# including the paid AI-assessment step) — this is the one place we CAN
# cheaply detect "nothing changed since last sync" and skip re-spending on a
# no-op run. It can't skip individual unchanged wards within a mixed batch
# (uk.pipeline_ward doesn't support partial input lists), only an
# all-unchanged batch entirely.
shopt -s nullglob
SCRAPED_FILES=("$WARD_SCRAPED_DIR"/*_search_targets.csv)
shopt -u nullglob

if [[ ${#SCRAPED_FILES[@]} -eq 0 ]]; then
    echo "No scraped ward files found in $WARD_SCRAPED_DIR — nothing to process."
    exit 0
fi

CURRENT_HASH="$(cat "${SCRAPED_FILES[@]}" | shasum -a 256 | awk '{print $1}')"
RECORDED_HASH="$(cat "$MANIFEST" 2>/dev/null || true)"

if [[ "$FORCE" -eq 0 && -n "$CURRENT_HASH" && "$CURRENT_HASH" == "$RECORDED_HASH" ]]; then
    echo "No change in scraped ward data since last sync — skipping uk.pipeline_ward."
    echo "(pass --force to reprocess anyway)"
    exit 0
fi

echo
echo "=== processing wards ==="
if ! (cd "$REPO_ROOT" && python3 -m uk.pipeline_ward); then
    echo "!! uk.pipeline_ward failed" >&2
    exit 1
fi

echo
echo "=== staging finished groups_*.csv into $INPUTS_WARDS_DIR ==="
mkdir -p "$INPUTS_WARDS_DIR"
MOVED=()
shopt -s nullglob
for f in "$WARD_OUTPUT_DIR"/groups_*.csv; do
    mv -f "$f" "$INPUTS_WARDS_DIR/"
    MOVED+=("$(basename "$f")")
done
shopt -u nullglob

if [[ ${#MOVED[@]} -eq 0 ]]; then
    echo "Nothing to move (no groups_*.csv survived filtering)."
else
    echo "Moved ${#MOVED[@]} file(s) → $INPUTS_WARDS_DIR:"
    printf '  %s\n' "${MOVED[@]}"
fi

echo "$CURRENT_HASH" > "$MANIFEST"
