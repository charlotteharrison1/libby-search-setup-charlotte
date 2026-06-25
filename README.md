# Libby Search Setup

Find and assess local community **Facebook groups** for a geographic area, then
produce a curated list per area. Two pipelines share one core:

- **`uk/`** — UK 2024 parliamentary constituencies.
- **`us/`** — US congressional districts.
- **`libby_core/`** — shared code both pipelines depend on.

Each pipeline: parse scraped groups → (UK only) geographically expand by
population density → keep public groups → **AI-assess relevance** → filter &
dedupe → write a final CSV.

## Layout

```
libby_core/        SHARED building blocks
  parse_groups.py  parse FB group fields (privacy, members, posts) from a scrape
  ai.py            OpenRouter/LLM client + concurrent row iteration
  descriptions.py  generate + cache an AI description of an area
  assessment.py    LLM relevance check of each group vs. the area description
  settings.py      shared config (OpenRouter key from the root .env)

uk/                UK pipeline (see uk/README.md)
  pipeline.py  data_loading.py  geo.py  parsing.py  settings.py
  data/  output/

us/                US pipeline (see us/README.md)
  pipeline.py  download_places.py  settings.py
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
# From the repo root, run pipelines as modules:
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
