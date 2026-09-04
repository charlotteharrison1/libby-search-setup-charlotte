"""Load a group's own Facebook About-page text, for use as extra context in
the AI relevance assessment (see ``libby_core/assessment.py``'s
``context_column``).

Reads an about_pages CSV — the shape ``pull_about.sh`` pulls down and
``libby_download``'s ``extract_about.py`` originally wrote: a ``url`` column
and an ``about`` column holding JSON with a ``raw_text`` key (plus
``sections``/``section_headings_found``/``member_count``, unused here).

Shared by both ``uk/pipeline.py`` (constituencies) and
``uk/pipeline_ward.py`` (wards) — this module doesn't need to know which,
since ``load_global_about_context`` unions every about_pages CSV found
anywhere under ``uk/settings.ABOUT_PAGES_DIR`` (constituencies and wards
alike) into one URL-keyed cache, rather than routing to one file per area.

Why global, not per-area: candidacy for a given area is decided entirely by
that area's own search/scrape step — the about-context lookup only ever
attaches text onto a URL already present in that area's own candidate list,
it never introduces a new one. So a group that's a genuine local match for
one area but a same-name false positive in another (a real, recurring
pattern — see the module's git history) gets correctly resolved either way,
using whichever area's about-scrape happened to capture it first. A global
cache just means that resolution doesn't have to be re-earned per area: once
any area's about-scrape has covered a given group, every other area that
independently stumbles on the same URL benefits for free, which matters a
lot given how slow the About scraper is to run broadly.
"""

import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 1500


def _clean_text(text: str) -> str:
    """Strip unpaired UTF-16 surrogates. Facebook-scraped text occasionally
    contains them (mangled emoji/reactions), which raise
    ``UnicodeEncodeError`` deep in the HTTP client when the LLM request is
    sent — same issue ``libby_core/parse_groups.py`` already works around
    for group names; About-page text is long-form scraped content, so it's
    considerably more likely to hit this than a short group name."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def load_about_context(path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, str]:
    """Return {url: about_raw_text} for the about_pages CSV at ``path``.

    ``{}`` if the file doesn't exist — About-scraping is optional and only
    covers whatever's been scraped so far, so a missing file just means no
    extra context is available, not an error. A row with unparseable/missing
    JSON, or no ``raw_text``, is skipped rather than failing the whole load.
    ``raw_text`` is truncated to ``max_chars`` to keep assessment prompts
    bounded.
    """
    path = Path(path)
    if not path.exists():
        logger.info("No about-context file at %s — proceeding without it", path)
        return {}

    try:
        df = pd.read_csv(path, dtype=str)
    except (pd.errors.ParserError, UnicodeDecodeError, pd.errors.EmptyDataError) as e:
        logger.warning("Could not read %s (%s) — proceeding without about-context", path, e)
        return {}
    if "url" not in df.columns or "about" not in df.columns:
        logger.warning("%s missing 'url'/'about' columns — proceeding without about-context", path)
        return {}

    context: dict[str, str] = {}
    n_skipped = 0
    for _, row in df.iterrows():
        url = row.get("url")
        about_raw = row.get("about")
        if not isinstance(url, str) or not url or not isinstance(about_raw, str) or not about_raw.strip():
            continue
        try:
            about = json.loads(about_raw)
        except json.JSONDecodeError:
            n_skipped += 1
            continue
        raw_text = about.get("raw_text") if isinstance(about, dict) else None
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        context[url] = _clean_text(raw_text.strip())[:max_chars]

    if n_skipped:
        logger.warning("Skipped %d row(s) with unparseable 'about' JSON in %s", n_skipped, path)
    logger.info("Loaded about-context for %d group(s) from %s", len(context), path)
    return context


def load_global_about_context(root: Path, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, str]:
    """Return {url: about_raw_text} unioned across every ``*_about.csv``
    found anywhere under ``root`` — both the flat constituency files and the
    ``wards/`` subfolder (``root.rglob`` covers both without needing to know
    which is which).

    When the same URL appears in more than one file (the same real group was
    independently About-scraped as a candidate for two different areas), the
    newest-mtime file wins — same tie-break ``scrape_group_about.py``'s own
    ``load_cross_target_cache`` already uses for exactly this situation, so
    a fresher re-scrape of a group is preferred over a stale one.

    Meant to be called once per pipeline run (not once per area) — it's a
    shared, run-wide resource now, not something worth re-globbing and
    re-parsing on every iteration.
    """
    root = Path(root)
    paths = sorted(root.rglob("*_about.csv"), key=lambda p: p.stat().st_mtime)

    context: dict[str, str] = {}
    for path in paths:
        context.update(load_about_context(path, max_chars=max_chars))

    logger.info(
        "Loaded global about-context: %d group(s) across %d file(s) under %s",
        len(context), len(paths), root,
    )
    return context
