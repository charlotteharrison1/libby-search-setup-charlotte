"""
Compares Facebook group counts (from Clacton-etc) against constituency
urban/rural classification (from LU-playground/rural_index.json).

For each constituency: count the groups in its CSV, then look up its
urbanness. One file = one constituency, named by its filename.

Read-only with respect to Clacton-etc — nothing there is ever written to.
"""
import csv
import json
import re
from pathlib import Path

CLACTON_ETC = Path("/Users/charlotte/vs_code/Clacton-etc")
RURAL_INDEX_PATH = Path(
    "/Users/charlotte/vs code/campaignlab/LU-playground/rural_index.json"
)
OUT_DIR = Path(__file__).parent
OUT_CSV = OUT_DIR / "urban_group_comparison.csv"
OUT_JSON = OUT_DIR / "urban_group_comparison.json"


def normalize_name(name: str) -> str:
    name = name.strip().lower()
    name = name.replace("&", "and")
    name = re.sub(r"[,\.]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def constituency_name_from_filename(csv_path: Path) -> str:
    name = csv_path.stem
    if name.startswith("groups_"):
        name = name[len("groups_"):]
    return name


def count_groups(csv_path: Path) -> int:
    """Unique group URLs in the file (no file here has internal duplicates,
    but dedupe defensively rather than trust a raw row count)."""
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "url" not in reader.fieldnames:
            return 0
        return len({row["url"].strip() for row in reader if row["url"].strip()})


def collect_group_counts(valid_constituency_names: set):
    """
    One CSV per constituency, keyed by filename. groups/ is scanned first and
    wins over inputs/ when the same constituency name appears in both (e.g.
    Norwich North: the groups/ version is the later, curated pass — confirmed
    by hand, the two disagree on which groups passed assessment).
    Files whose filename isn't one of the ~650 real Westminster constituencies
    are ward/locality-level breakdowns (e.g. inputs/wards/*, and
    groups_Blakenall.csv — Blakenall is a ward of Walsall and Bloxwich, not a
    constituency) and are skipped so they don't get miscounted as their own
    "constituency".
    """
    group_counts: dict[str, int] = {}
    source_of: dict[str, str] = {}

    for folder, tag in [(CLACTON_ETC / "groups", "groups"), (CLACTON_ETC / "inputs", "inputs")]:
        if not folder.is_dir():
            continue
        for csv_path in sorted(folder.glob("*.csv")):
            name = constituency_name_from_filename(csv_path)
            if normalize_name(name) not in valid_constituency_names:
                print(f"Skipping ward/locality-level file: {csv_path.relative_to(CLACTON_ETC)}")
                continue
            if name in group_counts:
                continue  # already claimed by groups/ (scanned first, higher priority)
            group_counts[name] = count_groups(csv_path)
            source_of[name] = tag

    return group_counts, source_of


def load_rural_index():
    with open(RURAL_INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)
    resource = next(r for r in data["resources"] if r["name"] == "pcon_2025_ruc")
    lookup = {}
    for row in resource["data"]:
        lookup[normalize_name(row["constituency-name"])] = row
    return lookup


def main():
    rural_lookup = load_rural_index()
    valid_constituency_names = set(rural_lookup.keys())
    group_counts, source_of = collect_group_counts(valid_constituency_names)

    rows = []
    unmatched = []
    for constituency, count in sorted(group_counts.items()):
        rural = rural_lookup.get(normalize_name(constituency))
        if rural is None:
            unmatched.append(constituency)
            continue
        rows.append(
            {
                "constituency": constituency,
                "group_count": count,
                "source": source_of[constituency],
                "urban_pct": round(rural["urban"] * 100, 2),
                "rural_pct": round(rural["rural"] * 100, 2),
                "highly_rural_pct": round(rural["highly_rural"] * 100, 2),
                "label": rural["label"],
            }
        )

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "constituency",
                "group_count",
                "source",
                "urban_pct",
                "rural_pct",
                "highly_rural_pct",
                "label",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    print(f"Matched {len(rows)} constituencies -> {OUT_CSV}")
    if unmatched:
        print(f"\n{len(unmatched)} constituency name(s) did NOT match rural_index.json:")
        for name in unmatched:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
