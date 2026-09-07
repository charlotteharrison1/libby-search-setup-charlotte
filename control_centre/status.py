"""Build the control centre's status table by scanning existing local files
— no new tracking mechanism, just reading the same manifests/folders the
CLI scripts already write (push_manifest, uk/data/, Clacton-etc/).

One row per area (constituency or ward) that has *some* local evidence
already — not all 650 constituencies, just the ones you've actually
touched, keyed by slug (uk.generate_search.slugify — the same single
source of truth every push/pull script already uses) so a search_targets
file (named by slug) and a groups_<Display Name>.csv (named by display
name) can be recognized as the same area.
"""

import os
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uk.generate_search import slugify
from uk.settings import (
    ABOUT_PAGES_DIR,
    OUTPUT_DIR,
    REFERENCE_DIR,
    SCRAPED_DIR,
    SEARCH_TARGETS_DIR,
    WARD_ABOUT_PAGES_DIR,
    WARD_OUTPUT_DIR,
    WARD_SCRAPED_DIR,
    WARD_SEARCH_TARGETS_DIR,
)

CLACTON_INPUTS_DIR = Path(os.environ.get("CLACTON_INPUTS_DIR", "/Users/charlotte/vs_code/Clacton-etc/inputs"))
CLACTON_GROUPS_DIR = CLACTON_INPUTS_DIR.parent / "groups"

PUSH_MANIFEST = SEARCH_TARGETS_DIR / ".push_manifest"


def _load_pcon_names() -> dict[str, str]:
    """slug -> real PCON24NM, for every one of the ~650 constituencies —
    the one place we have a complete, authoritative name list."""
    path = REFERENCE_DIR / "constituencies_2024.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return {slugify(name): name for name in df["PCON24NM"].dropna().unique()}


def _load_pushed_slugs() -> set[str]:
    if not PUSH_MANIFEST.exists():
        return set()
    slugs = set()
    with open(PUSH_MANIFEST) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if parts and parts[0]:
                slugs.add(parts[0])
    return slugs


def _strip_suffix(stem: str, *suffixes: str) -> str:
    for suf in suffixes:
        if stem.endswith(suf):
            return stem[: -len(suf)]
    return stem


def build_status_table() -> list[dict]:
    pcon_names = _load_pcon_names()
    pushed_slugs = _load_pushed_slugs()

    # slug -> row. "type" constituency/ward; "name" the best display name
    # found so far (prefer a real PCON24NM or a name pulled from an actual
    # output file over a prettified slug guess).
    rows: dict[str, dict] = {}

    def get(slug: str, type_: str) -> dict:
        row = rows.setdefault(slug, {
            "slug": slug, "name": slug.replace("_", " ").title(), "type": type_,
            "generated": False, "pushed": False, "scraped": False,
            "about": False, "processed": False, "staged": False, "promoted": False,
        })
        if type_ == "constituency":
            row["type"] = "constituency"
        return row

    def prefer_name(row: dict, name: str) -> None:
        # A real PCON24NM, or any name pulled from an actual file (not a
        # prettified-slug guess), is always better than what's there.
        row["name"] = name

    # constituencies: generated
    for f in SEARCH_TARGETS_DIR.glob("*_search_targets.csv"):
        slug = _strip_suffix(f.stem, "_search_targets")
        row = get(slug, "constituency")
        row["generated"] = True
        if slug in pcon_names:
            prefer_name(row, pcon_names[slug])
        row["pushed"] = slug in pushed_slugs

    # wards: generated
    for f in WARD_SEARCH_TARGETS_DIR.glob("*_search_targets.csv"):
        slug = _strip_suffix(f.stem, "_search_targets")
        row = get(slug, "ward")
        row["generated"] = True
        row["pushed"] = slug in pushed_slugs

    # constituencies: scraped (pulled back)
    for f in SCRAPED_DIR.glob("*_search_targets.csv"):
        if f.name == "master_constituency_place_data_file.csv":
            continue
        slug = _strip_suffix(f.stem, "_search_targets")
        row = get(slug, "constituency")
        row["scraped"] = True
        if slug in pcon_names:
            prefer_name(row, pcon_names[slug])

    # wards: scraped
    for f in WARD_SCRAPED_DIR.glob("*_search_targets.csv"):
        slug = _strip_suffix(f.stem, "_search_targets")
        row = get(slug, "ward")
        row["scraped"] = True

    # constituencies: about-scraped (any file for this slug — real pull or local --about)
    for f in ABOUT_PAGES_DIR.glob("*_about*.csv"):
        if not f.is_file():
            continue
        slug = _strip_suffix(f.stem, "_search_targets_about", "_about_local", "_about")
        row = get(slug, "constituency")
        row["about"] = True
        if slug in pcon_names:
            prefer_name(row, pcon_names[slug])

    # wards: about-scraped
    if WARD_ABOUT_PAGES_DIR.exists():
        for f in WARD_ABOUT_PAGES_DIR.glob("*_about*.csv"):
            slug = _strip_suffix(f.stem, "_search_targets_about", "_about_local", "_about")
            row = get(slug, "ward")
            row["about"] = True

    # constituencies: processed output (groups_<Display Name>.csv — real name, not slug)
    for f in OUTPUT_DIR.glob("groups_*.csv"):
        name = f.stem[len("groups_"):]
        slug = slugify(name)
        row = get(slug, "constituency" if slug in pcon_names else "ward")
        row["processed"] = True
        prefer_name(row, name)

    # wards: processed output
    if WARD_OUTPUT_DIR.exists():
        for f in WARD_OUTPUT_DIR.glob("groups_*.csv"):
            name = f.stem[len("groups_"):]
            slug = slugify(name)
            row = get(slug, "ward")
            row["processed"] = True
            prefer_name(row, name)

    # staged to Clacton-etc/inputs/ (constituency, flat)
    if CLACTON_INPUTS_DIR.is_dir():
        for f in CLACTON_INPUTS_DIR.glob("groups_*.csv"):
            name = f.stem[len("groups_"):]
            slug = slugify(name)
            row = get(slug, "constituency" if slug in pcon_names else "ward")
            row["staged"] = True
            prefer_name(row, name)
        # staged to Clacton-etc/inputs/wards/
        wards_inputs = CLACTON_INPUTS_DIR / "wards"
        if wards_inputs.is_dir():
            for f in wards_inputs.glob("groups_*.csv"):
                name = f.stem[len("groups_"):]
                slug = slugify(name)
                row = get(slug, "ward")
                row["staged"] = True
                prefer_name(row, name)

    # promoted to Clacton-etc/groups/ (billable-eligible; mixes constituency + ward flatly)
    if CLACTON_GROUPS_DIR.is_dir():
        for f in CLACTON_GROUPS_DIR.glob("groups_*.csv"):
            name = f.stem[len("groups_"):]
            slug = slugify(name)
            row = get(slug, "constituency" if slug in pcon_names else "ward")
            row["promoted"] = True
            prefer_name(row, name)

    return sorted(rows.values(), key=lambda r: (r["type"], r["name"]))
