import pytest

from third_sector import generations


@pytest.fixture(autouse=True)
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(generations, "GENERATIONS_DIR", tmp_path)


def _rec(terms, prompt="p"):
    return generations.record("t", "keyword_expansion", prompt, "m", terms)


def test_record_and_load_roundtrip():
    e = _rec(["a", "b"])
    assert generations.load("t") == [e]
    assert e["prompt_hash"] == generations.record("t", "keyword_expansion", "p", "m", [])["prompt_hash"]


def test_compare_normalises_and_counts():
    a = _rec(["Litter Picking ", "wildlife", "climate"])
    b = _rec(["litter  picking", "wildlife", "rewilding"])
    r = generations.compare(a, b)
    assert r["added"] == ["rewilding"] and r["removed"] == ["climate"]
    assert len(r["kept"]) == 2
    assert r["pct_changed"] == 50.0          # 2 changed of 4 distinct
    assert r["same_prompt"] and r["prompt_diff"] == []


def test_compare_shows_prompt_diff():
    r = generations.compare(_rec(["a"], "line1\nold"), _rec(["a"], "line1\nnew"))
    assert not r["same_prompt"]
    assert any(l == "-old" for l in r["prompt_diff"]) and any(l == "+new" for l in r["prompt_diff"])


def test_baselines_only_for_repeated_prompts():
    _rec(["a", "b"], "same"); _rec(["a", "c"], "same"); _rec(["x"], "other")
    out = generations.baselines(generations.load("t"))
    assert list(out.values()) == [{"runs": 2, "mean_pct": 66.7, "max_pct": 66.7}]
