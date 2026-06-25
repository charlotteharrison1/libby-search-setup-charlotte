# UK pipeline

Builds a curated list of local Facebook groups for each **UK 2024 parliamentary
constituency**.

## How it works

1. **Merge redos** — fold any re-scraped rows (`data/redo_groups.csv`) back into
   the master scrape where they found more groups.
2. **Load & parse** — load the new scrape, parse each group's details into
   `public_y_n` / `members` / `posts_a_month` (`parsing.py`), and aggregate to
   one row per group URL per constituency.
3. **Geographic add-on** (`geo.py`) — pull in *local* groups from neighbouring
   constituencies that fall within a **population-density-scaled distance** of
   the constituency boundary. Denser seats reach less far; sparse seats reach
   further (bounded 500–3000 m by default).
4. **Combine & filter** — public groups only, dedupe by URL, attach the
   constituency name.
5. **AI assess** (`libby_core`) — get/generate a constituency description
   (cached in `data/constituency_descriptions.csv`) and have the LLM mark each
   group Yes/Unsure/No. Keep `!= No` and `members > 100`.
6. **Output** — one `output/Intermediate/<code>.csv` per constituency, combined
   into `output/output.csv` (full run) or `output/<name>-run.csv` (single).

The per-constituency loop is **resumable**: constituencies that already have an
`output/Intermediate/<code>.csv` are skipped on a full run.

## Inputs (`uk/data/`)

| File | Purpose |
|------|---------|
| `master_constituency_place_data_file.csv` | new scrape (one row per place, `groups` column) |
| `libby_list_groups_by_constituency.csv` | previous scrape (add-on candidates) |
| `redo_groups.csv` | re-scraped rows to merge in (optional) |
| `Westminster_PCON_(2010)_to_future_..._(V2).csv` | 2010→2024 constituency mapping |
| `Westminster_Parliamentary_Constituencies_..._BFC_...geojson` | constituency boundaries (large) |
| `parliament_con_data_inc_densities_2025.csv` | population densities (drives the add-on) |
| `constituency_descriptions.csv` | cached AI descriptions (reused; keyed by `PCON24CD`) |

## Run

```bash
# From the repo root:
python -m uk.pipeline                                   # all constituencies → output/output.csv
python -m uk.pipeline --constituency "Aldershot"        # one → output/Aldershot-run.csv
python -m uk.pipeline --constituency "Aldershot" --stop-before-ai-assessment
```

## Output

`output/output.csv` — final groups across all constituencies (columns include
`PCON24CD`, `PCON24NM`, `name`, `url`, `members`, `posts_a_month`,
`first_assessment`). A summary table (group counts, member sums per
constituency) is logged at the end.
