#!/usr/bin/env bash
# Usage:
#   ./sync_scrape.sh push "Birmingham Ladywood"
#   ./sync_scrape.sh pull "Birmingham Ladywood"
#   ./sync_scrape.sh push|pull --all               (every constituency + ward file)
#   ./sync_scrape.sh push|pull --constituencies     (every constituency file only)
#   ./sync_scrape.sh push|pull --wards              (every ward file only)
#   ./sync_scrape.sh push|pull --wards "Name"           (one name, forced to wards/)
#   ./sync_scrape.sh push|pull --constituencies "Name"  (one name, forced to search_targets/)
#
# Constituency files live flat in search_targets/ and scraped/; ward files
# live in search_targets/wards/ and scraped/wards/. A bare name (no --wards /
# --constituencies) is resolved to whichever location has a matching file —
# but if a slug matches a file in BOTH locations (e.g. a ward batch named
# after its parent constituency), that's refused as ambiguous rather than
# silently guessed; add --wards or --constituencies to that name to say which
# one you meant.

set -uo pipefail

DEVICE="libby"
REMOTE_BASE="/home/pub/libby_download"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOCAL_TARGETS="$REPO_ROOT/uk/data/search_targets"
LOCAL_TARGETS_WARDS="$LOCAL_TARGETS/wards"
LOCAL_SCRAPED="$REPO_ROOT/uk/data/scraped"
LOCAL_SCRAPED_WARDS="$LOCAL_SCRAPED/wards"

mkdir -p "$LOCAL_TARGETS_WARDS" "$LOCAL_SCRAPED_WARDS"

# --- helpers -----------------------------------------------------------------

usage() {
    echo "Usage: $0 push|pull <name> [<name> ...]"
    echo "       $0 push|pull --all                       (every constituency + ward file)"
    echo "       $0 push|pull --constituencies             (every constituency file only)"
    echo "       $0 push|pull --wards                      (every ward file only)"
    echo "       $0 push|pull --wards <name> [<name> ...]          (force these names to wards/)"
    echo "       $0 push|pull --constituencies <name> [<name> ...] (force these names to search_targets/)"
    exit 1
}

# Derive slug via uk.generate_search's slugify() — the single source of truth
# for how names become filenames, so push/pull always agree with what
# generate_search.py / generate_search_ward.py actually wrote.
to_slug() {
    (cd "$REPO_ROOT" && python3 -m uk.generate_search --print-slug "$1")
}

# List slugs from every *_search_targets.csv directly inside a directory
# (non-recursive, so this never descends into a wards/ subfolder by accident).
list_slugs() {
    local dir="$1"
    for f in "$dir"/*_search_targets.csv; do
        [[ -f "$f" ]] || continue
        basename="${f##*/}"
        echo "${basename%_search_targets.csv}"
    done
}

# --- args --------------------------------------------------------------------

[[ $# -lt 2 ]] && usage
ACTION="$1"
shift

CATEGORY=""   # "", "wards", or "constituencies" — "" means auto-detect per name
BATCH_MODE=0

case "$1" in
  --all)
    CONSTITUENCIES=($(list_slugs "$LOCAL_TARGETS") $(list_slugs "$LOCAL_TARGETS_WARDS"))
    BATCH_MODE=1
    ;;
  --constituencies|--wards)
    CATEGORY="${1#--}"
    [[ "$CATEGORY" == "wards" ]] && CATEGORY_DIR="$LOCAL_TARGETS_WARDS" || CATEGORY_DIR="$LOCAL_TARGETS"
    shift
    if [[ $# -eq 0 ]]; then
        CONSTITUENCIES=($(list_slugs "$CATEGORY_DIR"))
        BATCH_MODE=1
    else
        CONSTITUENCIES=("$@")
    fi
    ;;
  *)
    CONSTITUENCIES=("$@")
    ;;
esac

if [[ ${#CONSTITUENCIES[@]} -eq 0 ]]; then
    echo "Error: no files found" >&2
    exit 1
fi
[[ "$BATCH_MODE" -eq 1 ]] && echo "Found ${#CONSTITUENCIES[@]} files: ${CONSTITUENCIES[*]}"

# --- commands ----------------------------------------------------------------
# Each name is independent: one bad file/SSH hiccup is logged and skipped
# rather than aborting the rest of the list.

FAILED=()
SUCCEEDED=()

for CONSTITUENCY in "${CONSTITUENCIES[@]}"; do
    SLUG="$(to_slug "$CONSTITUENCY")" || { echo "!! could not derive slug for: $CONSTITUENCY" >&2; FAILED+=("$CONSTITUENCY"); continue; }

    WARD_FILE="$LOCAL_TARGETS_WARDS/${SLUG}_search_targets.csv"
    CONSTITUENCY_FILE="$LOCAL_TARGETS/${SLUG}_search_targets.csv"

    if [[ "$CATEGORY" == "wards" ]]; then
        RESOLVED="wards"
    elif [[ "$CATEGORY" == "constituencies" ]]; then
        RESOLVED="constituencies"
    elif [[ -f "$WARD_FILE" && -f "$CONSTITUENCY_FILE" ]]; then
        echo "!! '$CONSTITUENCY' (slug '$SLUG') matches a file in both search_targets/ and search_targets/wards/ — ambiguous. Disambiguate with:" >&2
        echo "     $0 $ACTION --wards \"$CONSTITUENCY\"" >&2
        echo "     $0 $ACTION --constituencies \"$CONSTITUENCY\"" >&2
        FAILED+=("$CONSTITUENCY")
        continue
    elif [[ -f "$WARD_FILE" ]]; then
        RESOLVED="wards"
    else
        RESOLVED="constituencies"
    fi

    if [[ "$RESOLVED" == "wards" ]]; then
        LOCAL_TARGET_FILE="$WARD_FILE"
        LOCAL_SCRAPED_FILE="$LOCAL_SCRAPED_WARDS/${SLUG}_search_targets.csv"
        REMOTE_DIR="$REMOTE_BASE/wards/$SLUG"
    else
        LOCAL_TARGET_FILE="$CONSTITUENCY_FILE"
        LOCAL_SCRAPED_FILE="$LOCAL_SCRAPED/${SLUG}_search_targets.csv"
        REMOTE_DIR="$REMOTE_BASE/constituencies/$SLUG"
    fi

    case "$ACTION" in
      push)
        if [[ ! -f "$LOCAL_TARGET_FILE" ]]; then
            echo "!! local file not found: $LOCAL_TARGET_FILE" >&2
            FAILED+=("$CONSTITUENCY")
            continue
        fi
        echo "=== push: $CONSTITUENCY ==="
        echo "Creating remote directory (if needed): $DEVICE:$REMOTE_DIR"
        if ! ssh "$DEVICE" "mkdir -p '$REMOTE_DIR'"; then
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
