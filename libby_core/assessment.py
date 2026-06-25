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
    "You are a geopolitical analyst. You are to look at this Facebook group "
    "name='{group_name}' and assess if this group is likely used by people "
    "who live in or have an interest in the following {area_kind}: "
    "<area>{area_description}</area>. "
    "Examples might include community groups, items for sale, events, etc. "
    "If the group is linked to a large city neighbouring but not overlapping the area, mark no. "
    "Answer with one of the following three responses 'Yes|Unsure|No'. "
    "You are to mark unsure if there is any US spelling, or if the group is linked to a large city "
    "from the US/Canada/Australia/New Zealand. "
    "Only answer with the single word response."
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
