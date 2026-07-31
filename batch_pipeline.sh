#!/usr/bin/env bash
# Batch-run a list of constituencies through the two manual sides of the
# pipeline that used to be done one-by-one:
#
#   prep  =  generate search targets  +  push to the libby device   (stage 1)
#   pull  =  pull scraped results back from the libby device        (after stage 2)
#   sync  =  pull only constituencies with NEW data on libby, reprocess just
#            those through uk.pipeline, and move finished groups_{name}.csv
#            files into the Clacton-etc inputs/ staging folder
#
# You still start the scraper on the libby device yourself between prep and
# pull/sync — this just removes the need to babysit each constituency
# through generate_search / sync_scrape.sh / uk.pipeline individually.
#
# Usage:
#   ./batch_pipeline.sh prep "Aldershot" "Bolsover" "Clacton"
#   ./batch_pipeline.sh prep --file constituencies.txt
#   ./batch_pipeline.sh prep --force "Aldershot"        # regenerate even if already prepped
#   ./batch_pipeline.sh pull "Aldershot" "Bolsover" "Clacton"
#   ./batch_pipeline.sh pull --file constituencies.txt
#   ./batch_pipeline.sh sync "Aldershot" "Bolsover" "Clacton"
#   ./batch_pipeline.sh sync --file constituencies.txt
#   ./batch_pipeline.sh sync --force "Aldershot"        # reprocess even if libby data is unchanged
#
# --file expects one constituency name per line; blank lines and lines
# starting with # are ignored.
#
# prep skips generating a constituency that already has a search-targets
# file, and separately skips pushing one whose file content hasn't changed
# since its last successful push (tracked in a local manifest) — so you can
# keep adding names to a growing list and rerun prep without re-spending LLM
# calls or re-uploading files the device already has. Pass --force to
# regenerate + re-push everyone in the list regardless.
#
# sync checks (via SSH) whether each constituency's scraped file on libby has
# changed since the last successful sync (tracked in a local manifest); a
# constituency with no file yet, or an unchanged file, is left alone. Note
# this only lands processed output in Clacton-etc/inputs/ as a staging area —
# it does NOT touch Clacton-etc/groups/, which is what data_collection.py
# (billable) actually reads. Moving a file from inputs/ into groups/ is a
# deliberate manual step.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SYNC_SCRAPE="$REPO_ROOT/sync_scrape.sh"
SEARCH_TARGETS_DIR="$REPO_ROOT/uk/data/search_targets"
SCRAPED_DIR="$REPO_ROOT/uk/data/scraped"
OUTPUT_DIR="$REPO_ROOT/uk/output"
INTERMEDIATE_DIR="$OUTPUT_DIR/intermediate"
PUSH_MANIFEST="$SEARCH_TARGETS_DIR/.push_manifest"
PULL_MANIFEST="$SCRAPED_DIR/.pull_manifest"
INPUTS_DIR="${CLACTON_INPUTS_DIR:-/Users/charlotte/vs_code/Clacton-etc/inputs}"

# Must match sync_scrape.sh.
DEVICE="libby"
REMOTE_BASE="/home/pub/libby_download"

# --- manifests: slug<TAB>sha256, one line per constituency -----------------
# Shared by push (prep) and remote-data (sync) tracking; pass the manifest
# file as $1.

manifest_get() {
    local manifest="$1" slug="$2"
    [[ -f "$manifest" ]] || return 0
    awk -F'\t' -v s="$slug" '$1==s{print $2; exit}' "$manifest"
}

manifest_set() {
    local manifest="$1" slug="$2" hash="$3" tmp
    tmp="$(mktemp "${manifest}.XXXXXX")"
    [[ -f "$manifest" ]] && awk -F'\t' -v s="$slug" '$1!=s' "$manifest" > "$tmp"
    printf '%s\t%s\n' "$slug" "$hash" >> "$tmp"
    mv "$tmp" "$manifest"
}

# sha256 of a remote file over SSH; empty output if it doesn't exist yet.
remote_hash() {
    ssh "$DEVICE" "sha256sum '$1' 2>/dev/null || shasum -a 256 '$1' 2>/dev/null" 2>/dev/null | awk '{print $1}'
}

