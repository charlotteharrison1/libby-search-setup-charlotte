"""Ground-truth supplement for uk/generate_search_ward.py: look up a ward's
actual boundary polygon and query OpenStreetMap (via the Overpass API) for
named roads/POIs that fall inside it.

This exists because the LLM step in generate_search_ward.py answers from web
search results indexed under the ward's name — which is unreliable for small
or recently-redrawn wards that aren't well documented online. A boundary
polygon plus a spatial query is correct regardless of how well documented the
ward is, since it doesn't depend on the boundary change having been written
about anywhere.

Requires the ward boundary shapefile at uk/settings.WARD_BOUNDARIES_PATH
(ONS "Wards Boundaries UK BFE" or similar — one row per ward, with WD*NM /
LAD*NM name columns and polygon geometry).
"""

import logging

import geopandas as gpd
import requests
from shapely.geometry import LineString, Point

logger = logging.getLogger(__name__)

# Delimiter used to encode "<ward name> | <local authority>" into the
# search-targets CSV's existing 'county' column — shared by
# generate_search_ward.py (writer) and pipeline_ward.py (reader) so there's
# one place to change it. Not a comma: ward names can themselves contain a
# comma (e.g. "Harden, Goscote & Ryecroft"), which broke a naive comma-split.
COUNTY_DELIMITER = " | "

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 90

# Maps OSM tag combinations to the same category vocabulary
# generate_search_ward.py's LLM prompt uses (VALID_TYPES there).
#
# highway=residential/living_street/unclassified are included (local roads),
# but length-filtered to the biggest ones only (BIG_RESIDENTIAL_MIN_LENGTH_M
# below) — OSM tags every named street this way with no notability signal,
# so length is the proxy for "big enough to be a neighbourhood identifier."
# Balances the earlier two extremes: excluding local roads entirely (missed
# real streets), and including every one unfiltered (mostly noise).
#
# Still deliberately NOT querying amenity=marketplace: it picks up individual
# market-stall businesses rather than an actual market landmark. Hotels/pubs
# are excluded too — private businesses, not public landmarks.
_HIGHSTREET_HIGHWAYS = {"primary", "secondary", "tertiary", "pedestrian"}
_LOCAL_ROAD_HIGHWAYS = {"residential", "living_street", "unclassified"}
BIG_RESIDENTIAL_MIN_LENGTH_M = 300
_AMENITY_TO_TYPE = {
    "place_of_worship": "place_of_worship",
    "school": "school",
    "college": "school",
    "community_centre": "other_landmark",
}
_TOURISM_TO_TYPE = {"artwork": "other_landmark"}


def encode_county(ward_name: str, local_authority: str) -> str:
    """Build the 'county' field value generate_search_ward.py writes."""
    return f"{ward_name}{COUNTY_DELIMITER}{local_authority}" if local_authority else ward_name


def split_county(county: str) -> tuple[str, str]:
    """Split a 'county' field back into (ward_name, local_authority).
    local_authority is '' if it wasn't set."""
    parts = str(county).split(COUNTY_DELIMITER, 1)
    ward_name = parts[0].strip()
    local_authority = parts[1].strip() if len(parts) > 1 else ""
    return ward_name, local_authority


def _find_name_column(columns, prefix: str) -> str | None:
    """The ward/local-authority name column is versioned (WD24NM, WD26NM, …)
    — find whichever one is present rather than hardcoding a year."""
    candidates = [c for c in columns if c.startswith(prefix) and c.endswith("NM")]
    return candidates[0] if candidates else None


def load_ward_boundaries(path) -> gpd.GeoDataFrame:
    """Load the ward boundary shapefile, reprojected to EPSG:4326 (lon/lat)
    to match Overpass's coordinate system."""
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        raise ValueError(f"{path} has no CRS set")
    gdf = gdf.to_crs("EPSG:4326")
    logger.info("Loaded %d ward boundaries from %s", len(gdf), path)
    return gdf


