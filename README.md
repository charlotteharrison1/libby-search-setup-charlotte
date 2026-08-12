# Libby Search Setup

Find and assess local community **Facebook groups** for a geographic area, then
produce a curated list per area.

---

## Index — running the scripts

| Script | Command | What it does |
|---|---|---|
| **Prep** (generate + push, one seat) | `python -m uk.generate_search --constituency "Aldershot"` then `./sync_scrape.sh push "Aldershot"` | Builds the search-targets CSV for one constituency, then uploads it to the `libby` scraper device. |
| **Prep** (generate + push, batch) | `./batch_pipeline.sh prep "Aldershot" "Bolsover" "Clacton"` or `./batch_pipeline.sh prep --file constituencies.txt` | Same as above for a whole list at once. Skips constituencies already generated/pushed; rerun freely to top up a growing list. Add `--force` to redo everyone. |
| **Push** (upload only) | `./sync_scrape.sh push "Aldershot"` | Uploads an already-generated search-targets file to `libby:/home/pub/libby_download/constituencies/<slug>/`. |
| **Pull** (download, one seat) | `./sync_scrape.sh pull "Aldershot"` | Downloads the scraped file back from `libby` into `uk/data/scraped/`, once the external scraper has finished. |
| **Pull** (download, batch) | `./batch_pipeline.sh pull "Aldershot" "Bolsover" "Clacton"` or `./batch_pipeline.sh pull --file constituencies.txt` | Same as above for a whole list; a seat whose scrape isn't finished just fails that one — rerun later. |
| **Sync** (pull + process + stage, batch) | `./batch_pipeline.sh sync "Aldershot" "Bolsover" "Clacton"` or `./batch_pipeline.sh sync --file constituencies.txt` | Pulls only constituencies with *new* data on `libby`, runs `uk.pipeline` on them, and stages the finished `groups_*.csv` into the `Clacton-etc/inputs/` folder. Spends real OpenRouter (and no Data365) calls. Add `--force` to reprocess unchanged data. |
| **Process** (manual, one seat) | `python -m uk.pipeline --input uk/data/scraped/<slug>_search_targets.csv` | Parses → geo-expands → AI-assesses a scraped file into `uk/output/<constituency>-run.csv`. Use `--stop-before-ai-assessment` to skip the (billable) LLM step while inspecting results. |

`push`/`pull` also accept `--all` (every constituency *and* ward file), or
`--constituencies` / `--wards` to sweep just one or the other.
See [Quick start](#quick-start-uk) below for the full walkthrough, or the
[Repo layout](#repo-layout) section for how the pieces fit together.

---

## Index — running the ward scripts

A separate, smaller set of tools for researching a handful of specific
electoral wards rather than a whole constituency — e.g. when a constituency's
own search targets are missing a smaller or recently-redrawn area. Not wired
into `batch_pipeline.sh`; run these directly.

Each ward gets its own search-targets file, named the same way a
constituency's is (`slugify(ward_name)`) — mirroring the constituency
pattern deliberately, so pushing a newly-added ward can't silently overwrite
another ward's already-scraped remote data (they push to separate remote
folders, just like constituencies do).

| Script | Command | What it does |
|---|---|---|
| **Prep** (generate + push) | `python -m uk.generate_search_ward` then `./sync_scrape.sh push --wards` | Reads `uk/data/search_targets/adhoc_wards.csv` (one row per ward: `ward_name`, `constituency_name`, `local_authority`), asks a web-search-grounded LLM for colloquial names/landmarks per ward (plus real OpenStreetMap data — highstreets and the biggest residential roads — if `uk/data/reference/wards/` has a boundary shapefile), and writes one file per ward to `uk/data/search_targets/wards/`. Skips any ward whose file already exists — pass `--force` to regenerate. Then pushes every ward file. |
| **Push** (upload only) | `./sync_scrape.sh push --wards` (all ward files) or `./sync_scrape.sh push "<ward name>"` (one) | Uploads each ward's file to its own remote folder, same as any constituency push. |
| **Pull** (download) | `./sync_scrape.sh pull --wards` or `./sync_scrape.sh pull "<ward name>"` | Downloads each ward's scraped file back into `uk/data/scraped/wards/`. |
| **Raw dump** (no AI cost) | `python -m uk.raw_groups --input uk/data/scraped/wards/<ward>_search_targets.csv` | Explodes every group out of one scraped ward file, unfiltered — no AI assessment. Useful for a quick look before spending on the real process step. |
| **Process** | `python -m uk.pipeline_ward` | Processes every scraped ward file in `uk/data/scraped/wards/` (or pass `--input <file>` for just one) — aggregates, filters, and AI-assesses each ward separately (no geo add-on, no PCON-based caching — see the module docstring for why), writing `uk/output/wards/ward_groups_<ward>.csv` per ward plus a combined `ward_output.csv`. Use `--stop-before-ai-assessment` to inspect first, or `--force` to regenerate a ward's cached AI area-description instead of reusing it. |

See `uk/data/search_targets/adhoc_wards.csv.example` for the input format.

---

## Index — remote scripts (on the `libby` device)

Source of truth for these lives in `remote_scripts/` in this repo; the
deployed copies run from `/home/pub/libby_download/` on `libby`. Re-deploy
after editing with `scp remote_scripts/<script> libby:/home/pub/libby_download/`.

| Script | Command | What it does |
|---|---|---|
| **Set scrape target** | `ssh libby` then `cd /home/pub/libby_download && ./set_scrape_target.sh <slug>` | Points `clacton.json` at a given constituency's or ward's pushed search-targets file (rewrites `master_file_name`/`output_directory` only). Checks `constituencies/<slug>/` first, then `wards/<slug>/`. Refuses slugs that haven't been pushed yet — `--force` overrides. |
| **Pick next scrape target** | `ssh libby` then `cd /home/pub/libby_download && ./pick_next_scrape_target.sh` | Finds the oldest-pushed constituency or ward that hasn't been scraped yet (skipping whatever's currently active), scanning both `constituencies/*/` and `wards/*/`, and hands it to `set_scrape_target.sh`. Add `--dry-run` to just see what it would pick. |