usage() {
    cat <<EOF
Usage:
  $0 prep [--force] <constituency> [<constituency> ...]
  $0 prep [--force] --file <path>
  $0 pull <constituency> [<constituency> ...]
  $0 pull --file <path>
  $0 sync [--force] <constituency> [<constituency> ...]
  $0 sync [--force] --file <path>

  prep = generate search targets, then push each to the libby device.
         Skips constituencies already generated unless --force is given.
  pull = pull scraped results for each back from the libby device.
  sync = pull + reprocess + stage into Clacton-etc/inputs/, but only for
         constituencies with new data on libby since the last sync.
EOF
    exit 1
}

[[ $# -lt 1 ]] && usage
MODE="$1"; shift
[[ "$MODE" != "prep" && "$MODE" != "pull" && "$MODE" != "sync" ]] && usage
[[ $# -lt 1 ]] && usage

FORCE=0
if [[ ( "$MODE" == "prep" || "$MODE" == "sync" ) && "${1:-}" == "--force" ]]; then
    FORCE=1
    shift
    [[ $# -lt 1 ]] && usage
fi

CONSTITUENCIES=()
if [[ "$1" == "--file" ]]; then
    [[ $# -lt 2 ]] && usage
    LIST_FILE="$2"
    [[ -f "$LIST_FILE" ]] || { echo "Error: file not found: $LIST_FILE" >&2; exit 1; }
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$line" ]] && continue
        CONSTITUENCIES+=("$line")
    done < "$LIST_FILE"
else
    CONSTITUENCIES=("$@")
fi

[[ ${#CONSTITUENCIES[@]} -eq 0 ]] && { echo "Error: no constituencies given" >&2; exit 1; }

echo "${#CONSTITUENCIES[@]} constituencies: ${CONSTITUENCIES[*]}"
echo

if [[ "$MODE" == "prep" ]]; then
    GENERATE_FAILED=()
    GEN_SKIPPED=()
    PUSHED=()
    PUSH_SKIPPED=()
    PUSH_FAILED=()

    for NAME in "${CONSTITUENCIES[@]}"; do
        SLUG="$(cd "$REPO_ROOT" && python3 -m uk.generate_search --print-slug "$NAME")"
        if [[ -z "$SLUG" ]]; then
            echo "!! could not derive slug for: $NAME" >&2
            GENERATE_FAILED+=("$NAME")
            continue
        fi
        TARGET_FILE="$SEARCH_TARGETS_DIR/${SLUG}_search_targets.csv"

        if [[ "$FORCE" -eq 0 && -f "$TARGET_FILE" ]]; then
            echo "=== skip generate_search (already exists): $NAME ==="
            GEN_SKIPPED+=("$NAME")
        else
            echo "=== generate_search: $NAME ==="
            GEN_ARGS=(--constituency "$NAME")
            [[ "$FORCE" -eq 1 ]] && GEN_ARGS+=(--force)
            if ! (cd "$REPO_ROOT" && python3 -m uk.generate_search "${GEN_ARGS[@]}"); then
                echo "!! generate_search failed for: $NAME" >&2
                GENERATE_FAILED+=("$NAME")
                continue
            fi
            echo
        fi

        CURRENT_HASH="$(shasum -a 256 "$TARGET_FILE" 2>/dev/null | awk '{print $1}')"
        RECORDED_HASH="$(manifest_get "$PUSH_MANIFEST" "$SLUG")"

        if [[ "$FORCE" -eq 0 && -n "$CURRENT_HASH" && "$CURRENT_HASH" == "$RECORDED_HASH" ]]; then
            echo "=== skip push (unchanged since last push): $NAME ==="
            PUSH_SKIPPED+=("$NAME")
            continue
        fi

        if "$SYNC_SCRAPE" push "$NAME"; then
            PUSHED+=("$NAME")
            [[ -n "$CURRENT_HASH" ]] && manifest_set "$PUSH_MANIFEST" "$SLUG" "$CURRENT_HASH"
        else
            PUSH_FAILED+=("$NAME")
        fi
    done

    echo
    echo "prep summary: ${#PUSHED[@]} pushed, ${#GEN_SKIPPED[@]} generation skipped (already existed), ${#PUSH_SKIPPED[@]} push skipped (unchanged since last push), ${#GENERATE_FAILED[@]} generate failed, ${#PUSH_FAILED[@]} push failed"
    [[ ${#GENERATE_FAILED[@]} -gt 0 ]] && echo "generate_search failed for: ${GENERATE_FAILED[*]}"
    [[ ${#PUSH_FAILED[@]} -gt 0 ]] && echo "push failed for: ${PUSH_FAILED[*]}"

    [[ ${#GENERATE_FAILED[@]} -gt 0 || ${#PUSH_FAILED[@]} -gt 0 ]] && exit 1
    exit 0

elif [[ "$MODE" == "pull" ]]; then
    "$SYNC_SCRAPE" pull "${CONSTITUENCIES[@]}"

else  # sync
    NOT_READY=()
    UNCHANGED=()
    PULL_FAILED=()
    PIPELINE_FAILED=()
    NOTHING_TO_MOVE=()
    MOVED=()

    for NAME in "${CONSTITUENCIES[@]}"; do
        SLUG="$(cd "$REPO_ROOT" && python3 -m uk.generate_search --print-slug "$NAME")"
        if [[ -z "$SLUG" ]]; then
            echo "!! could not derive slug for: $NAME" >&2
            PULL_FAILED+=("$NAME")
            continue
        fi

        REMOTE_FILE="$REMOTE_BASE/$SLUG/data/${SLUG}_search_targets.csv"
        echo "=== checking libby for new data: $NAME ==="
        NEW_HASH="$(remote_hash "$REMOTE_FILE")"
        if [[ -z "$NEW_HASH" ]]; then
            echo "  no scraped file on libby yet — skipping"
            NOT_READY+=("$NAME")
            continue
        fi

        RECORDED_HASH="$(manifest_get "$PULL_MANIFEST" "$SLUG")"
        if [[ "$FORCE" -eq 0 && "$NEW_HASH" == "$RECORDED_HASH" ]]; then
            echo "  unchanged since last sync — skipping"
            UNCHANGED+=("$NAME")
            continue
        fi

        echo "=== pulling: $NAME ==="
        if ! "$SYNC_SCRAPE" pull "$NAME"; then
            PULL_FAILED+=("$NAME")
            continue
        fi

        LOCAL_SCRAPED_FILE="$SCRAPED_DIR/${SLUG}_search_targets.csv"

        # Bust uk.pipeline's per-constituency resumability cache: it skips
        # re-processing a constituency whose Intermediate/<code>.csv already
        # exists, which would silently ignore the fresh pull otherwise.
        CODES="$(cd "$REPO_ROOT" && python3 - "$LOCAL_SCRAPED_FILE" <<'PY'
import sys
import pandas as pd
df = pd.read_csv(sys.argv[1])
print(" ".join(sorted(set(df["PCON24CD"].dropna().astype(str)))))
PY
)"
        for CODE in $CODES; do
            rm -f "$INTERMEDIATE_DIR/${CODE}.csv"
        done

        echo "=== processing: $NAME ==="
        if ! (cd "$REPO_ROOT" && python3 -m uk.pipeline --input "$LOCAL_SCRAPED_FILE" --constituency "$NAME"); then
            echo "!! uk.pipeline failed for: $NAME" >&2
            PIPELINE_FAILED+=("$NAME")
            continue
        fi

        RUN_OUTPUT="$OUTPUT_DIR/groups_${NAME}.csv"
        if [[ ! -f "$RUN_OUTPUT" ]]; then
            echo "  no groups survived filtering for $NAME — nothing to move"
            NOTHING_TO_MOVE+=("$NAME")
        else
            mkdir -p "$INPUTS_DIR"
            mv -f "$RUN_OUTPUT" "$INPUTS_DIR/"
            echo "  moved → $INPUTS_DIR/groups_${NAME}.csv"
            MOVED+=("$NAME")
        fi

        manifest_set "$PULL_MANIFEST" "$SLUG" "$NEW_HASH"
        echo
    done

    echo
    echo "sync summary: ${#MOVED[@]} moved to inputs/, ${#NOTHING_TO_MOVE[@]} processed with nothing to move, ${#UNCHANGED[@]} unchanged (skipped), ${#NOT_READY[@]} not ready on libby, ${#PULL_FAILED[@]} pull failed, ${#PIPELINE_FAILED[@]} pipeline failed"
    [[ ${#PULL_FAILED[@]} -gt 0 ]] && echo "pull failed for: ${PULL_FAILED[*]}"
    [[ ${#PIPELINE_FAILED[@]} -gt 0 ]] && echo "pipeline failed for: ${PIPELINE_FAILED[*]}"

    [[ ${#PULL_FAILED[@]} -gt 0 || ${#PIPELINE_FAILED[@]} -gt 0 ]] && exit 1
    exit 0
fi