def find_ward_geometry(gdf: gpd.GeoDataFrame, ward_name: str, local_authority: str = ""):
    """Return the (single) polygon for ward_name, or None if it can't be
    found or is ambiguous. Ward names are not unique nationally, so
    local_authority is used to disambiguate when there's more than one match."""
    ward_col = _find_name_column(gdf.columns, "WD")
    lad_col = _find_name_column(gdf.columns, "LAD")
    if ward_col is None:
        raise KeyError(f"No ward-name column found in boundary file (columns: {list(gdf.columns)})")

    matches = gdf[gdf[ward_col].str.casefold() == ward_name.casefold()]

    if len(matches) > 1 and local_authority and lad_col:
        narrowed = matches[matches[lad_col].str.casefold().str.contains(local_authority.casefold(), na=False)]
        if not narrowed.empty:
            matches = narrowed

    if matches.empty:
        logger.warning("Ward '%s' not found in boundary file", ward_name)
        return None
    if len(matches) > 1:
        options = ", ".join(matches[lad_col].tolist()) if lad_col else str(len(matches))
        logger.warning(
            "Ward '%s' is ambiguous (found in: %s) — add/narrow local_authority to disambiguate. Skipping boundary lookup.",
            ward_name, options,
        )
        return None

    return matches.iloc[0].geometry


def _classify_element(tags: dict) -> str | None:
    name = tags.get("name", "")
    highway = tags.get("highway")
    if highway == "pedestrian" or "high street" in name.lower():
        return "highstreet"
    if highway in _HIGHSTREET_HIGHWAYS:
        return "highstreet"
    if highway in _LOCAL_ROAD_HIGHWAYS:
        return "residential_road"
    if tags.get("amenity") in _AMENITY_TO_TYPE:
        return _AMENITY_TO_TYPE[tags["amenity"]]
    if tags.get("leisure") == "park":
        return "park"
    if tags.get("tourism") in _TOURISM_TO_TYPE:
        return _TOURISM_TO_TYPE[tags["tourism"]]
    return None


def query_overpass_within(polygon) -> list[dict]:
    """Query Overpass for named roads/POIs within polygon's bounding box, then
    keep only elements that actually fall inside the polygon. Returns a list
    of {"name", "type", "confidence": "high"} dicts (real OSM data, so
    treated as high-confidence — unlike the LLM step, this isn't a guess)."""
    minx, miny, maxx, maxy = polygon.bounds
    bbox = f"{miny},{minx},{maxy},{maxx}"  # Overpass wants (south,west,north,east)

    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT_S}];
    (
      way["highway"~"^(primary|secondary|tertiary|pedestrian|residential|living_street|unclassified)$"]["name"]({bbox});
      node["amenity"~"^(place_of_worship|school|college|community_centre)$"]["name"]({bbox});
      way["amenity"~"^(place_of_worship|school|college|community_centre)$"]["name"]({bbox});
      node["leisure"="park"]["name"]({bbox});
      way["leisure"="park"]["name"]({bbox});
      node["tourism"="artwork"]["name"]({bbox});
      way["tourism"="artwork"]["name"]({bbox});
    );
    out geom tags;
    """
    # "out geom" (not "out center") because the residential-road length
    # filter below needs each way's full shape, not just a point.

    headers = {"User-Agent": "libby-search-setup-ward-script/1.0 (one-off ward research, contact: repo owner)"}
    try:
        resp = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=OVERPASS_TIMEOUT_S + 10)
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except Exception as e:
        logger.warning("Overpass query failed: %s", e)
        return []

    seen = set()
    items = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue

        geometry_coords = el.get("geometry")  # list of {"lat", "lon"} for ways
        if el.get("lat") is not None:  # node
            point = Point(el["lon"], el["lat"])
            line = None
        elif geometry_coords:  # way
            line = LineString([(c["lon"], c["lat"]) for c in geometry_coords])
            point = line.centroid
        else:
            continue

        if not polygon.contains(point):
            continue

        item_type = _classify_element(tags)
        if item_type is None:
            continue

        if item_type == "residential_road":
            if line is None:
                continue
            length_m = gpd.GeoSeries([line], crs="EPSG:4326").to_crs("EPSG:27700").length.iloc[0]
            if length_m < BIG_RESIDENTIAL_MIN_LENGTH_M:
                continue

        key = (name.casefold(), item_type)
        if key in seen:
            continue
        seen.add(key)
        items.append({"name": name, "type": item_type, "confidence": "high"})

    logger.info("Overpass: %d in-boundary items found", len(items))
    return items
