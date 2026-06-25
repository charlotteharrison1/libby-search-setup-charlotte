"""Minimal helpers for calling an LLM, one of them across the rows of a DataFrame.

Two functions:

- ``get_llm_text_response`` sends a prompt and returns the reply text.
- ``iterate_df_rows`` builds a prompt per row and calls the LLM concurrently,
  writing each reply back into the DataFrame.

Both talk to OpenRouter via the OpenAI client. OpenRouter is OpenAI-API
compatible, so the only difference from plain OpenAI is the ``base_url`` and the
``"vendor/model"`` style model names (e.g. ``"openai/gpt-4o-mini"``).
"""

import concurrent.futures
import logging

import openai
import pandas as pd

from libby_core.settings import OPEN_ROUTER_KEY

logger = logging.getLogger(__name__)

# Quieten the per-request HTTP logging the OpenAI client emits via httpx.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

client = openai.OpenAI(
    api_key=OPEN_ROUTER_KEY,
    base_url="https://openrouter.ai/api/v1",
)


def get_llm_text_response(prompt, model="openai/gpt-4o-mini", max_tokens=1000):
    """Send a single prompt to the model and return the reply text."""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


def iterate_df_rows(
    df,
    get_prompt,
    response_column="gpt_text",
    model="openai/gpt-4o-mini",
    concurrency=10,
    max_tokens=500,
):
    """Call the LLM once per row and store the reply in ``response_column``.

    ``get_prompt(row)`` turns a row into a prompt string. Rows are processed
    concurrently. Returns a copy of ``df`` with the replies added; rows that
    error out are left blank.
    """
    df = df.copy()

    def process_row(index, row):
        try:
            prompt = get_prompt(row)
            return index, get_llm_text_response(prompt, model=model, max_tokens=max_tokens)
        except Exception as e:
            logger.error("Error processing row %s: %s", index, e)
            return index, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(process_row, i, row) for i, row in df.iterrows()]
        for future in concurrent.futures.as_completed(futures):
            index, text = future.result()
            if text is not None:
                df.loc[index, response_column] = text

    return df
