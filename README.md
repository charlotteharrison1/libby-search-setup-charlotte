# Libby Search Setup

Find and assess local community **Facebook groups** for a geographic area, then
produce a curated list per area. Two pipelines share one core:

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

See **`uk/README.md`** and **`us/README.md`** for inputs, outputs, and details.

## Tests

```bash
pytest
```

## Notes

- `uk/data/`, `us/data/`, both `output/` folders, and `.env` are git-ignored.
- The AI steps use OpenRouter (Gemini by default); models are set in
  `libby_core/descriptions.py` and `libby_core/assessment.py`.
