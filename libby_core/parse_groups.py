"""Parse Facebook group fields from Libby CSV exports."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from typing import Any

import pandas as pd

_NUMBER_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)(?P<suffix>[KkMm])?\+?")

def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()

    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False

    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _dicts_only(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _clean_string(value: str) -> str:
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _clean_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _clean_string(value) if isinstance(value, str) else value
        for key, value in group.items()
    }


def _parse_compact_number(value: str) -> int | None:
    match = _NUMBER_RE.search(value)
    if not match:
        return None

    number = float(match.group("number"))
    suffix = (match.group("suffix") or "").lower()
    multiplier = {"k": 1_000, "m": 1_000_000}.get(suffix, 1)
    return int(number * multiplier)


def _parse_details(details: object) -> dict[str, Any]:
    if not isinstance(details, str) or not details.strip():
        return {
            "privacy": None,
            "member_count": None,
            "posts_per_day": None,
        }

    parts = [part.strip() for part in details.split("·")]
    privacy = parts[0] if parts else None
    member_count = None
    posts_per_day = None

    for part in parts[1:]:
        if "member" in part:
            member_count = _parse_compact_number(part)
        elif "post" in part and "day" in part:
            posts_per_day = _parse_compact_number(part)

    return {
        "privacy": privacy,
        "member_count": member_count,
        "posts_per_day": posts_per_day,
    }


def _enrich_group(group: dict[str, Any]) -> dict[str, Any]:
    group = _clean_group(group)
    return group | _parse_details(group.get("details"))


def parse_groups(value: object) -> list[dict[str, Any]]:
    """Parse a CSV ``groups`` value into a list of group dictionaries.

    The scraped CSV stores groups as a string representation of a Python/JSON
    list. Empty, missing, malformed, and non-list values return an empty list.
    Group details are enriched with privacy, member count, and daily post count
    fields when those values are present.
    """
    if _is_missing(value):
        return []

    if isinstance(value, list):
        return [_enrich_group(group) for group in _dicts_only(value)]

    if not isinstance(value, str):
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return []

    return [_enrich_group(group) for group in _dicts_only(parsed)]


def count_groups(value: object) -> int:
    """Return the number of parsed group dictionaries in ``value``."""
    return len(parse_groups(value))


def explode_groups(
    df: pd.DataFrame,
    groups_column: str = "groups",
    parsed_column: str = "groups_list",
) -> pd.DataFrame:
    """Return one row per parsed group, expanding group dict keys into columns."""
    if groups_column not in df.columns:
        raise KeyError(f"'{groups_column}' not found in DataFrame columns")

    exploded = df.copy()
    exploded[parsed_column] = exploded[groups_column].apply(parse_groups)
    exploded = exploded.explode(parsed_column).reset_index(drop=True)
    exploded = exploded[exploded[parsed_column].notna()].reset_index(drop=True)

    if exploded.empty:
        return exploded

    group_columns = pd.DataFrame(
        list(exploded[parsed_column]),
        index=exploded.index,
        dtype=object,
    )
    return pd.concat([exploded, group_columns], axis=1)
