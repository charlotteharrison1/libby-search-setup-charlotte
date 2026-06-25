import logging

import geopandas as gpd
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Haversine helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorized haversine distance in km."""
    R = 6371.0088
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * (np.sin(dlon / 2.0) ** 2)
    return 2.0 * R * np.arcsin(np.sqrt(a))


def max_avg_distance_km(lats, lons):
    """Return (max_km, avg_km) across all pairs of points."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    n = len(lats)
    if n <= 1:
        return 0.0, 0.0
    if n > 250:
        return float("inf"), float("inf")

    d = haversine_km(lats[:, None], lons[:, None], lats[None, :], lons[None, :])
    iu = np.triu_indices(n, k=1)
    vals = d[iu]
    if vals.size == 0:
        return 0.0, 0.0
    return float(np.max(vals)), float(np.mean(vals))


# ---------------------------------------------------------------------------
# Locality classification
# ---------------------------------------------------------------------------

def classify_locality_from_places(g: pd.DataFrame) -> pd.Series:
    """Classify a group's locality as L (local) / R (regional) / X (national)
    based on the places it was found in."""
    _g = g.copy()
    _g["LAT"] = pd.to_numeric(_g.get("LAT"), errors="coerce")
    _g["LONG"] = pd.to_numeric(_g.get("LONG"), errors="coerce")
    _g = _g.dropna(subset=["place_name", "LAT", "LONG"], how="any")
    _g = _g.drop_duplicates(subset=["place_name", "LAT", "LONG"])

    place_names = _g["place_name"].dropna().unique()
    n_places = int(len(place_names))
    n_pcon = int(_g["PCON24CD"].nunique()) if "PCON24CD" in _g.columns else 0

    if n_places == 0:
        return pd.Series({"locality": "X", "locality_name": ""})
    if n_places > 200:
        return pd.Series({"locality": "X", "locality_name": ""})
    if n_places >= 20 and n_pcon > 10:
        return pd.Series({"locality": "X", "locality_name": ""})

    max_km, avg_km = max_avg_distance_km(_g["LAT"].to_numpy(), _g["LONG"].to_numpy())

    if n_places <= 1 or max_km <= 15:
        locality = "L"
        locality_name = str(_g["place_name"].mode().iloc[0]) if not _g["place_name"].mode().empty else ""
    elif avg_km <= 100:
        locality = "R"
        if "PCON24NM" in _g.columns and not _g["PCON24NM"].mode().empty:
            locality_name = str(_g["PCON24NM"].mode().iloc[0])
        else:
            locality_name = ""
    else:
        locality = "X"
        locality_name = ""

    return pd.Series({"locality": locality, "locality_name": locality_name})


# ---------------------------------------------------------------------------
# Density-to-distance power-law mapping
# ---------------------------------------------------------------------------

def density_to_distance(density, min_density, max_density, min_add_on=200, max_add_on=5000):
    """Power-law mapping: lowest density -> max_add_on, highest -> min_add_on."""
    density = np.array(density, dtype=float)
    p = np.log(max_add_on / min_add_on) / np.log(max_density / min_density)
    A = max_add_on * (min_density ** p)
    return A * (density ** -p)


# ---------------------------------------------------------------------------
# Build add-on candidates from both scrape sources
# ---------------------------------------------------------------------------

def _build_addon_candidates(
    df_previous: pd.DataFrame,
    df_new_exploded: pd.DataFrame,
    constituency_codes: set[str],
) -> pd.DataFrame:
    """Build a unified table of LOCAL groups from both scrapes for add-on
    spatial joining."""

    # --- Locality for new scrape groups ---
    coi_df = df_new_exploded[df_new_exploded.PCON24CD.isin(constituency_codes)].copy()
    for col in ["public_y_n", "members", "posts_a_month"]:
        if col not in coi_df.columns:
            coi_df[col] = pd.NA

    new_candidates = (
        coi_df.groupby("url", dropna=False)
        .apply(
            lambda g: pd.Series({
                "pcon21cd": (
                    g["PCON24CD"].mode().iloc[0]
                    if ("PCON24CD" in g.columns and not g["PCON24CD"].mode().empty)
                    else g["PCON24CD"].iloc[0] if "PCON24CD" in g.columns else pd.NA
                ),
                "pcon21nm": (
                    g["PCON24NM"].mode().iloc[0]
                    if ("PCON24NM" in g.columns and not g["PCON24NM"].mode().empty)
                    else g["PCON24NM"].iloc[0] if "PCON24NM" in g.columns else ""
                ),
                "name": g["name"].iloc[0] if "name" in g.columns else "",
                "url": g.name,
                "lat": pd.to_numeric(g.get("LAT"), errors="coerce").median(),
                "long": pd.to_numeric(g.get("LONG"), errors="coerce").median(),
                **classify_locality_from_places(g).to_dict(),
            }),
            include_groups=False,
        )
        .reset_index(drop=True)
    )

    # --- Previous scrape ---
    _df_prev = df_previous.copy()
    for req in ["locality", "pcon21cd", "lat", "long", "url", "name"]:
        if req not in _df_prev.columns:
            raise KeyError(f"df_previous is missing required column: '{req}'")
    _df_prev["lat"] = pd.to_numeric(_df_prev["lat"], errors="coerce")
    _df_prev["long"] = pd.to_numeric(_df_prev["long"], errors="coerce")
    _df_prev["pcon21cd"] = _df_prev["pcon21cd"].astype("string").str.strip().replace({"": pd.NA})

    _df_new = new_candidates.copy()
    _df_new["lat"] = pd.to_numeric(_df_new["lat"], errors="coerce")
    _df_new["long"] = pd.to_numeric(_df_new["long"], errors="coerce")
    _df_new["pcon21cd"] = _df_new["pcon21cd"].astype("string").str.strip().replace({"": pd.NA})

    candidates = pd.concat([_df_prev, _df_new], axis=0, ignore_index=True, sort=False)
    candidates = candidates[candidates["locality"] == "L"].copy()
    candidates = candidates.dropna(subset=["lat", "long", "pcon21cd", "url"])
    return candidates


