"""Generate and cache AI geographic descriptions for an area.

Generic over the *kind* of area (UK parliament constituency, US congressional
district, …). Each pipeline supplies the area kind and prompt wording; the
descriptions are cached to a CSV keyed by an id column so they are generated
once and reused across runs.

The cache CSV has three columns whose names are configurable via ``id_col`` and
``name_col`` (the third is always ``description``). This lets the UK pipeline
keep reusing its existing ``constituency_descriptions.csv`` (PCON24CD/PCON24NM)
without regenerating ~600 descriptions.
"""

import logging
from pathlib import Path

import pandas as pd

from libby_core import ai

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "google/gemini-3-pro-preview:online"
DEFAULT_MAX_TOKENS = 20_000

DEFAULT_PROMPT_TEMPLATE = (
    "Give me a brief geographical summary of {area_kind} {area_name}. "
    "Just the names of the cities, wards, towns and villages, satellite towns "
    "and other areas in or overlapping the area. "
    "Avoid cities that are outside the area. "
    "Check the current boundaries for the area. "
    "Include other areas and important landmarks, schools and other notable places."
)


def build_prompt(
    area_name: str,
    area_kind: str,
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
) -> str:
    return prompt_template.format(area_kind=area_kind, area_name=area_name)


def generate_description(
    area_name: str,
    area_kind: str,
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str | None:
    """Call the LLM to produce a geographic description for one area."""
    prompt = build_prompt(area_name, area_kind, prompt_template)
    return ai.get_llm_text_response(prompt, model=model, max_tokens=max_tokens)


def load_descriptions(
    path: Path,
    id_col: str = "area_id",
    name_col: str = "area_name",
) -> pd.DataFrame:
    """Load the cached descriptions CSV. Returns an empty frame if missing."""
    if not Path(path).exists():
        return pd.DataFrame(columns=[id_col, name_col, "description"])
    return pd.read_csv(path, dtype=str).fillna("")


def ensure_description(
    area_id: str,
    area_name: str,
    path: Path,
    area_kind: str,
    id_col: str = "area_id",
    name_col: str = "area_name",
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Return the cached description for ``area_id``, generating it if absent.

    Loads the cache CSV, returns any existing non-empty description immediately,
    otherwise generates one via the LLM, appends it to the cache, saves, and
    returns it.
    """
    existing = load_descriptions(path, id_col=id_col, name_col=name_col)

    if not existing.empty and id_col in existing.columns:
        match = existing[existing[id_col] == str(area_id)]
        if not match.empty:
            desc = match.iloc[0]["description"]
            if isinstance(desc, str) and desc.strip():
                return desc

    logger.info("  Generating description for %s (%s)", area_name, area_id)
    desc = generate_description(
        area_name,
        area_kind=area_kind,
        prompt_template=prompt_template,
        model=model,
        max_tokens=max_tokens,
    ) or ""

    new_row = pd.DataFrame([{id_col: str(area_id), name_col: str(area_name), "description": desc}])
    existing = pd.concat([existing, new_row], ignore_index=True)
    # Keep the longest (most complete) description per area id.
    existing = (
        existing.sort_values("description", ascending=False, key=lambda s: s.str.len())
        .drop_duplicates(subset=[id_col], keep="first")
        .reset_index(drop=True)
    )
    existing.to_csv(path, index=False)
    logger.info("  Saved description for %s → %s", area_name, path)
    return desc
