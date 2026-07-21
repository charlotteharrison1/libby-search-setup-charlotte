#!/usr/bin/env bash
# Usage:
#   ./sync_scrape.sh push "Birmingham Ladywood"
#   ./sync_scrape.sh pull "Birmingham Ladywood"

set -euo pipefail

DEVICE="libby"
REMOTE_BASE="/home/pub/libby_download"
LOCAL_DATA="$(dirname "$0")/uk/data"

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
CONSTITUENCY="$2"

SLUG="$(to_slug "$CONSTITUENCY")"
LOCAL_FILE="$LOCAL_DATA/${SLUG}_search_targets.csv"
REMOTE_DIR="$REMOTE_BASE/$SLUG"

# --- commands ----------------------------------------------------------------

case "$ACTION" in
  push)
    if [[ ! -f "$LOCAL_FILE" ]]; then
        echo "Error: local file not found: $LOCAL_FILE" >&2
        exit 1
    fi
    echo "Creating remote directory (if needed): $DEVICE:$REMOTE_DIR"
    ssh "$DEVICE" "test -d '$REMOTE_DIR' || mkdir '$REMOTE_DIR'"
    echo "Uploading: $LOCAL_FILE → $DEVICE:$REMOTE_DIR/"
    scp "$LOCAL_FILE" "$DEVICE:$REMOTE_DIR/"
    echo "Done."
    ;;

  pull)
    REMOTE_FILE="$REMOTE_DIR/data/${SLUG}_search_targets.csv"
    echo "Downloading: $DEVICE:$REMOTE_FILE → $LOCAL_FILE"
    scp "$DEVICE:$REMOTE_FILE" "$LOCAL_FILE"
    echo "Done."
    ;;

  *)
    usage
    ;;
esac
