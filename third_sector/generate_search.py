#!/usr/bin/env python3
"""Generate a libby search-targets CSV for a topic (no geography involved).

An LLM (prompt: third_sector/prompts.json -> keyword_expansion) turns the topic
into Facebook group search terms; one row per term, in the schema script.py
needs (search_string, scroll, processed, groups).

    python -m third_sector.generate_search --topic uk_environmental_groups
    python -m third_sector.generate_search --topic uk_environmental_groups --terms "climate action uk;litter picking"
"""

import argparse
import logging
import sys

import pandas as pd

from libby_core import ai
from third_sector import config, generations
from third_sector.settings import SEARCH_TARGETS_DIR

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "google/gemini-3-flash-preview"


def _generate_terms(slug: str, topic: dict, model: str, source: str) -> list[str]:
    """One LLM generation of search terms, logged with its exact prompt (see generations.py)."""
    prompt = config.prompt_for(topic, "keyword_expansion").format(
        topic=topic["name"], topic_description=topic["description"]
    )
    reply = ai.get_llm_text_response(prompt, model=model)
    terms = [t for t in (line.strip(" -*•\t") for line in reply.splitlines()) if t]
    generations.record(slug, "keyword_expansion", prompt, model, terms, source)
    return terms


def baseline(slug: str, n: int, model: str = DEFAULT_MODEL):
    """Generate n times with the CURRENT prompt, logging each, without touching the
    search-targets file — the runs only exist to measure the model's own noise."""
    topic = config.get_topic(slug)
    for i in range(n):
        terms = _generate_terms(slug, topic, model, "baseline")
        logger.info("baseline %d/%d: %d terms", i + 1, n, len(terms))
    entries = generations.load(slug)
    for h, b in generations.baselines(entries).items():
        logger.info("prompt %s (%s): %d runs, mean %.1f%% / max %.1f%% changed", b["prompt_hash"], b["model"], b["runs"], b["mean_pct"], b["max_pct"])


def consensus_terms(runs: list[list[str]], min_agree: int) -> list[str]:
    """Terms (first-seen spelling) produced by >= min_agree runs, most-agreed first."""
    count, label = {}, {}
    for r in runs:
        for k in {generations.norm(t) for t in r}:
            count[k] = count.get(k, 0) + 1
        for t in r:
            label.setdefault(generations.norm(t), t)
    return [label[k] for k in sorted(count, key=lambda k: -count[k]) if count[k] >= min_agree]


def run(slug: str, terms: list[str] | None = None, scroll: int = 3, force: bool = False, model: str = DEFAULT_MODEL,
        consensus: int = 0, min_agree: int = 0):
    out = SEARCH_TARGETS_DIR / f"{slug}_search_targets.csv"
    if out.exists() and not force:
        logger.error("%s already exists (it may hold scraped data). Use --force to overwrite.", out)
        sys.exit(1)

    topic = config.get_topic(slug)
    if not terms and consensus:
        min_agree = min_agree or consensus // 2 + 1
        runs = [_generate_terms(slug, topic, model, "consensus") for _ in range(consensus)]
        terms = consensus_terms(runs, min_agree)
        logger.info("consensus: kept %d terms found in >=%d of %d runs", len(terms), min_agree, consensus)
    elif not terms:
        terms = _generate_terms(slug, topic, model, "llm")
    topic_name = topic["name"]
    save_terms(slug, [topic_name, *terms], scroll)


def save_terms(slug: str, terms: list[str], scroll: int = 3) -> int:
    """Write the search-targets CSV from a list of terms (dedupes case-insensitively,
    keeps order). A term already in the file keeps its scroll count. Used by
    run() and by the control centre's term editor. Returns the number written."""
    out = SEARCH_TARGETS_DIR / f"{slug}_search_targets.csv"
    old_scroll = {}
    if out.exists():
        old = pd.read_csv(out)
        old_scroll = dict(zip(old["search_string"].str.lower(), old["scroll"]))
    seen, unique = set(), []
    for t in terms:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    pd.DataFrame({
        "topic": config.get_topic(slug)["name"],
        "search_string": unique,
        "scroll": [old_scroll.get(t.lower(), scroll) for t in unique],
        "processed": False,
        "groups": "",
    }).to_csv(out, index=False)
    logger.info("Wrote %d search terms → %s", len(unique), out)
    return len(unique)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--topic", required=True, help="Topic slug (see prompts.json)")
    p.add_argument("--terms", help="';'-separated search terms, skipping the LLM")
    p.add_argument("--scroll", type=int, default=3, help="Page-down scroll count per search (default 3)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--baseline", type=int, metavar="N", help="Generate N times with the current prompt, log them, and write no file")
    p.add_argument("--consensus", type=int, metavar="N", help="Generate N times and keep only terms most runs agree on")
    p.add_argument("--min-agree", type=int, default=0, help="With --consensus: keep terms in at least this many runs (default: majority)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.baseline:
        return baseline(a.topic, a.baseline, a.model)
    run(a.topic, [t.strip() for t in a.terms.split(";")] if a.terms else None, a.scroll, a.force, a.model, a.consensus, a.min_agree)


if __name__ == "__main__":
    main()