# ---------------------------------------------------------------------------
# Full geographic add-on pipeline
# ---------------------------------------------------------------------------

def compute_geographic_addon(
    df_previous: pd.DataFrame,
    df_new_exploded: pd.DataFrame,
    gdf_pcon: gpd.GeoDataFrame,
    densities_df: pd.DataFrame,
    constituency_codes: set[str],
    global_min_add_on: int = 500,
    global_max_add_on: int = 3000,
) -> pd.DataFrame:
    """Compute the geographic add-on: find local groups from neighbouring
    constituencies that fall within a density-scaled distance of the
    constituency boundary.

    Returns a DataFrame of add-on rows with locality='A'.
    """

    # Filter boundaries to requested constituencies
    gdf = gdf_pcon[gdf_pcon["PCON24CD"].isin(constituency_codes)].copy()
    if gdf.empty:
        logger.warning("No constituency polygons matched the requested codes")
        return pd.DataFrame()

    # Build candidate points
    candidates = _build_addon_candidates(df_previous, df_new_exploded, constituency_codes)
    if candidates.empty:
        return pd.DataFrame()

    gdf_pts = gpd.GeoDataFrame(
        candidates,
        geometry=gpd.points_from_xy(candidates["long"], candidates["lat"]),
        crs="EPSG:4326",
    )

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf_pts = gdf_pts.to_crs(gdf.crs)

    # Density-scaled max distances
    densities_df["density"] = pd.to_numeric(densities_df["density"], errors="coerce")
    density_by_pcon = densities_df.set_index("gss_code")["density"]

    all_dens = densities_df["density"].dropna()
    if all_dens.empty:
        raise ValueError("No numeric densities found")
    dens_min, dens_max = float(all_dens.min()), float(all_dens.max())
    if dens_min <= 0:
        raise ValueError(f"Lowest density <= 0 ({dens_min})")

    pcon_dens = pd.Series(
        {code: density_by_pcon.get(code, pd.NA) for code in constituency_codes},
        name="density",
    )
    d = pd.to_numeric(pcon_dens, errors="coerce")
    raw_dist = density_to_distance(d, dens_min, dens_max, global_min_add_on, global_max_add_on)
    pcon_max_dist_m = pd.Series(raw_dist, index=d.index).round().astype("Int64").dropna().astype(int)

    logger.info(
        "Add-on distances: min=%d max=%d median=%d",
        pcon_max_dist_m.min(), pcon_max_dist_m.max(), int(pcon_max_dist_m.median()),
    )

    # Project to metres for distance calculations
    gdf_m = gdf.to_crs("EPSG:27700")
    gdf_pts_m = gdf_pts.to_crs("EPSG:27700")

    gdf_boundary = gdf_m[["PCON24CD", "geometry"]].copy()
    gdf_boundary["geometry"] = gdf_boundary.geometry.boundary

    median_dist = int(pcon_max_dist_m.median())
    gdf_boundary["max_dist_m"] = (
        gdf_boundary["PCON24CD"].map(pcon_max_dist_m).fillna(median_dist).astype(int)
    )

    # Spatial join
    try:
        joined = gpd.sjoin_nearest(
            gdf_pts_m,
            gdf_boundary[["PCON24CD", "max_dist_m", "geometry"]],
            how="inner",
            distance_col="dist_to_boundary_m",
        )
        joined = joined[joined["dist_to_boundary_m"] <= joined["max_dist_m"]]
    except Exception:
        corridors = gdf_boundary.copy()
        corridors["geometry"] = corridors.apply(lambda r: r.geometry.buffer(r["max_dist_m"]), axis=1)
        joined = gpd.sjoin(
            gdf_pts_m,
            corridors[["PCON24CD", "max_dist_m", "geometry"]],
            how="inner",
            predicate="intersects",
        )
        _boundary_geom = gdf_boundary[["PCON24CD", "geometry"]].rename(columns={"geometry": "boundary_geom"})
        joined = joined.merge(_boundary_geom, on="PCON24CD", how="left")
        joined["dist_to_boundary_m"] = joined.geometry.distance(joined["boundary_geom"])
        joined = joined[joined["dist_to_boundary_m"] <= joined["max_dist_m"]]
        joined = joined.drop(columns=["boundary_geom"])

    # Keep only add-on rows: groups from a *different* constituency
    if "pcon21cd" not in joined.columns:
        raise KeyError("Joined dataframe missing 'pcon21cd'")
    joined = joined[joined["pcon21cd"].notna()].copy()
    joined = joined[joined["pcon21cd"] != joined["PCON24CD"]].copy()
    joined["locality"] = "A"

    drop_cols = [c for c in ["geometry", "index_right"] if c in joined.columns]
    return joined.drop(columns=drop_cols)
