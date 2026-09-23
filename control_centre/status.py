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

from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uk.generate_search import slugify
from uk.settings import (
    ABOUT_PAGES_DIR,
    CLACTON_GROUPS_DIR,
    CLACTON_INPUTS_DIR,
    CUSTOM_OUTPUT_DIR,
    OUTPUT_DIR,
    REFERENCE_DIR,
    SCRAPED_DIR,
    SEARCH_TARGETS_DIR,
    WARD_ABOUT_PAGES_DIR,
    WARD_OUTPUT_DIR,
    WARD_SCRAPED_DIR,
    WARD_SEARCH_TARGETS_DIR,
)

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


def _load_custom_area_names() -> set[str]:
    """Every custom region with a cached membership resolution (see
    uk/region_membership.py) — read directly as a CSV, not via that module,
    so this stays a pure file scan with no OpenRouter/LLM client dependency
    just to load the dashboard."""
    from uk.settings import CUSTOM_REGION_MEMBERSHIP_PATH
    if not CUSTOM_REGION_MEMBERSHIP_PATH.exists():
        return set()
    df = pd.read_csv(CUSTOM_REGION_MEMBERSHIP_PATH, dtype=str)
    return set(df["area_name"].dropna().unique())


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
    # output file over a prettified slug guess). Type is fixed at first
    # creation (setdefault) and never overwritten by a later call — every
    # real constituency is seeded up front below, precisely so a
    # differently-typed file that happens to compute a colliding slug can't
    # silently relabel it.
    rows: dict[str, dict] = {}

    def get(slug: str, type_: str) -> dict:
        return rows.setdefault(slug, {
            "slug": slug, "name": slug.replace("_", " ").title(), "type": type_,
            "generated": False, "pushed": False, "scraped": False,
            "about": False, "processed": False, "staged": False, "promoted": False,
        })

    def prefer_name(row: dict, name: str) -> None:
        # A real PCON24NM, or any name pulled from an actual file (not a
        # prettified-slug guess), is always better than what's there.
        row["name"] = name

    # Seed every one of the ~650 real constituencies up front, all-false —
    # so the dropdown/table are exhaustive and searchable even for areas
    # you've never touched, not just ones with local file evidence. Wards
    # have no equivalent complete reference in this repo (only the ones
    # you've generated for), so they stay "whatever's been touched".
    for slug, name in pcon_names.items():
        row = get(slug, "constituency")
        prefer_name(row, name)

    # custom regions: seeded from uk/region_membership.py's cache — a region
    # has no scrape/generate step of its own (see uk/pipeline_custom.py), so
    # "generated" here means "membership resolved" and "scraped" means "has
    # enough local data to run the pipeline" — both true as soon as
    # membership exists, which is also what lets it through the frontend's
    # run_pipeline area picker (that filters on `.scraped`, written for the
    # constituency/ward case but equally correct here: a region isn't
    # runnable until it has a member list, same as a constituency isn't
    # runnable until it's been scraped).
    custom_area_names = _load_custom_area_names()
    custom_slugs = {slugify(name): name for name in custom_area_names}
    for slug, name in custom_slugs.items():
        row = get(slug, "custom")
        row["generated"] = True
        row["scraped"] = True
        prefer_name(row, name)

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

    # constituencies: leftover named output in uk/output/ itself (only ever
    # populated by a full multi-constituency batch run — a single-area run,
    # the control centre's normal case, writes this same file then moves it
    # straight into CLACTON_INPUTS_DIR as its last step, so this glob is
    # usually empty; "processed" itself is computed from staged/promoted
    # below, not from this — see there for why).
    for f in OUTPUT_DIR.glob("groups_*.csv"):
        name = f.stem[len("groups_"):]
        slug = slugify(name)
        row = get(slug, "constituency" if slug in pcon_names else "ward")
        prefer_name(row, name)

    # wards: same leftover-only caveat as above
    if WARD_OUTPUT_DIR.exists():
        for f in WARD_OUTPUT_DIR.glob("groups_*.csv"):
            name = f.stem[len("groups_"):]
            slug = slugify(name)
            row = get(slug, "ward")
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
        # staged to Clacton-etc/inputs/custom/
        custom_inputs = CLACTON_INPUTS_DIR / "custom"
        if custom_inputs.is_dir():
            for f in custom_inputs.glob("groups_*.csv"):
                name = f.stem[len("groups_"):]
                slug = slugify(name)
                row = get(slug, "custom")
                row["staged"] = True
                prefer_name(row, name)

    # custom regions: leftover output in uk/output/custom/ itself — same
    # "normally empty, staging already moved it" caveat as the constituency/
    # ward leftover scans above.
    if CUSTOM_OUTPUT_DIR.exists():
        for f in CUSTOM_OUTPUT_DIR.glob("groups_*.csv"):
            name = f.stem[len("groups_"):]
            slug = slugify(name)
            row = get(slug, "custom")
            prefer_name(row, name)

    # promoted to Clacton-etc/groups/ (billable-eligible; mixes constituency + ward flatly)
    if CLACTON_GROUPS_DIR.is_dir():
        for f in CLACTON_GROUPS_DIR.glob("groups_*.csv"):
            name = f.stem[len("groups_"):]
            slug = slugify(name)
            row = get(slug, "constituency" if slug in pcon_names else "ward")
            row["promoted"] = True
            prefer_name(row, name)

    # "Processed" = has a completed pipeline run landed anywhere downstream
    # of it — staged (Clacton-etc/inputs/) or promoted (Clacton-etc/groups/)
    # — rather than checking uk/output/ directly, which the pipeline always
    # empties back out on a normal single-area run (see the comment above).
    for row in rows.values():
        row["processed"] = row["staged"] or row["promoted"]

    return sorted(rows.values(), key=lambda r: (r["type"], r["name"]))
