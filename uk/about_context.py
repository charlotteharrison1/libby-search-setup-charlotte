"""Load a group's own Facebook About-page text, for use as extra context in
the AI relevance assessment (see ``libby_core/assessment.py``'s
``context_column``).

Reads an about_pages CSV — the shape ``pull_about.sh`` pulls down and
``libby_download``'s ``extract_about.py`` originally wrote: a ``url`` column
and an ``about`` column holding JSON with a ``raw_text`` key (plus
``sections``/``section_headings_found``/``member_count``, unused here).

Shared by both ``uk/pipeline.py`` (constituencies) and
``uk/pipeline_ward.py`` (wards) — this module doesn't need to know which,
since each pipeline resolves its own path (see ``uk/settings.py``'s
``ABOUT_PAGES_DIR`` / ``WARD_ABOUT_PAGES_DIR``) and passes it in.
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
