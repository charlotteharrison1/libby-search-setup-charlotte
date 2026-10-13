"""Editable overrides for the shared pipeline prompts.

The *original* prompt of each is the DEFAULT_PROMPT_TEMPLATE constant in
assessment.py / descriptions.py — never modified. The control centre saves
edits to prompt_overrides.json ({prompt id: text}); delete an entry (the UI's
"Revert") and the original applies again. Read fresh on every call, so an edit
affects the very next run.
"""

import json
import string
from pathlib import Path

OVERRIDES_PATH = Path(__file__).resolve().parent / "prompt_overrides.json"


def _load() -> dict:
    return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")) if OVERRIDES_PATH.exists() else {}


def get(prompt_id: str, default: str) -> str:
    return _load().get(prompt_id) or default


def set_override(prompt_id: str, text: str | None) -> None:
    data = _load()
    if text:
        data[prompt_id] = text
    else:
        data.pop(prompt_id, None)
    OVERRIDES_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check_placeholders(name: str, text: str, required: set[str], allowed: set[str]) -> list[str]:
    """Errors if `text` can't be .format()ed safely: bad braces, missing or unknown {fields}."""
    try:
        used = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    except ValueError as e:
        return [f"{name}: bad braces ({e})"]
    errors = []
    if required - used:
        errors.append(f"{name}: missing placeholder(s) {sorted('{' + v + '}' for v in required - used)}")
    if used - allowed:
        errors.append(f"{name}: unknown placeholder(s) {sorted('{' + v + '}' for v in used - allowed)}")
    return errors
