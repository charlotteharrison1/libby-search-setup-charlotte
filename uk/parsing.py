import re

import pandas as pd

K_MULTIPLIER = 1_000

_UNIT_TO_MONTH = {
    "day": 30.0,
    "month": 1.0,
    "year": 1.0 / 12.0,
}


def parse_count(value_text: str) -> int | None:
    if not value_text:
        return None
    value_text = value_text.strip().upper().replace(",", "")
    try:
        if value_text.endswith("K"):
            return int(float(value_text[:-1]) * K_MULTIPLIER)
        return int(float(value_text))
    except ValueError:
        return None


def parse_posts_per_month(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)(\+)?\s+posts?\s+a\s+(day|month|year)", text)
    if not match:
        return None
    count = float(match.group(1))
    unit = match.group(3)
    return count * _UNIT_TO_MONTH[unit]


def parse_details(detail: str) -> pd.Series:
    if pd.isna(detail):
        return pd.Series({"public_y_n": None, "members": None, "posts_a_month": None})
    detail = str(detail).strip()
    if detail.startswith("Unread"):
        return pd.Series({"public_y_n": None, "members": None, "posts_a_month": None})

    is_public = None
    if detail.startswith("Public"):
        is_public = True
    elif detail.startswith("Private"):
        is_public = False

    members_match = re.search(r"\b([\d.,]+\s*K?)\s+members\b", detail, re.IGNORECASE)
    members = parse_count(members_match.group(1)) if members_match else None

    posts_a_month = parse_posts_per_month(detail)

    return pd.Series({
        "public_y_n": is_public,
        "members": members,
        "posts_a_month": posts_a_month,
    })
