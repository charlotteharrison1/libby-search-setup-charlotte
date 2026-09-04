"""Assess whether each Facebook group is relevant to an area.

Generic over area kind. Takes a pre-generated area description (see
``descriptions.py``) and asks the LLM, for each group, whether it is likely
used by people who live in or care about that area. Adds a ``first_assessment``
column containing 'Yes', 'Unsure', or 'No'.
"""

import logging

import pandas as pd

from libby_core import ai

logger = logging.getLogger(__name__)

DEFAULT_ASSESSMENT_MODEL = "google/gemini-3-flash-preview"


def _clean_text(text: str) -> str:
    """Strip unpaired UTF-16 surrogates. Scraped Facebook text (group names,
    About-page text) occasionally contains them — e.g. from mangled emoji —
    which raise UnicodeEncodeError deep in the HTTP client when the prompt
    is sent, silently failing that row's assessment. Same fix
    uk/about_context.py applies to About text; applied here too so a group
    *name* with the same issue can't crash the call either."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")

DEFAULT_PROMPT_TEMPLATE = (
    "You are assessing whether a Facebook group is genuinely local to a specific area. "
    "The area is: <area>{area_description}</area> ({area_kind}). "
    "The Facebook group name is: '{group_name}'. "
    "{about_context}"
    "Answer 'Yes' only if the group is clearly local to that specific area — "
    "for example a neighbourhood group, local events, local sport, local services, or local community. "
    "Answer 'No' if the group covers a broader region, county, or country rather than the specific area "
    "(e.g. an Essex-wide group for a Clacton constituency, or a Scotland-wide group for a Midlothian constituency). "
    "Answer 'No' if the group is linked to a different area entirely, or is international. "
    "Answer 'Unsure' if the group name gives ambiguous signals about its geographic scope. "
    "When in doubt, prefer 'Unsure' over 'No' — these results undergo human review. "
    "Only answer with the single word: 'Yes', 'Unsure', or 'No'."
)


def assess_groups(
    df: pd.DataFrame,
    area_description: str,
    area_kind: str,
    name_column: str = "name",
    response_column: str = "first_assessment",
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
    model: str = DEFAULT_ASSESSMENT_MODEL,
    concurrency: int = 10,
    context_column: str | None = None,
) -> pd.DataFrame:
    """Assess each row's Facebook group against ``area_description``.

    ``context_column``, if given, names a column holding an excerpt of the
    group's own Facebook About-page text (see ``uk/about_context.py``) to
    include as extra grounding for the LLM. Rows with no text there (or when
    ``context_column`` is ``None``) get a prompt identical to not passing it
    at all — ``prompt_template``'s ``{about_context}`` placeholder renders
    as "" — so this is backward compatible with every existing caller.

    Returns the input DataFrame with an added ``response_column`` containing
    'Yes', 'Unsure', or 'No'.
    """

    def get_prompt(row: pd.Series) -> str:
        group_name = row.get(name_column)
        if pd.isna(group_name) or str(group_name).strip() == "":
            group_name = row.name
        else:
            group_name = _clean_text(str(group_name))

        about_context = ""
        if context_column:
            about_text = row.get(context_column)
            if isinstance(about_text, str) and about_text.strip():
                about_context = (
                    "Additional context — an excerpt from this group's own "
                    f"Facebook About page: \"{_clean_text(about_text.strip())}\"\n\n"
                )

        return prompt_template.format(
            group_name=group_name,
            area_kind=area_kind,
            area_description=area_description,
            about_context=about_context,
        )

    df_result = ai.iterate_df_rows(
        df.copy(),
        get_prompt=get_prompt,
        response_column=response_column,
        model=model,
        concurrency=concurrency,
    )

    if response_column not in df_result.columns:
        df_result[response_column] = None

    return df_result
