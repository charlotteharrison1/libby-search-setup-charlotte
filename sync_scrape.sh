#!/usr/bin/env bash
# Usage:
#   ./sync_scrape.sh push "Birmingham Ladywood"
#   ./sync_scrape.sh pull "Birmingham Ladywood"

set -uo pipefail

DEVICE="libby"
REMOTE_BASE="/home/pub/libby_download"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOCAL_TARGETS="$REPO_ROOT/uk/data/search_targets"
LOCAL_SCRAPED="$REPO_ROOT/uk/data/scraped"

# --- helpers -----------------------------------------------------------------

usage() {
    echo "Usage: $0 push|pull <constituency name> [<constituency name> ...]"
    echo "       $0 push|pull --all"
    exit 1
}

# Derive slug via uk.generate_search's slugify() — the single source of truth
# for how constituency names become filenames, so push/pull always agree with
# what generate_search.py actually wrote.
to_slug() {
    (cd "$REPO_ROOT" && python3 -m uk.generate_search --print-slug "$1")
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
# Each constituency is independent: one bad file/SSH hiccup is logged and
# skipped rather than aborting the rest of the list.

FAILED=()
SUCCEEDED=()

for CONSTITUENCY in "${CONSTITUENCIES[@]}"; do
    SLUG="$(to_slug "$CONSTITUENCY")" || { echo "!! could not derive slug for: $CONSTITUENCY" >&2; FAILED+=("$CONSTITUENCY"); continue; }
    LOCAL_TARGET_FILE="$LOCAL_TARGETS/${SLUG}_search_targets.csv"
    LOCAL_SCRAPED_FILE="$LOCAL_SCRAPED/${SLUG}_search_targets.csv"
    REMOTE_DIR="$REMOTE_BASE/$SLUG"

    case "$ACTION" in
      push)
        if [[ ! -f "$LOCAL_TARGET_FILE" ]]; then
            echo "!! local file not found: $LOCAL_TARGET_FILE" >&2
            FAILED+=("$CONSTITUENCY")
            continue
        fi
        echo "=== push: $CONSTITUENCY ==="
        echo "Creating remote directory (if needed): $DEVICE:$REMOTE_DIR"
        if ! ssh "$DEVICE" "test -d '$REMOTE_DIR' || mkdir '$REMOTE_DIR'"; then
            echo "!! failed to create remote dir for: $CONSTITUENCY" >&2
            FAILED+=("$CONSTITUENCY")
            continue
        fi
        echo "Uploading: $LOCAL_TARGET_FILE → $DEVICE:$REMOTE_DIR/"
        if scp "$LOCAL_TARGET_FILE" "$DEVICE:$REMOTE_DIR/"; then
            echo "Done: $CONSTITUENCY"
            SUCCEEDED+=("$CONSTITUENCY")
        else
            echo "!! upload failed for: $CONSTITUENCY" >&2
            FAILED+=("$CONSTITUENCY")
        fi
        ;;

      pull)
        REMOTE_FILE="$REMOTE_DIR/data/${SLUG}_search_targets.csv"
        echo "=== pull: $CONSTITUENCY ==="
        echo "Downloading: $DEVICE:$REMOTE_FILE → $LOCAL_SCRAPED_FILE"
        if scp "$DEVICE:$REMOTE_FILE" "$LOCAL_SCRAPED_FILE"; then
            echo "Done: $CONSTITUENCY"
            SUCCEEDED+=("$CONSTITUENCY")
        else
            echo "!! download failed for: $CONSTITUENCY (scrape may not be finished yet)" >&2
            FAILED+=("$CONSTITUENCY")
        fi
        ;;

      *)
        usage
        ;;
    esac
done

echo
echo "$ACTION complete: ${#SUCCEEDED[@]} succeeded, ${#FAILED[@]} failed"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "Failed: ${FAILED[*]}"
    exit 1
fi
