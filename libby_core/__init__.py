"""libby_core — shared building blocks for the UK and US Libby search pipelines.

Modules
-------
parse_groups : parse Facebook group fields from a scraped CSV ``groups`` column.
ai           : OpenRouter/LLM client and concurrent row-iteration helper.
descriptions : generate + cache AI geographic descriptions for an area.
assessment   : LLM relevance check of each group against an area description.
settings     : shared config (OpenRouter API key from the root .env).
"""

import pandas as pd

# Scraped Facebook group names contain unpaired Unicode surrogates. pandas 3.0
# defaults ``future.infer_string`` to True, which converts object string columns
# to a PyArrow large_string backend that cannot encode surrogates (raising
# UnicodeEncodeError). The pipelines were written for pandas 2.x object strings,
# so restore that behaviour globally for any process that imports libby_core.
pd.set_option("future.infer_string", False)
