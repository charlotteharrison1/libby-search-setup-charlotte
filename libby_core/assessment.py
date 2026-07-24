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

DEFAULT_PROMPT_TEMPLATE = (
    "You are assessing whether a Facebook group is genuinely local to a specific area. "
    "The area is: <area>{area_description}</area> ({area_kind}). "
    "The Facebook group name is: '{group_name}'. "
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
) -> pd.DataFrame:
    """Assess each row's Facebook group against ``area_description``.

    Returns the input DataFrame with an added ``response_column`` containing
    'Yes', 'Unsure', or 'No'.
    """

    def get_prompt(row: pd.Series) -> str:
        group_name = row.get(name_column)
        if pd.isna(group_name) or str(group_name).strip() == "":
            group_name = row.name
        return prompt_template.format(
            group_name=group_name,
            area_kind=area_kind,
            area_description=area_description,
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
