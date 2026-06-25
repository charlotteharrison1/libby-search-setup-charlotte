# US pipeline

Builds a curated list of local Facebook groups for a **US congressional
district** (default: Illinois 14th, `il-14`).

## How it works

1. **Parse & aggregate** (`pipeline.py`) — load the scraped `*_places.csv` and
   `*_localities.csv`, parse each group's details with
   `libby_core/parse_groups.py` (→ `privacy` / `member_count` / `posts_per_day`),
   and aggregate to one row per group URL — recording every place/locality that
   found it (`source_types`, `source_values`).
2. **Keep public** — public groups only.
3. **AI assess** (`libby_core`) — get/generate a district description (cached in
   `data/district_descriptions.csv`) and have the LLM mark each group
   Yes/Unsure/No. Keep `!= No` and `member_count > 100`.
4. **Output** — `output/<district>_libby_list.csv`.

This mirrors the UK pipeline. The UK-only geographic density add-on is **not**
part of the US flow.

## Inputs (`us/data/<district>/`)

| File | Purpose |
|------|---------|
| `<district>_places.csv` | scraped groups keyed by place name (needs a `groups` column) |
| `<district>_localities.csv` | scraped groups keyed by locality (needs a `groups` column) |

## Stage 1 — Generate search targets — `download_places.py`

This is the US equivalent of `uk/generate_search.py`: it decides *what to search
for*. Run **once per district**, before scraping, to build the place/locality
lists (the `filters_param` column is the Facebook search filter — the actual
"search string"):

```bash
python -m us.download_places --district il-14 --statefp 17 --cd-fp 14
```

It downloads the Census district boundary, queries **Overture Maps** places
within it (via DuckDB + S3), and dumps the category list.

> ⚠️ **Manual step:** the original workflow then hand-curates a `filters.csv`
> (locality → Facebook search-filter URL) and shapes the final
> `<district>_places.csv` / `<district>_localities.csv`. That curation is
> intentionally left manual — see the notes in `download_places.py`.

## Run

```bash
# From the repo root:
python -m us.pipeline --district il-14
python -m us.pipeline --district il-14 --district-name "Illinois 14th Congressional District"
python -m us.pipeline --stop-before-ai-assessment      # inspect public groups, no LLM calls
```

To process a different district, add `us/data/<id>/<id>_places.csv` +
`<id>_localities.csv` and pass `--district <id> --district-name "..."`.

## Output

`output/<district>_libby_list.csv` — final groups (columns include `name`,
`url`, `privacy`, `member_count`, `posts_per_day`, `source_values`,
`first_assessment`).
