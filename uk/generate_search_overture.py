#!/usr/bin/env python3
"""Generate UK search targets using Overture Maps place data.

Alternative to generate_search.py that replaces the LLM place-name lookup with
verified Overture Maps data. For each constituency centroid it finds nearby
Overture places, ranks their localities by frequency, and takes the top N as
search targets.

How it works:
  1. Query Overture Maps (via DuckDB + S3) for all UK places that have a
     locality field. Results are cached as uk/data/uk_overture_places.parquet.
  2. For each constituency centroid (from constituencies_2024.csv), filter
     places within a radius using haversine distance.
  3. Rank localities by place count and take the top N.
  4. Write the master scrape file in the same format as generate_search.py.

Only requires constituencies_2024.csv — no GeoJSON needed.
The Overture cache is reused on subsequent runs. Pass --rebuild-cache to
re-download (e.g. after a new Overture release).

    python -m uk.generate_search_overture
    python -m uk.generate_search_overture --constituency "Finchley and Golders Green"
    python -m uk.generate_search_overture --top-n 5 --radius-km 15 --force

Output format is identical to generate_search.py so the rest of the pipeline
is unchanged.

Requires: duckdb (pip install duckdb  or  pip install -e ".[us]")
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from uk.settings import CONSTITUENCIES_PATH, NEW_SCRAPE_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

OVERTURE_RELEASE = "2026-05-20.0"
UK_BBOX = (-8.0, 49.5, 2.0, 61.0)
UK_PLACES_CACHE = Path(__file__).resolve().parent / "data" / "uk_overture_places.parquet"
DEFAULT_TOP_N = 5
DEFAULT_RADIUS_KM = 15.0


def _would_clobber_scraped_data(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        existing = pd.read_csv(path)
    except Exception:
        return False
    return "groups" in existing.columns and existing["groups"].notna().any()


def _haversine_km(lat1, lon1, lat2, lon2):
    """Vectorised haversine distance in km."""
    R = 6371.0088
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def download_uk_places(out_parquet: Path) -> pd.DataFrame:
    """Query Overture for all UK places that have a locality field.

    Returns a DataFrame with columns: locality, lon, lat.
    Cached to out_parquet — delete it to force a re-download.
    """
    try:
        import duckdb
    except ImportError:
        logger.error("duckdb is required: pip install duckdb")
        sys.exit(1)

    minx, miny, maxx, maxy = UK_BBOX
    places_path = (
        f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}"
        f"/theme=places/type=place/*"
    )

    logger.info("Querying Overture places for UK bbox — this may take several minutes…")
    con = duckdb.connect()
    con.execute(
        "INSTALL spatial; LOAD spatial; "
        "INSTALL httpfs; LOAD httpfs; "
        "SET s3_region='us-west-2';"
    )
    df = con.execute(f"""
        SELECT
            addresses[1].locality AS locality,
            ST_X(geometry) AS lon,
            ST_Y(geometry) AS lat
        FROM read_parquet('{places_path}', filename=true, hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {minx} AND {maxx}
          AND bbox.ymin BETWEEN {miny} AND {maxy}
          AND names.primary IS NOT NULL
          AND (confidence IS NULL OR confidence >= 0.70)
          AND addresses[1].locality IS NOT NULL
          AND addresses[1].locality != ''
    """).df()

    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_parquet, index=False)
    logger.info("Cached %d UK places → %s", len(df), out_parquet)
    return df


def top_localities_for_constituency(
    places_df: pd.DataFrame,
    centroid_lat: float,
    centroid_lon: float,
    radius_km: float,
    top_n: int,
) -> list[str]:
    """Return the top N locality names within radius_km of a centroid."""
    dist = _haversine_km(centroid_lat, centroid_lon, places_df["lat"].values, places_df["lon"].values)
    nearby = places_df[dist <= radius_km]
    if nearby.empty:
        return []
    counts = nearby["locality"].value_counts()
    return counts.head(top_n).index.tolist()


def run(
    constituency_name: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    radius_km: float = DEFAULT_RADIUS_KM,
    output_path: Path = NEW_SCRAPE_PATH,
    force: bool = False,
    rebuild_cache: bool = False,
) -> pd.DataFrame:
    if not force and Path(output_path) == NEW_SCRAPE_PATH and _would_clobber_scraped_data(output_path):
        logger.error(
            "%s already contains scraped 'groups' data. Refusing to overwrite; "
            "pass --force or --output to write elsewhere.",
            output_path,
        )
        sys.exit(1)

    if rebuild_cache and UK_PLACES_CACHE.exists():
        UK_PLACES_CACHE.unlink()
        logger.info("Removed cached Overture places")

    if UK_PLACES_CACHE.exists():
        logger.info("Loading cached Overture places: %s", UK_PLACES_CACHE)
        places_df = pd.read_parquet(UK_PLACES_CACHE)
    else:
        places_df = download_uk_places(UK_PLACES_CACHE)
    logger.info("Loaded %d places", len(places_df))

    con_df = pd.read_csv(CONSTITUENCIES_PATH)
    if constituency_name:
        con_df = con_df[con_df["PCON24NM"] == constituency_name].copy()
        if con_df.empty:
            logger.error("Constituency '%s' not found in %s", constituency_name, CONSTITUENCIES_PATH)
            sys.exit(1)

    logger.info("Finding top %d localities per constituency (radius=%.1f km)…", top_n, radius_km)

    rows = []
    for _, c in con_df.iterrows():
        localities = top_localities_for_constituency(
            places_df,
            centroid_lat=c["LAT"],
            centroid_lon=c["LONG"],
            radius_km=radius_km,
            top_n=top_n,
        )
        if not localities:
            logger.warning("No Overture localities found near %s — skipping", c["PCON24NM"])
            continue
        for locality in localities:
            rows.append({**c.to_dict(), "place_name": locality})

    if not rows:
        logger.error("No search targets generated — check radius or Overture cache")
        sys.exit(1)

    result = pd.DataFrame(rows)
    result["processed"] = False
    result["groups"] = ""

    result.to_csv(output_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info(
        "Wrote %d search-target rows across %d constituencies → %s",
        len(result),
        result["PCON24NM"].nunique(),
        output_path,
    )
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generate UK constituency search targets from Overture Maps"
    )
    parser.add_argument(
        "--constituency", default=None,
        help="Generate for a single constituency (PCON24NM)",
    )
    parser.add_argument(
        "--top-n", type=int, default=DEFAULT_TOP_N,
        help=f"Top N localities per constituency (default: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--radius-km", type=float, default=DEFAULT_RADIUS_KM,
        help=f"Search radius from constituency centroid in km (default: {DEFAULT_RADIUS_KM})",
    )
    parser.add_argument(
        "--output", default=str(NEW_SCRAPE_PATH),
        help="Output CSV path",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite even if the master file has scraped 'groups' data",
    )
    parser.add_argument(
        "--rebuild-cache", action="store_true",
        help="Re-download Overture data even if the parquet cache exists",
    )
    args = parser.parse_args()

    run(
        constituency_name=args.constituency,
        top_n=args.top_n,
        radius_km=args.radius_km,
        output_path=Path(args.output),
        force=args.force,
        rebuild_cache=args.rebuild_cache,
    )


if __name__ == "__main__":
    main()
