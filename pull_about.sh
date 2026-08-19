#!/usr/bin/env bash
# Pulls scrape_group_about.py's output (About-page data for groups already
# found by the main scraper) from the libby device to this repo. Companion
# to sync_scrape.sh, but pull-only: there's no "push" side here, since
# scrape_group_about.py reads groups_file directly off the remote device
# (the main scrape's own output, already pushed/scraped there) rather than
# from anything this script would need to upload first.
#
# Usage:
#   ./pull_about.sh lincoln
#   ./pull_about.sh lincoln walsall_and_bloxwich
#   ./pull_about.sh --all                          (every about-scrape output currently on the device)
#   ./pull_about.sh --wards "Name"                  (one name, forced to wards/)
#   ./pull_about.sh --constituencies "Name"         (one name, forced to constituencies/)
#
# A bare name (no --wards/--constituencies) is resolved to whichever
# category already has that slug's main-scrape data pulled locally (via
# sync_scrape.sh pull) — since about-scraping only makes sense once the main
# scrape exists. If neither is found locally (e.g. you never pulled the raw
# scrape, only care about the about-data), this falls back to checking the
# remote about_pages/ directory directly. If a slug matches in BOTH
# categories, that's refused as ambiguous, same as sync_scrape.sh.

set -uo pipefail

DEVICE="libby"
REMOTE_BASE="/home/pub/libby_download"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
LOCAL_SCRAPED="$REPO_ROOT/uk/data/scraped"
LOCAL_SCRAPED_WARDS="$LOCAL_SCRAPED/wards"
LOCAL_ABOUT="$REPO_ROOT/uk/data/scraped_about"
LOCAL_ABOUT_WARDS="$LOCAL_ABOUT/wards"

mkdir -p "$LOCAL_ABOUT_WARDS"

usage() {
    echo "Usage: $0 <name> [<name> ...]"
    echo "       $0 --all                                  (every about-scrape output on the device)"
    echo "       $0 --wards <name> [<name> ...]             (force these names to wards/)"
    echo "       $0 --constituencies <name> [<name> ...]    (force these names to constituencies/)"
    exit 1
}

to_slug() {
    (cd "$REPO_ROOT" && python3 -m uk.generate_search --print-slug "$1")
}

# --- args ----------------------------------------------------------------

[[ $# -lt 1 ]] && usage

CATEGORY=""   # "", "wards", or "constituencies" — "" means auto-detect per name
NAMES=()

case "$1" in
  --all)
    echo "Listing about-scrape output on $DEVICE..."
    ALL_REMOTE="$(ssh "$DEVICE" "find '$REMOTE_BASE/about_pages' -mindepth 3 -maxdepth 3 -name '*_search_targets_about.csv' 2>/dev/null")"
    if [[ -z "$ALL_REMOTE" ]]; then
        echo "Nothing found under $DEVICE:$REMOTE_BASE/about_pages" >&2
        exit 1
    fi
    # Each line: .../about_pages/<category>/<slug>/<slug>_search_targets_about.csv
    while IFS= read -r line; do
        slug="$(basename "$(dirname "$line")")"
        NAMES+=("$slug")
    done <<< "$ALL_REMOTE"
    ;;
  --wards|--constituencies)
    CATEGORY="${1#--}"
    shift
    [[ $# -eq 0 ]] && usage
    NAMES=("$@")
    ;;
  *)
    NAMES=("$@")
    ;;
esac

# --- pull ------------------------------------------------------------------

FAILED=()
SUCCEEDED=()

for NAME in "${NAMES[@]}"; do
    SLUG="$(to_slug "$NAME")" || { echo "!! could not derive slug for: $NAME" >&2; FAILED+=("$NAME"); continue; }

    if [[ -n "$CATEGORY" ]]; then
        RESOLVED="$CATEGORY"
    else
        LOCAL_WARD_SCRAPE="$LOCAL_SCRAPED_WARDS/${SLUG}_search_targets.csv"
        LOCAL_CONSTITUENCY_SCRAPE="$LOCAL_SCRAPED/${SLUG}_search_targets.csv"
        if [[ -f "$LOCAL_WARD_SCRAPE" && -f "$LOCAL_CONSTITUENCY_SCRAPE" ]]; then
            echo "!! '$NAME' (slug '$SLUG') matches locally-pulled scrape data in BOTH categories — ambiguous. Disambiguate with:" >&2
            echo "     $0 --wards \"$NAME\"" >&2
            echo "     $0 --constituencies \"$NAME\"" >&2
            FAILED+=("$NAME")
            continue
        elif [[ -f "$LOCAL_WARD_SCRAPE" ]]; then
            RESOLVED="wards"
        elif [[ -f "$LOCAL_CONSTITUENCY_SCRAPE" ]]; then
            RESOLVED="constituencies"
        else
            # No local proxy file (never pulled the raw scrape) — fall back
            # to asking the remote device directly which category has this
            # slug's about-output.
            HAS_WARD="$(ssh "$DEVICE" "[[ -f '$REMOTE_BASE/about_pages/wards/$SLUG/${SLUG}_search_targets_about.csv' ]] && echo yes")"
            HAS_CONSTITUENCY="$(ssh "$DEVICE" "[[ -f '$REMOTE_BASE/about_pages/constituencies/$SLUG/${SLUG}_search_targets_about.csv' ]] && echo yes")"
            if [[ "$HAS_WARD" == "yes" && "$HAS_CONSTITUENCY" == "yes" ]]; then
                echo "!! '$NAME' (slug '$SLUG') matches remote about-output in BOTH categories — ambiguous. Disambiguate with:" >&2
                echo "     $0 --wards \"$NAME\"" >&2
                echo "     $0 --constituencies \"$NAME\"" >&2
                FAILED+=("$NAME")
                continue
            elif [[ "$HAS_WARD" == "yes" ]]; then
                RESOLVED="wards"
            elif [[ "$HAS_CONSTITUENCY" == "yes" ]]; then
                RESOLVED="constituencies"
            else
                echo "!! could not find about-output for '$NAME' (slug '$SLUG') locally or on $DEVICE — has it been about-scraped yet?" >&2
                FAILED+=("$NAME")
                continue
            fi
        fi
    fi

    if [[ "$RESOLVED" == "wards" ]]; then
        LOCAL_DEST="$LOCAL_ABOUT_WARDS/${SLUG}_search_targets_about.csv"
        REMOTE_FILE="$REMOTE_BASE/about_pages/wards/$SLUG/${SLUG}_search_targets_about.csv"
    else
        LOCAL_DEST="$LOCAL_ABOUT/${SLUG}_search_targets_about.csv"
        REMOTE_FILE="$REMOTE_BASE/about_pages/constituencies/$SLUG/${SLUG}_search_targets_about.csv"
    fi

    echo "=== pull: $NAME ($RESOLVED) ==="
    echo "Downloading: $DEVICE:$REMOTE_FILE → $LOCAL_DEST"
    if scp "$DEVICE:$REMOTE_FILE" "$LOCAL_DEST"; then
        echo "Done: $NAME"
        SUCCEEDED+=("$NAME")
    else
        echo "!! download failed for: $NAME (about-scrape may not have produced output yet)" >&2
        FAILED+=("$NAME")
    fi
done

echo
echo "pull complete: ${#SUCCEEDED[@]} succeeded, ${#FAILED[@]} failed"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "Failed: ${FAILED[*]}"
    exit 1
fi
