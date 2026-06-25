#!/usr/bin/env python3
"""District data-prep: download places for a US congressional district from
Overture Maps and produce the locality/place lists that drive the Facebook
group scrape.

This is run ONCE per district, before the scrape, to generate the
``<district>_places.csv`` / ``<district>_localities.csv`` inputs that
``us/pipeline.py`` later consumes (after the scrape has filled in a ``groups``
column).

Pipeline:
  1. Download the Census congressional-district boundary.
  2. Query Overture Places within the boundary (via DuckDB + S3) → GeoPackage.
  3. List localities, ranked by place count.

⚠️  MANUAL STEP between 3 and 4: the original workflow hand-curates a
``filters.csv`` (locality → Facebook search filter URL). Locality filtering and
the final ``*_places.csv`` / ``*_localities.csv`` shaping depend on that file.
That curation is intentionally left manual — see us/README.md. The functions
below cover the automatable parts; adapt the bottom section per district.

Requires: geopandas, duckdb (with spatial + httpfs extensions).
"""

import argparse
import json
import logging
from pathlib import Path

import geopandas as gpd

logger = logging.getLogger(__name__)

OVERTURE_RELEASE = "2026-05-20.0"
CENSUS_CD_URL = "https://www2.census.gov/geo/tiger/TIGER2025/CD/tl_2025_17_cd119.zip"


def download_boundary(statefp: str, cd_fp: str, out_geojson: Path) -> tuple[float, float, float, float]:
    """Download the Census CD boundary, write it as GeoJSON, return its bbox."""
    cds = gpd.read_file(CENSUS_CD_URL)
    district = cds[(cds["STATEFP"] == statefp) & (cds["CD119FP"] == cd_fp)].to_crs(4326)
    district.to_file(out_geojson, driver="GeoJSON")
    minx, miny, maxx, maxy = district.total_bounds
    logger.info("District bbox: %s %s %s %s", minx, miny, maxx, maxy)
    return float(minx), float(miny), float(maxx), float(maxy)


def query_overture_places(boundary_geojson: Path, bbox, out_gpkg: Path) -> None:
    """Query Overture Places intersecting the boundary into a GeoPackage."""
    import duckdb

    minx, miny, maxx, maxy = bbox
    places_path = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*"

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    con.execute(f"""
        COPY (
            WITH boundary AS (SELECT geom FROM ST_Read('{boundary_geojson}')),
            candidates AS (
                SELECT
                    id, names.primary AS name, categories.primary AS category,
                    basic_category, confidence, operating_status,
                    addresses[1].freeform AS address, addresses[1].locality AS locality,
                    addresses[1].region AS region, addresses[1].postcode AS postcode,
                    websites[1] AS website, geometry
                FROM read_parquet('{places_path}', filename=true, hive_partitioning=1)
                WHERE bbox.xmin BETWEEN {minx} AND {maxx}
                  AND bbox.ymin BETWEEN {miny} AND {maxy}
                  AND names.primary IS NOT NULL
                  AND (confidence IS NULL OR confidence >= 0.70)
                  AND (operating_status IS NULL OR operating_status NOT IN ('closed_permanently'))
            )
            SELECT p.* FROM candidates p JOIN boundary b
            ON ST_Intersects(p.geometry,
               ST_Transform(b.geom, 'EPSG:4326', 'OGC:CRS84', always_xy := true))
        ) TO '{out_gpkg}' WITH (FORMAT GDAL, DRIVER 'GPKG');
    """)
    logger.info("Wrote Overture places → %s", out_gpkg)


def list_categories(gpkg: Path, out_json: Path) -> None:
    """Dump the distinct basic_category values (helps curate interesting types)."""
    gdf = gpd.read_file(gpkg)
    json.dump(sorted(str(c) for c in gdf.basic_category.dropna().unique()), open(out_json, "w"), indent=2)
    logger.info("Wrote %d categories → %s", gdf.basic_category.nunique(), out_json)


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Download Overture places for a US congressional district")
    parser.add_argument("--district", default="il-14")
    parser.add_argument("--statefp", default="17", help="Census state FIPS (17 = Illinois)")
    parser.add_argument("--cd-fp", default="14", help="Census congressional-district number")
    parser.add_argument("--workdir", default=None, help="Output dir (default us/data/<district>)")
    args = parser.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path(__file__).resolve().parent / "data" / args.district
    workdir.mkdir(parents=True, exist_ok=True)

    geojson = workdir / f"{args.district}.geojson"
    gpkg = workdir / f"{args.district}_overture_places.gpkg"

    bbox = download_boundary(args.statefp, args.cd_fp, geojson)
    query_overture_places(geojson, bbox, gpkg)
    list_categories(gpkg, workdir / "types_of_place.json")

    logger.info(
        "Done. Next: hand-curate locality filters (filters.csv) and shape "
        "%s_places.csv / %s_localities.csv — see us/README.md.",
        args.district, args.district,
    )


if __name__ == "__main__":
    main()
