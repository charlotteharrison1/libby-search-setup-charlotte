"""Append-only log of LLM generations (currently: search terms) with the exact
prompt used, plus comparison between any two generations.

One JSONL file per topic: third_sector/data/generations/<slug>.jsonl. Each line:
{id, stage, source ("llm" | "baseline"), model, prompt, prompt_hash, outputs: [...]}.
Runs sharing a prompt_hash form the noise baseline: how much the output moves
when nothing about the prompt changed.

    python -m third_sector.generations --topic uk_environmental_groups            # last two
    python -m third_sector.generations --topic uk_environmental_groups --a ID --b ID
"""

import argparse
import difflib
import hashlib
import itertools
import json
from datetime import datetime

from third_sector.settings import DATA_DIR

GENERATIONS_DIR = DATA_DIR / "generations"


def _path(slug: str):
    GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    return GENERATIONS_DIR / f"{slug}.jsonl"


def norm(term: str) -> str:
    return " ".join(term.lower().split())


def record(slug: str, stage: str, prompt: str, model: str, outputs: list[str], source: str = "llm") -> dict:
    entry = {
        "id": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "stage": stage, "source": source, "model": model,
        "prompt": prompt, "prompt_hash": hashlib.sha1(prompt.encode()).hexdigest()[:8],
        "outputs": outputs,
    }
    with open(_path(slug), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load(slug: str) -> list[dict]:
    p = _path(slug)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def compare(a: dict, b: dict) -> dict:
    """Diff two generations. pct_changed = (added + removed) / all distinct terms."""
    sa = {norm(t): t for t in a["outputs"]}
    sb = {norm(t): t for t in b["outputs"]}
    added = [sb[k] for k in sb if k not in sa]
    removed = [sa[k] for k in sa if k not in sb]
    kept = [sb[k] for k in sb if k in sa]
    union = len(set(sa) | set(sb))
    return {
        "a": a["id"], "b": b["id"],
        "same_prompt": a["prompt_hash"] == b["prompt_hash"],
        "added": added, "removed": removed, "kept": kept,
        "pct_changed": round(100 * (len(added) + len(removed)) / union, 1) if union else 0.0,
        "prompt_diff": [] if a["prompt"] == b["prompt"] else list(difflib.unified_diff(
            a["prompt"].splitlines(), b["prompt"].splitlines(), "A", "B", lineterm="", n=1)),
    }


def baselines(entries: list[dict]) -> dict[str, dict]:
    """Per prompt_hash with >=2 runs: pairwise % changed between same-prompt runs."""
    by_hash: dict[str, list[dict]] = {}
    for e in entries:
        by_hash.setdefault(e["prompt_hash"], []).append(e)
    out = {}
    for h, runs in by_hash.items():
        if len(runs) < 2:
            continue
        pcts = [compare(x, y)["pct_changed"] for x, y in itertools.combinations(runs, 2)]
        out[h] = {"runs": len(runs), "mean_pct": round(sum(pcts) / len(pcts), 1), "max_pct": max(pcts)}
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--topic", required=True)
    p.add_argument("--a")
    p.add_argument("--b")
    args = p.parse_args()
    entries = load(args.topic)
    byid = {e["id"]: e for e in entries}
    if args.a and args.b:
        a, b = byid[args.a], byid[args.b]
    elif len(entries) >= 2:
        a, b = entries[-2], entries[-1]
    else:
        raise SystemExit("Need at least two generations to compare.")
    r = compare(a, b)
    print(f"{r['a']} → {r['b']}  ({'same prompt' if r['same_prompt'] else 'prompt changed'}): {r['pct_changed']}% changed")
    print("added:  ", r["added"]); print("removed:", r["removed"]); print("kept:   ", len(r["kept"]))
    print("\n".join(r["prompt_diff"]))
    for h, s in baselines(entries).items():
        print(f"baseline {h}: {s['runs']} runs, mean {s['mean_pct']}% / max {s['max_pct']}% changed")


if __name__ == "__main__":
    main()
