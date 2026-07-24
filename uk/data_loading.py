import logging

import geopandas as gpd
import pandas as pd

from uk.settings import (
    DENSITIES_PATH,
    GEOJSON_PATH,
    NEW_SCRAPE_PATH,
    PCON_MAPPING_PATH,
    PREVIOUS_SCRAPE_PATH,
)

logger = logging.getLogger(__name__)


def load_new_scrape(path=NEW_SCRAPE_PATH) -> pd.DataFrame:
    """Load the master constituency/place data file, filter to processed rows,
    and explode the groups column into one row per group."""
    df = pd.read_csv(path)
    df = df[df.processed].copy()
    def _parse_groups(x):
        if not isinstance(x, str) or not x.strip():
            return []
        try:
            return eval(x)
        except Exception:
            return []

    df["groups_list"] = df.groups.apply(_parse_groups)

    df_exploded = df.explode("groups_list").reset_index(drop=True)
    group_keys_df = df_exploded["groups_list"].apply(pd.Series)
    df_exploded = pd.concat([df_exploded, group_keys_df], axis=1)

    if "LAT" in df_exploded.columns:
        df_exploded["LAT"] = pd.to_numeric(df_exploded["LAT"], errors="coerce")
    if "LONG" in df_exploded.columns:
        df_exploded["LONG"] = pd.to_numeric(df_exploded["LONG"], errors="coerce")

    logger.info("Loaded new scrape: %d rows after explode", len(df_exploded))
    return df_exploded


def load_previous_scrape(path=PREVIOUS_SCRAPE_PATH) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    logger.info("Loaded previous scrape: %d rows", len(df))
    return df


def load_pcon_mapping(path=PCON_MAPPING_PATH) -> pd.DataFrame:
    df = pd.read_csv(path)
    logger.info("Loaded PCON mapping: %d rows", len(df))
    return df


def load_densities(path=DENSITIES_PATH) -> pd.DataFrame:
    last_err = None
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin1"):
        try:
            df = pd.read_csv(
                path,
                dtype={"gss_code": str},
                encoding=enc,
                encoding_errors="replace",
            )
            logger.info("Loaded densities: %d rows (encoding=%s)", len(df), enc)
            return df
        except UnicodeDecodeError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


def load_constituency_boundaries(path=GEOJSON_PATH) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    if "PCON24CD" not in gdf.columns:
        raise KeyError(f"'PCON24CD' not found. Available: {list(gdf.columns)}")
    logger.info("Loaded constituency boundaries: %d features", len(gdf))
    return gdf
