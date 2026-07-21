#!/usr/bin/env bash
# Usage:
#   ./sync_scrape.sh push "Birmingham Ladywood"
#   ./sync_scrape.sh pull "Birmingham Ladywood"

set -euo pipefail

DEVICE="libby"
REMOTE_BASE="/home/pub/libby_download"
LOCAL_TARGETS="$(dirname "$0")/uk/data/search_targets"
LOCAL_SCRAPED="$(dirname "$0")/uk/data/scraped"

# --- helpers -----------------------------------------------------------------

usage() {
    echo "Usage: $0 push|pull <constituency name>"
    exit 1
}

# Derive slug: lowercase, spaces→underscores, &→and, strip punctuation
to_slug() {
    echo "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/ & / and /g; s/&/and/g' \
        | sed 's/ /_/g' \
        | sed "s/[^a-z0-9_]//g"
}

# --- args --------------------------------------------------------------------

[[ $# -lt 2 ]] && usage
ACTION="$1"
shift

# --all: derive constituency list from every file in search_targets/
if [[ "$1" == "--all" ]]; then
    CONSTITUENCIES=()
    for f in "$LOCAL_TARGETS"/*_search_targets.csv; do
        [[ -f "$f" ]] || continue
        basename="${f##*/}"
        slug="${basename%_search_targets.csv}"
        CONSTITUENCIES+=("$slug")
    done
    if [[ ${#CONSTITUENCIES[@]} -eq 0 ]]; then
        echo "Error: no files found in $LOCAL_TARGETS" >&2
        exit 1
    fi
    echo "Found ${#CONSTITUENCIES[@]} files: ${CONSTITUENCIES[*]}"
else
    CONSTITUENCIES=("$@")
fi

# --- commands ----------------------------------------------------------------

for CONSTITUENCY in "${CONSTITUENCIES[@]}"; do
    SLUG="$(to_slug "$CONSTITUENCY")"  # no-op if already a slug
    LOCAL_TARGET_FILE="$LOCAL_TARGETS/${SLUG}_search_targets.csv"
    LOCAL_SCRAPED_FILE="$LOCAL_SCRAPED/${SLUG}_search_targets.csv"
    REMOTE_DIR="$REMOTE_BASE/$SLUG"

    case "$ACTION" in
      push)
        if [[ ! -f "$LOCAL_TARGET_FILE" ]]; then
            echo "Error: local file not found: $LOCAL_TARGET_FILE" >&2
            continue
        fi
        echo "Creating remote directory (if needed): $DEVICE:$REMOTE_DIR"
        ssh "$DEVICE" "test -d '$REMOTE_DIR' || mkdir '$REMOTE_DIR'"
        echo "Uploading: $LOCAL_TARGET_FILE → $DEVICE:$REMOTE_DIR/"
        scp "$LOCAL_TARGET_FILE" "$DEVICE:$REMOTE_DIR/"
        echo "Done: $CONSTITUENCY"
        ;;

      pull)
        REMOTE_FILE="$REMOTE_DIR/data/${SLUG}_search_targets.csv"
        echo "Downloading: $DEVICE:$REMOTE_FILE → $LOCAL_SCRAPED_FILE"
        scp "$DEVICE:$REMOTE_FILE" "$LOCAL_SCRAPED_FILE"
        echo "Done: $CONSTITUENCY"
        ;;

      *)
        usage
        ;;
    esac
done