See [step 2 of the quick start](#2-upload-to-scraper-device) for these in context.

---

## Where files live (UK)

Constituency and ward files are kept in separate, parallel locations at every
stage — a bare folder is the constituency side; the `wards/` subfolder (or,
for the two hand-edited input lists, just living alongside the constituency
ones) is the ward side.

```
uk/
├── data/
│   ├── reference/                     ← static, don't edit
│   │   ├── constituencies_2024.csv        (constituency lookup — used by both pipelines)
│   │   ├── *.geojson / densities / PCON mapping   (constituency geo add-on only)
│   │   └── wards/
│   │       └── WD_MAY_2026_UK_BFE.*       (ward boundary shapefile)
│   │
│   ├── search_targets/                ← hand-edited inputs + generated search-targets CSVs
│   │   ├── adhoc_wards.csv                (ward input list — edit this)
│   │   ├── nathan_targets.txt              (constituency batch list — edit this)
│   │   ├── <slug>_search_targets.csv      (generated, one per constituency)
│   │   └── wards/
│   │       └── <ward slug>_search_targets.csv  (generated, one file PER WARD)
│   │
│   ├── scraped/                       ← pulled back from the libby device
│   │   ├── <slug>_search_targets.csv
│   │   ├── master_constituency_place_data_file.csv
│   │   └── wards/
│   │       └── <ward slug>_search_targets.csv  (one file per ward, pulled separately)
│   │
│   ├── descriptions.csv               ← AI area-description cache (constituency)
│   └── ward_descriptions.csv          ← AI area-description cache (ward)
│
└── output/                            ← final, filtered, AI-assessed results
    ├── groups_<Constituency Name>.csv
    ├── output.csv                         (all constituencies combined)
    ├── intermediate/<PCON24CD>.csv        (per-constituency resumability cache)
    └── wards/
        ├── ward_groups_<ward>.csv
        └── ward_output.csv                (all wards combined)
```

`uk.pipeline` (constituency) and `uk.pipeline_ward` (ward) never write into
each other's folders — see the [Index — running the ward
scripts](#index--running-the-ward-scripts) table above for which script
produces which file.

---

## Quick start (UK)

### 0. Prerequisites

```bash
pip install -e .
# Add to .env in repo root:
# OPEN_ROUTER_KEY=sk-or-...
```

Requires `uk/data/constituencies_2024.csv` (FID, PCON24CD, PCON24NM, LONG, LAT, …).

---

### 1. Generate search targets

Run from the **repo root**:

```bash
python -m uk.generate_search --constituency "Finchley and Golders Green"
```

Output: `uk/data/search_targets/finchley_and_golders_green_search_targets.csv`
Contains one row per place name with `processed=False` and an empty `groups` column.

To regenerate a file that already has scraped data (overwrites everything):

```bash
python -m uk.generate_search --constituency "Finchley and Golders Green" --force
```

---

### 2. Upload to scraper device

```bash
./sync_scrape.sh push "Finchley and Golders Green"
```

This creates `libby:/home/pub/libby_download/constituencies/finchley_and_golders_green/` (if needed) and SCPs the file there. (Wards push to `libby:/home/pub/libby_download/wards/<ward-slug>/` instead — see [Index — running the ward scripts](#index--running-the-ward-scripts).)

Then configure and run the external Facebook scraper (`libby_download`) with:

```json
{
  "master_file_name": "path/to/finchley_and_golders_green_search_targets.csv",
  "search_column": "search_string",
  "scroll_column": "scroll"
}
```

The scraper fills in the `groups` column and sets `processed=True` for each row.

Instead of hand-editing that JSON before every scrape, `set_scrape_target.sh`
(deployed on libby at `/home/pub/libby_download/set_scrape_target.sh` —
source of truth kept in this repo under `remote_scripts/`) rewrites just the
`master_file_name` / `output_directory` fields for you:

```bash
ssh libby
cd /home/pub/libby_download
./set_scrape_target.sh bolsover        # slug, not the full constituency name
```

It refuses to point the scraper at a slug that hasn't actually been pushed
yet (`--force` overrides that check), and leaves every other field in
`clacton.json` — and the scraper itself — untouched. If you edit
`remote_scripts/set_scrape_target.sh` here, redeploy with:

```bash
scp remote_scripts/set_scrape_target.sh libby:/home/pub/libby_download/
```

Once scraping is complete, retrieve the file:

```bash
./sync_scrape.sh pull "Finchley and Golders Green"
```

This downloads the scraped file from `…/finchley_and_golders_green/data/` back to `uk/data/scraped/`.

#### Running a list of constituencies at once

`batch_pipeline.sh` runs steps 1 and 2 for several constituencies in one go,
instead of repeating `generate_search` + `sync_scrape.sh push` by hand for
each one:

```bash
./batch_pipeline.sh prep "Aldershot" "Bolsover" "Clacton"
# or, one constituency name per line in a file:
./batch_pipeline.sh prep --file constituencies.txt
```

This generates search targets and pushes every constituency that generated
successfully; failures are reported at the end rather than stopping the batch.

`prep` tracks what it's already done, so you can keep adding constituencies
to a running list and rerun the same command — it will only generate ones
that don't have a search-targets file yet, and only push ones whose file has
changed since the last successful push (recorded in
`uk/data/search_targets/.push_manifest`). Pass `--force` to regenerate and
re-push everything in the list regardless.

Then, as before, run the scraper on the device yourself for each one. Once
scraping is done, pull them all back:

```bash
./batch_pipeline.sh pull "Aldershot" "Bolsover" "Clacton"
# or:
./batch_pipeline.sh pull --file constituencies.txt
```

A constituency whose scrape isn't finished yet just fails that one pull —
rerun the same command later to pick up the rest.

To go all the way from "libby has new data" to a finished, staged output file
in one step, use `sync` instead of `pull`:

```bash
./batch_pipeline.sh sync "Aldershot" "Bolsover" "Clacton"
# or:
./batch_pipeline.sh sync --file constituencies.txt
```

For each constituency, `sync`:

1. Checks (via SSH, no download) whether the scraped file on libby has
   changed since the last successful sync — tracked in
   `uk/data/scraped/.pull_manifest`. Not-ready or unchanged constituencies
   are skipped.
2. Pulls it, then runs `uk.pipeline` on just that constituency (forcing
   reprocessing even if an `Intermediate/<code>.csv` already exists from a
   previous run — otherwise the fresh pull would be silently ignored).
3. Moves the resulting `uk/output/groups_{name}.csv` into
   `/Users/charlotte/vs_code/Clacton-etc/inputs/` as a staging area.

`inputs/` is *not* wired into `Clacton-etc`'s `data_collection.py` — that
script only ever reads `Clacton-etc/groups/` (hardcoded), and running it
fires real billable Data365 API calls. Moving a file from `inputs/` into
`groups/` is a deliberate manual step, by design, so nothing reaches the
billable script without a human looking at it first. Pass `--force` to
reprocess and re-stage a constituency even if libby's copy hasn't changed.

Each `sync` run also spends real OpenRouter LLM calls during AI assessment
(same as any `uk.pipeline` run) — that part isn't free either, just less
directly billable than Data365.

---

### 3. Process scraped results

Run from the **repo root**, passing the scraped file directly:

```bash
python -m uk.pipeline --input uk/data/scraped/finchley_and_golders_green_search_targets.csv
```

To inspect results before the AI assessment step (faster, no LLM calls):

```bash
python -m uk.pipeline --input uk/data/scraped/finchley_and_golders_green_search_targets.csv --stop-before-ai-assessment --constituency "Finchley and Golders Green"
```

Output: `uk/output/<constituency>-run.csv`

---

### Full multi-constituency run

```bash
python -m uk.generate_search                  # all 650 constituencies
# [scrape externally]
python -m uk.pipeline                         # all → uk/output/output.csv
```

---

## Repo layout

Two pipelines share one core:

- **`uk/`** — UK 2024 parliamentary constituencies.
- **`us/`** — US congressional districts.
- **`libby_core/`** — shared code both pipelines depend on.

### Lifecycle (each pipeline has three stages)

```
1. GENERATE search targets   →   2. SCRAPE (external)   →   3. PROCESS
   what to search for             fills the `groups`         parse → (UK) geo
   per area                       column per target          expand → keep public
                                                             → AI-assess → output
```

The **scrape** itself is done outside this repo (a Facebook group scraper reads
the search targets and writes back a `groups` column). This repo owns stage 1
(generate) and stage 3 (process).

- **Generate:** UK = `uk/generate_search.py` (AI picks popular place names per
  constituency); US = `us/download_places.py` (Overture places → locality search
  filters).
- **Process:** `uk/pipeline.py` / `us/pipeline.py`.

## Layout

```
libby_core/        SHARED building blocks
  parse_groups.py  parse FB group fields (privacy, members, posts) from a scrape
  ai.py            OpenRouter/LLM client + concurrent row iteration
  descriptions.py  generate + cache an AI description of an area
  assessment.py    LLM relevance check of each group vs. the area description
  settings.py      shared config (OpenRouter key from the root .env)

uk/                UK pipeline (see uk/README.md)
  generate_search.py   stage 1: AI place names → master scrape file
  pipeline.py  data_loading.py  geo.py  parsing.py  settings.py   stage 3
  data/  output/

us/                US pipeline (see us/README.md)
  download_places.py   stage 1: Overture places → locality search filters
  pipeline.py  settings.py                                        stage 3
  data/  output/

tests/             unit tests for the shared parser
```

### What's shared vs. pipeline-specific

| Capability                     | Where it lives          |
|--------------------------------|-------------------------|
| Parse FB group fields          | `libby_core/parse_groups.py` (US uses this; UK uses its own `uk/parsing.py` — see note below) |
| LLM client                     | `libby_core/ai.py`      |
| Area description (cached)      | `libby_core/descriptions.py` |
| Group relevance assessment     | `libby_core/assessment.py` |
| Geographic density add-on      | **UK only** — `uk/geo.py` |
| Scrape ingestion / area defn   | per-pipeline `data_loading` / `settings` |

> **Why two parsers?** The UK scrape encodes posting frequency as *per
> day/month/year* and the whole UK geo/filter chain is keyed to the columns
> `members` / `posts_a_month` / `public_y_n`. `libby_core/parse_groups.py`
> (used by US) is the more robust parser but emits `member_count` /
> `posts_per_day` / `privacy`. They are kept separate deliberately rather than
> forcing one onto the other's schema.

## Setup

```bash
# Python 3.11+ (developed on the pyenv env "libbylist", Python 3.12).
pip install -e .            # or: pip install -r requirements.txt
pip install -e ".[us,dev]"  # + duckdb (US data-prep) and pytest
```

Create a `.env` in the repo root with your OpenRouter key (shared by both
pipelines):

```
OPEN_ROUTER_KEY=sk-or-...
```

## Run

```bash
# From the repo root, run as modules.

# Stage 1 — GENERATE search targets (then scrape externally):
python -m uk.generate_search --constituency "Aldershot"   # one seat
python -m uk.generate_search                              # all seats
python -m us.download_places --district il-14             # Overture place download

# Stage 3 — PROCESS the scraped results:
python -m uk.pipeline --constituency "Sittingbourne and Sheppey"   # one seat
python -m uk.pipeline                                              # all seats
python -m us.pipeline --district il-14                             # one district

# Inspect inputs without spending LLM calls:
python -m uk.pipeline --constituency "..." --stop-before-ai-assessment
python -m us.pipeline --stop-before-ai-assessment
```


## Tests

```bash
pytest
```

## Notes

- `uk/data/`, `us/data/`, both `output/` folders, and `.env` are git-ignored.
- The AI steps use OpenRouter (Gemini by default); models are set in
  `libby_core/descriptions.py` and `libby_core/assessment.py`.
