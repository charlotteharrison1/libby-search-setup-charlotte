"""Load/validate/save third_sector/prompts.json (topics + editable prompts).
Re-read from disk on every call so edits in the control centre apply to the
very next run — no caching."""

import json

from libby_core.prompts import check_placeholders
from uk.generate_search import slugify
from third_sector.settings import PROMPTS_PATH

_TOPIC_VARS = {"topic", "topic_description"}
# prompt name -> (required placeholders, allowed placeholders)
PROMPT_VARS = {
    "keyword_expansion": (_TOPIC_VARS, _TOPIC_VARS),
    "stage1_filter": (_TOPIC_VARS | {"group_name"}, _TOPIC_VARS | {"group_name", "about_context"}),
    "stage2_with_posts": (_TOPIC_VARS | {"group_name", "posts"}, _TOPIC_VARS | {"group_name", "about_context", "posts"}),
}


def load() -> dict:
    return json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))


def validate(cfg: dict) -> list[str]:
    errors = []
    topics = cfg.get("topics")
    if not isinstance(topics, list) or not topics:
        errors.append("need at least one topic")
        topics = []
    slugs = [slugify(t.get("name", "")) for t in topics]
    if any(not s for s in slugs) or len(set(slugs)) != len(slugs):
        errors.append("every topic needs a unique, non-empty name")
    def check(label, texts, must_exist):
        for name, (required, allowed) in PROMPT_VARS.items():
            text = texts.get(name)
            if text is None and not must_exist:
                continue
            if not isinstance(text, str) or not text.strip():
                errors.append(f"{label}{name}: missing")
                continue
            errors.extend(check_placeholders(f"{label}{name}", text, required, allowed))

    check("default ", cfg.get("prompts", {}), True)
    for t in topics:
        check(f"[{t.get('name')}] ", t.get("prompts", {}), False)
    return errors


def save(cfg: dict) -> None:
    if PROMPTS_PATH.exists():
        PROMPTS_PATH.with_suffix(".json.bak").write_text(PROMPTS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    PROMPTS_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_topic(slug: str) -> dict:
    for t in load()["topics"]:
        if slugify(t["name"]) == slug:
            return t
    raise KeyError(f"Unknown topic slug: {slug}")


def prompt_for(topic: dict, name: str) -> str:
    """The topic's own prompt for this stage if it has one, else the shared default."""
    return topic.get("prompts", {}).get(name) or load()["prompts"][name]
