"""
CityGML 2.0 → Neo4j ingestion — lossless, dataset-agnostic, all modules.
Optimized for large files (GBs): streaming iterparse + batched UNWIND writes.

Supported CityGML 2.0 modules
───────────────────────────────
  Building · Transportation · Vegetation · WaterBody · LandUse
  CityFurniture · Relief · Bridge · Tunnel · CityObjectGroup

Graph schema
────────────
(Dataset)
(Building)              -[:PART_OF]->                      (Dataset)
(Building)              -[:LOCATED_IN]->                   (District)
(District)              -[:PART_OF]->                      (City)
(Dataset)               -[:COVERS]->                       (District)
(Building)              -[:HAS_FUNCTION]->                 (BuildingFunction)
(Building)              -[:HAS_ROOF_TYPE]->                (RoofType)
(Building)              -[:HAS_LOD_SOLID]->                (GeometrySolid)
(GeometrySolid)         -[:HAS_SURFACE_MEMBER {order}]->   (GeometryPolygon)
(Building)              -[:HAS_BOUNDARY {order}]->         (BoundarySurface)
(BoundarySurface)       -[:HAS_POLYGON {order}]->          (GeometryPolygon)
(GeometryPolygon)       -[:HAS_EXTERIOR_RING]->            (GeometryRing)
(GeometryPolygon)       -[:HAS_INTERIOR_RING {ring_index}]-> (GeometryRing)
(Building)              -[:HAS_TERRAIN_INTERSECTION]->     (TerrainIntersection)
(TerrainIntersection)   -[:HAS_LINE {order}]->             (GeometryLineString)
(Building)              -[:NEARBY_BUILDING {distance_m}]-> (Building)

(CityObject)            -[:PART_OF]->                      (Dataset)
(CityObject)            -[:HAS_LOD_SOLID]->                (GeometrySolid)
(CityObject)            -[:HAS_LOD_SURFACE {lod}]->        (GeometryMultiSurface)
(GeometryMultiSurface)  -[:HAS_SURFACE_MEMBER {order}]->   (GeometryPolygon)
(CityObject)            -[:HAS_BOUNDARY {order}]->         (BoundarySurface)

CityObject carries a feature_type property (e.g. "Road", "WaterBody") and
also has a module-specific Neo4j label added via APOC (requires APOC plugin).

Node properties (lossless round-trip)
──────────────────────────────────────
Building         id, name, source, creation_date?, function_code?,
                 function_code_space?, roof_type_code?, measured_height?,
                 measured_height_uom?, storeys_above_ground?,
                 storeys_below_ground?, external_information_system?,
                 external_object_name?, bbox_*, center_x/y, ground_z,
                 bbox_wkt, location, description, _gen_attrs_map,
                 <all gen:*Attribute names as lowercase props>

CityObject       id, name, feature_type, source, creation_date?,
                 class?, function?, usage?, bbox_*, center_x/y,
                 bbox_wkt (2D WKT POLYGON for R-tree, when bbox valid),
                 location (Neo4j point({x,y}), when bbox valid),
                 _gen_attrs_map, <module-specific scalar attrs>,
                 <all gen:*Attribute names as lowercase props>

BoundarySurface  id, surface_type, lod, multi_surface_id?,
                 creation_date?, source, _gen_attrs_map,
                 <all gen:*Attribute names as lowercase props>

GeometrySolid    id (gml:Solid gml:id), lod, composite_surface_id?, source
GeometryMultiSurface  id (gml:MultiSurface gml:id), lod, source
GeometryPolygon  id (gml:Polygon gml:id), source
GeometryRing     id, pos_list (verbatim), srs_dimension, ring_type, source
TerrainIntersection  id, lod, source
GeometryLineString   id, pos_list (verbatim), srs_dimension, source
"""

import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from lxml import etree
from neo4j import GraphDatabase
from neo4j.exceptions import TransientError

# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

NS = {
    "cg":    "http://www.opengis.net/citygml/2.0",
    "gml":   "http://www.opengis.net/gml",
    "bldg":  "http://www.opengis.net/citygml/building/2.0",
    "gen":   "http://www.opengis.net/citygml/generics/2.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "tran":  "http://www.opengis.net/citygml/transportation/2.0",
    "veg":   "http://www.opengis.net/citygml/vegetation/2.0",
    "wtr":   "http://www.opengis.net/citygml/waterbody/2.0",
    "luse":  "http://www.opengis.net/citygml/landuse/2.0",
    "frn":   "http://www.opengis.net/citygml/cityfurniture/2.0",
    "dem":   "http://www.opengis.net/citygml/relief/2.0",
    "brid":  "http://www.opengis.net/citygml/bridge/2.0",
    "tun":   "http://www.opengis.net/citygml/tunnel/2.0",
    "grp":   "http://www.opengis.net/citygml/cityobjectgroup/2.0",
    "app":   "http://www.opengis.net/citygml/appearance/2.0",
}

# Namespace URIs that this ingester models natively. Any direct-child element of
# a feature whose namespace is NOT in this set is treated as ADE / generic
# extension content (incl. gml:genericADEElement) and preserved verbatim.
_KNOWN_NS = set(NS.values())

GML_ROOT_NS_RE = re.compile(r'"(http://www\.opengis\.net/citygml/[\d.]+)"')

GML_ID     = "{http://www.opengis.net/gml}id"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

BLDG_TAG     = "{http://www.opengis.net/citygml/building/2.0}Building"
ENVELOPE_TAG = "{http://www.opengis.net/gml}Envelope"

BATCH_SIZE = 1000

# ---------------------------------------------------------------------------
# Module registry
# ---------------------------------------------------------------------------
# Each entry: tag → (neo4j_label, ns_prefix, scalar_attr_list,
#                    boundary_surface_localnames, geom_style, lod_range)
#
# geom_style:
#   "solid_or_surface" — tries lod*Solid then lod*MultiSurface
#   "surface_only"     — only lod*MultiSurface (Transportation, LandUse)
#   "geometry"         — lod*Geometry (Vegetation, CityFurniture, CityObjectGroup)
#   "relief"           — special: ReliefFeature with nested components

_TRAN_NS   = NS["tran"]
_VEG_NS    = NS["veg"]
_WTR_NS    = NS["wtr"]
_LUSE_NS   = NS["luse"]
_FRN_NS    = NS["frn"]
_DEM_NS    = NS["dem"]
_BRID_NS   = NS["brid"]
_TUN_NS    = NS["tun"]
_GRP_NS    = NS["grp"]
_GEN_NS    = NS["gen"]

# scalar_attr_list: list of (prop_name, xpath_with_ns_prefix)
_TRAN_ATTRS = [
    ("class",            "tran:class"),
    ("function",         "tran:function"),
    ("usage",            "tran:usage"),
    ("surface_material", "tran:surfaceMaterial"),
]
_VEG_ATTRS = [
    ("class",          "veg:class"),
    ("function",       "veg:function"),
    ("usage",          "veg:usage"),
    ("species",        "veg:species"),
    ("height",         "veg:height"),
    ("trunk_diameter", "veg:trunkDiameter"),
    ("crown_diameter", "veg:crownDiameter"),
    ("average_height", "veg:averageHeight"),
]
_WTR_ATTRS = [
    ("class",       "wtr:class"),
    ("function",    "wtr:function"),
    ("usage",       "wtr:usage"),
    ("water_level", "wtr:waterLevel"),
]
_LUSE_ATTRS = [
    ("class",    "luse:class"),
    ("function", "luse:function"),
    ("usage",    "luse:usage"),
]
_FRN_ATTRS = [
    ("class",    "frn:class"),
    ("function", "frn:function"),
    ("usage",    "frn:usage"),
]
_DEM_ATTRS = [("lod", "dem:lod")]   # dem:ReliefFeature/dem:lod (child element)
_BRID_ATTRS = [
    ("class",                "brid:class"),
    ("function",             "brid:function"),
    ("usage",                "brid:usage"),
    ("year_of_construction", "brid:yearOfConstruction"),
    ("year_of_demolition",   "brid:yearOfDemolition"),
    ("is_movable",           "brid:isMovable"),
]
_TUN_ATTRS = [
    ("class",                "tun:class"),
    ("function",             "tun:function"),
    ("usage",                "tun:usage"),
    ("year_of_construction", "tun:yearOfConstruction"),
    ("year_of_demolition",   "tun:yearOfDemolition"),
]
_GRP_ATTRS = [
    ("class",    "grp:class"),
    ("function", "grp:function"),
    ("usage",    "grp:usage"),
]
_GEN_ATTRS = [
    ("class",    "gen:class"),
    ("function", "gen:function"),
    ("usage",    "gen:usage"),
]

_WTR_BSURF  = ["WaterSurface", "WaterGroundSurface", "WaterClosureSurface"]
_BRID_BSURF = [
    "GroundSurface", "WallSurface", "RoofSurface", "ClosureSurface",
    "FloorSurface", "InteriorWallSurface", "CeilingSurface",
    "OuterFloorSurface", "OuterCeilingSurface",
]
_TUN_BSURF = _BRID_BSURF  # same surface type names

MODULE_REGISTRY: dict = {
    f"{{{_TRAN_NS}}}Road":                  ("Road",                  "tran", _TRAN_ATTRS, [],          "surface_only",     ("0","1","2","3","4")),
    f"{{{_TRAN_NS}}}Railway":               ("Railway",               "tran", _TRAN_ATTRS, [],          "surface_only",     ("0","1","2","3","4")),
    f"{{{_TRAN_NS}}}Track":                 ("Track",                 "tran", _TRAN_ATTRS, [],          "surface_only",     ("0","1","2","3","4")),
    f"{{{_TRAN_NS}}}Square":                ("Square",                "tran", _TRAN_ATTRS, [],          "surface_only",     ("0","1","2","3","4")),
    f"{{{_TRAN_NS}}}TransportationComplex": ("TransportationComplex", "tran", _TRAN_ATTRS, [],          "surface_only",     ("0","1","2","3","4")),
    f"{{{_VEG_NS}}}SolitaryVegetationObject": ("SolitaryVegetationObject", "veg", _VEG_ATTRS, [],      "geometry",         ("1","2","3","4")),
    f"{{{_VEG_NS}}}PlantCover":             ("PlantCover",            "veg",  _VEG_ATTRS,  [],          "geometry",         ("1","2","3","4")),
    f"{{{_WTR_NS}}}WaterBody":              ("WaterBody",             "wtr",  _WTR_ATTRS,  _WTR_BSURF,  "solid_or_surface", ("0","1","2","3","4")),
    f"{{{_LUSE_NS}}}LandUse":              ("LandUse",               "luse", _LUSE_ATTRS, [],          "surface_only",     ("0","1","2","3","4")),
    f"{{{_FRN_NS}}}CityFurniture":         ("CityFurniture",         "frn",  _FRN_ATTRS,  [],          "geometry",         ("1","2","3","4")),
    f"{{{_DEM_NS}}}ReliefFeature":         ("ReliefFeature",         "dem",  _DEM_ATTRS,  [],          "relief",           ("0","1","2","3","4")),
    f"{{{_BRID_NS}}}Bridge":               ("Bridge",                "brid", _BRID_ATTRS, _BRID_BSURF, "solid_or_surface", ("1","2","3","4")),
    f"{{{_TUN_NS}}}Tunnel":                ("Tunnel",                "tun",  _TUN_ATTRS,  _TUN_BSURF,  "solid_or_surface", ("1","2","3","4")),
    f"{{{_GRP_NS}}}CityObjectGroup":       ("CityObjectGroup",       "grp",  _GRP_ATTRS,  [],          "geometry",         ("0","1","2","3","4")),
    f"{{{_GEN_NS}}}GenericCityObject":     ("GenericCityObject",     "gen",  _GEN_ATTRS,  [],          "geometry",         ("0","1","2","3","4")),
}

CITY_OBJECT_TAGS = list(MODULE_REGISTRY.keys())

# ---------------------------------------------------------------------------
# ALKIS code tables
# ---------------------------------------------------------------------------

BUILDING_FUNCTION_NAMES = {
    "31001_1000": "Residential Building",
    "31001_1010": "Detached House",
    "31001_1020": "Residential Home / Dormitory",
    "31001_1100": "Mixed-Use Building with Residential",
    "31001_1110": "Residential Building with Community Facilities",
    "31001_1120": "Residential Building with Trade and Services",
    "31001_2000": "Commercial / Industrial Building",
    "31001_2010": "Trade and Services Building",
    "31001_2020": "Office Building",
    "31001_2030": "Credit Institution",
    "31001_2040": "Insurance Building",
    "31001_2050": "Commercial Building",
    "31001_2051": "Department Store",
    "31001_2052": "Shopping Centre",
    "31001_2060": "Trade Fair Hall",
    "31001_2070": "Accommodation Building",
    "31001_2071": "Hotel / Motel / Pension",
    "31001_2081": "Restaurant / Gasthaus",
    "31001_2100": "Industrial and Commercial Building",
    "31001_2110": "Production Building",
    "31001_2111": "Factory",
    "31001_2120": "Workshop",
    "31001_2130": "Petrol Station",
    "31001_2140": "Storage Building",
    "31001_2143": "Warehouse / Storage Hall",
    "31001_2150": "Freight / Forwarding Building",
    "31001_2160": "Research Building",
    "31001_2180": "Industrial Social Facilities Building",
    "31001_2210": "Mill",
    "31001_2220": "Weather Station",
    "31001_2400": "Transport Operations Building",
    "31001_2410": "Road Transport Operations Building",
    "31001_2420": "Railway Operations Building",
    "31001_2430": "Air Transport Operations Building",
    "31001_2440": "Shipping Operations Building",
    "31001_2441": "Shipyard Hall",
    "31001_2444": "Boathouse",
    "31001_2460": "Parking Building",
    "31001_2461": "Multi-Storey Car Park",
    "31001_2463": "Garage",
    "31001_2465": "Underground Car Park",
    "31001_2500": "Utilities / Supply Building",
    "31001_2510": "Water Supply Building",
    "31001_2511": "Waterworks",
    "31001_2520": "Electricity Supply Building",
    "31001_2522": "Electrical Substation",
    "31001_2540": "Telecommunications Building",
    "31001_2570": "Gas Supply Building",
    "31001_2580": "Heating Plant",
    "31001_3000": "Public Purposes Building",
    "31001_3010": "Administrative Building",
    "31001_3011": "Parliament",
    "31001_3012": "Town Hall",
    "31001_3016": "Embassy / Consulate",
    "31001_3020": "Building for Education and Research",
    "31001_3021": "School",
    "31001_3023": "University Building",
    "31001_3024": "Research Institute",
    "31001_3030": "Building for Cultural Purposes",
    "31001_3046": "Mosque",
    "31001_3050": "Healthcare Building",
    "31001_3060": "Building for Social Purposes",
    "31001_3065": "Nursery / Kindergarten",
    "31001_3070": "Security and Order Building",
    "31001_3071": "Police Station",
    "31001_3072": "Fire Station",
    "31001_3073": "Barracks",
    "31001_3074": "Air Raid Shelter / Bunker",
    "31001_3210": "Sports Building",
    "31001_3211": "Sports Hall / Gymnasium",
    "31001_3220": "Bath / Swimming Building",
    "31001_3230": "Stadium Building",
    "31001_9998": "Not Specifiable",
    "51002_1210": "Clarification Basin",
    "51002_1220": "Wind Turbine",
    "51002_1230": "Solar Panels",
    "51002_1250": "Mast",
    "51002_1260": "Radio / Telecommunications Mast",
    "51002_1290": "Chimney / Flue",
    "51002_1330": "Crane",
    "51002_9999": "Other Industrial/Commercial Structure",
    "51009_1610": "Canopy / Shelter",
    "51009_1700": "Wall",
    "51009_1750": "Monument / Memorial / Statue",
    "51009_9999": "Other Structure or Installation",
}

ROOF_TYPE_NAMES = {
    "1000": "Flat Roof",
    "2100": "Mono-Pitch Roof",
    "2200": "Offset Mono-Pitch Roof",
    "3100": "Gable Roof",
    "3200": "Hip Roof",
    "3300": "Half-Hip Roof",
    "3400": "Mansard Roof",
    "3500": "Tent / Pyramid Roof",
    "3600": "Conical Roof",
    "3700": "Dome Roof",
    "3800": "Sawtooth Roof",
    "3900": "Barrel / Arch Roof",
    "4000": "Tower Roof / Spire",
    "5000": "Mixed Form",
    "9999": "Other",
}

GEMEINDE_MAP = {
    "02100104": ("HafenCity",  "Hamburg"),
    "02100":    ("Hamburg",    "Hamburg"),
    "09161000": ("Ingolstadt", "Ingolstadt"),
    "09161":    ("Ingolstadt", "Ingolstadt"),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(el, xpath):
    found = el.find(xpath, NS)
    return found.text.strip() if found is not None and found.text else None


# ── Optional coordinate reprojection ───────────────────────────────────────
# When a dataset is not in a projected metric CRS (e.g. Tokyo/PLATEAU is
# EPSG:6697 = geographic degrees, NYC is EPSG:2263 = US survey feet), every
# distance/area/proximity query and the OSM footprint fusion would be wrong.
# With --target-crs, coordinates are reprojected to a metric CRS AT INGEST, at
# the two coordinate primitives below (_parse_corner for all bbox/centroid math,
# _ring_coords/line posLists for geometry). The transformer is held per-thread.
# NOTE: this deliberately breaks byte-exact round-trip for reprojected cities
# (their pos_list is rewritten); an already-metric dataset stays lossless.
# See the CRS/units caveat in GRAPH.md.
_tls = threading.local()

# Serializes the few statements that MERGE *shared* singleton nodes (Dataset,
# City, District) plus schema setup across parallel tile parsers: a concurrent
# `MERGE (n {id})` without a uniqueness constraint can create duplicate nodes.
# Edge-only MERGEs between existing nodes don't need this (Neo4j node locks
# serialize them); they get transient-retry instead (_run_retry).
_SHARED_NODE_LOCK = threading.Lock()


def _run_retry(sess, query, retries=5, **params):
    """Run one statement, retrying transient lock conflicts (deadlocks between
    parallel tile finalizers whose edge batches lock shared endpoints such as the
    Dataset / BuildingFunction / District nodes in differing orders)."""
    from neo4j.exceptions import TransientError as _TE
    for attempt in range(retries):
        try:
            return sess.run(query, **params).consume()
        except _TE:
            time.sleep(0.2 * (attempt + 1))
    return sess.run(query, **params).consume()


def _epsg_from_srs(srs):
    """Resolve a CityGML srsName to an 'EPSG:NNNN' string (mirrors ingest_osm)."""
    if not srs:
        return None
    m = re.search(r"EPSG[:/]+(?:\d+/)?(\d{4,6})", srs)
    if m:
        return f"EPSG:{m.group(1)}"
    u = srs.upper().replace(" ", "")
    m = re.search(r"UTM(\d{2})", u)
    if m:
        return f"EPSG:258{m.group(1)}"
    if "CH1903+" in u or "LV95" in u:
        return "EPSG:2056"
    if "CH1903" in u or "LV03" in u:
        return "EPSG:21781"
    return srs


class _Reprojector:
    """Reproject CityGML coordinates from a source CRS to a target metric CRS.
    Emits (easting, northing[, height_m]) so downstream x/y are metric-planar.
    Handles GML axis order (geographic CRS store lat,lon) and scales Z by the
    source horizontal linear unit (feet->m) for projected non-metric sources
    (geographic Z is already a metric height and passes through)."""

    def __init__(self, src_srs, dst_crs):
        from pyproj import CRS, Transformer
        src = _epsg_from_srs(src_srs) or src_srs
        dst = _epsg_from_srs(dst_crs) or dst_crs
        self.dst_epsg = dst
        src_obj = CRS.from_user_input(src)
        self.geographic = src_obj.is_geographic
        self.z_factor = 1.0
        if not self.geographic:
            try:
                f = src_obj.axis_info[0].unit_conversion_factor
                if f:
                    self.z_factor = f
            except Exception:
                pass
        self.tf = Transformer.from_crs(src_obj, CRS.from_user_input(dst), always_xy=True)

    def point(self, a, b, z=0.0):
        # GML geographic order is (lat, lon); always_xy wants (lon, lat).
        x, y = self.tf.transform(b, a) if self.geographic else self.tf.transform(a, b)
        return x, y, z * self.z_factor

    def poslist(self, text, srs_dim):
        vals = text.split()
        if not vals:
            return text
        stride = srs_dim if srs_dim in (2, 3) else (3 if len(vals) % 3 == 0 else 2)
        out = []
        for i in range(0, len(vals) - stride + 1, stride):
            try:
                a = float(vals[i]); b = float(vals[i + 1])
                z = float(vals[i + 2]) if stride == 3 else 0.0
            except ValueError:
                return text  # non-numeric content: leave untouched
            x, y, zz = self.point(a, b, z)
            out.append(f"{x} {y} {zz}" if stride == 3 else f"{x} {y}")
        return " ".join(out)


def _reproject_poslist(text, srs_dim):
    xf = getattr(_tls, "reproj", None)
    return xf.poslist(text, srs_dim) if (xf is not None and text) else text


def _peek_srsname(path, limit=2_000_000):
    """Read the first srsName from a (possibly huge) CityGML file, bounded."""
    with open(path, "rb") as fh:
        chunk = fh.read(limit).decode("utf-8", "ignore")
    m = re.search(r'srsName="([^"]+)"', chunk)
    return m.group(1) if m else None


def _parse_corner(raw):
    parts = raw.strip().split()
    x = float(parts[0])
    y = float(parts[1])
    z = float(parts[2]) if len(parts) > 2 else 0.0
    xf = getattr(_tls, "reproj", None)
    if xf is not None:
        x, y, z = xf.point(x, y, z)
    return x, y, z


def _bbox_from_geometry(el):
    """
    Compute (lower_corner_text, upper_corner_text) by scanning every gml:posList
    and gml:pos coordinate under ``el``. Used as a fallback when a feature carries
    no per-feature gml:boundedBy/gml:Envelope (which is optional in CityGML 2.0).
    Returns (None, None) if no usable coordinates are found.
    """
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    found = False
    for coord_el in (*el.iter("{http://www.opengis.net/gml}posList"),
                     *el.iter("{http://www.opengis.net/gml}pos")):
        if not coord_el.text:
            continue
        try:
            dim = int(coord_el.get("srsDimension", 3))
        except (TypeError, ValueError):
            dim = 3
        if dim < 2:
            dim = 3
        nums = coord_el.text.split()
        for i in range(0, len(nums) - dim + 1, dim):
            try:
                x = float(nums[i]); y = float(nums[i + 1])
                z = float(nums[i + 2]) if dim > 2 else 0.0
            except ValueError:
                continue
            found = True
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            min_z, max_z = min(min_z, z), max(max_z, z)
    if not found:
        return None, None
    lc = f"{_fmt_num(min_x)} {_fmt_num(min_y)} {_fmt_num(min_z)}"
    uc = f"{_fmt_num(max_x)} {_fmt_num(max_y)} {_fmt_num(max_z)}"
    return lc, uc


def _fmt_num(v):
    """Render a float without trailing ``.0`` for whole numbers (compact corners)."""
    return str(int(v)) if v == int(v) else repr(v)


def _slug(s):
    return re.sub(r"[^a-z0-9_]", "_", s.lower())


def _attr_key(s):
    """
    Unicode-aware property key for generic (gen:*) attribute names. Unlike
    _slug (ASCII-only, used for ids), this preserves non-Latin letters/digits
    so that non-German/English schemas survive as distinct, queryable keys.
    Example: the Tokyo PLATEAU attribute names are Japanese —
    _slug('地区計画') == '____' (all attributes collide to underscores and are
    lost), whereas _attr_key('地区計画') == '地区計画'. Neo4j supports Unicode
    property keys (address them with backticks in Cypher). For pure-ASCII names
    this returns exactly what _slug did (e.g. 'PredictedRoofMaterial' ->
    'predictedroofmaterial'), so existing datasets are unaffected. The original
    name is always preserved verbatim in _gen_attrs_map for lossless export.
    """
    key = re.sub(r"[^\w]", "_", s.lower(), flags=re.UNICODE).strip("_")
    return key or "attr"


def _new_id_registry():
    """Per-ingest-run state for feature-id resolution (see _resolve_feature_id).
    ``seen``: raw gml:id -> {"c": {content_hash: id}, "g": {geom_hash: id}} for the
    content and geometry duplicate tiers. ``assigned``: every id handed out (raw +
    synthesized), so synthesized ids never collide. ``lock`` guards both dicts when
    one registry is shared across parallel tile parsers (--parse-workers > 1); the
    hash computation stays outside the lock, so contention is microseconds."""
    return {"seen": {}, "assigned": set(), "lock": threading.Lock()}


def _content_hash(el):
    """Compact hash of a feature's FULL content (geometry + attributes + nested
    structure). Serializing the whole element means the hash also proves non-
    geometric identity: two byte-identical occurrences (e.g. a Tokyo PLATEAU tile
    replicated verbatim across ward packages) hash the same and deduplicate."""
    return hashlib.sha1(etree.tostring(el)).digest()


def _geom_hash(el):
    """SHA1 over a feature's raw coordinate strings (gml:posList / gml:pos), in
    document order, or ``None`` when the feature carries no coordinates. This is the
    second-tier duplicate signal: two occurrences that share a gml:id and have
    coordinate-identical geometry but differ in ancillary attributes are the same
    physical building and must deduplicate, not split. The archetype is Tokyo
    PLATEAU, where a boundary building recurs in adjacent ward packages with
    identical geometry but ward-specific ``uro:*`` ADE data-quality codes: an exact
    content hash would over-split those into phantom buildings, whereas an identical
    geometry hash correctly merges them. Returning ``None`` for a geometry-less
    feature keeps two distinct coordinate-free features (e.g. Zurich placeholder
    ``ID_`` buildings with no geometry) from ever merging on an empty signature.
    Coordinate-identical is a deliberately strict criterion: a fuzzy footprint
    overlap would risk merging two genuinely distinct buildings that share a
    gml:id and happen to overlap (Helsinki's duplicates already share bounding
    boxes), which would silently break the lossless round-trip."""
    parts = [sub.text.strip() for sub in el.iter()
             if isinstance(sub.tag, str)
             and (sub.tag.endswith("}posList") or sub.tag.endswith("}pos"))
             and sub.text and sub.text.strip()]
    return hashlib.sha1("\n".join(parts).encode()).digest() if parts else None


def _resolve_feature_id(raw_id, content_hash, geom_hash, source):
    """Resolve the node id for a top-level feature (Building / BuildingPart /
    CityObject), distinguishing exact/geometric duplicates from distinct features
    that share a gml:id. Returns ``(id_to_use, original_gml_id_or_None, is_dup)``.

    ``MERGE (n {id: raw_gml_id})`` silently *collapses* two features that share a
    gml:id into one node. That is correct for a genuine duplicate (dedup) but loses
    a distinct feature. The collision is decided in three tiers, by content then by
    geometry, never by editing the source:

    * **Exact duplicate** (same gml:id, same *content* hash) -> return the already
      assigned id with ``is_dup=True``; the caller skips it (MERGE would only re-
      touch the same node). Byte-identical ward-replicated PLATEAU tiles hit this.
    * **Geometric duplicate** (same gml:id, different content but identical *geometry*
      hash) -> the same building re-shipped with differing ancillary attributes
      (Tokyo's ward-specific ADE metadata); merge onto the existing node
      (``is_dup=True``) rather than fabricate a phantom split.
    * **Distinct feature sharing an id** (same gml:id, different geometry, or geometry
      absent) -> synthesize a deterministic unique id ``<id>__<source-slug>__<n>``
      (split) and return the original gml:id so callers store it as
      ``source_gml_id`` (export re-emits it for a faithful round-trip). This is the
      Zurich ``ID_`` / Helsinki case; keeping distinct features distinct is what
      preserves the lossless round-trip.

    Complexity: O(1) amortized per feature. Thread-safety: the registry dicts hang
    off ``_tls`` (one pointer per parser thread); when the tiled path shares one
    registry across parallel tile parsers (--parse-workers > 1) every access runs
    under the registry lock (set as ``_tls.id_lock``), so cross-tile duplicates are
    still caught race-free. Hashes are computed by the caller, outside the lock.
    Ordering note: with parallel parsers, WHICH occurrence of a duplicate id is seen
    first (and thus keeps the raw id / donates the stored attributes of a
    geometry-identical dedup) follows thread scheduling rather than file order;
    content-identical replicas are unaffected.
    """
    lock = getattr(_tls, "id_lock", None)
    if lock is not None:
        with lock:
            return _resolve_feature_id_locked(raw_id, content_hash, geom_hash, source)
    return _resolve_feature_id_locked(raw_id, content_hash, geom_hash, source)


def _resolve_feature_id_locked(raw_id, content_hash, geom_hash, source):
    seen = getattr(_tls, "seen_ids", None)
    if seen is None:
        seen = _tls.seen_ids = {}
        _tls.assigned_ids = set()
    assigned = _tls.assigned_ids
    bucket = seen.get(raw_id)
    if bucket is None:
        seen[raw_id] = {"c": {content_hash: raw_id},
                        "g": ({geom_hash: raw_id} if geom_hash is not None else {})}
        assigned.add(raw_id)
        return raw_id, None, False
    hit = bucket["c"].get(content_hash)                 # tier 1: exact content
    if hit is not None:
        return hit, None, True
    if geom_hash is not None:                           # tier 2: identical geometry
        ghit = bucket["g"].get(geom_hash)
        if ghit is not None:
            bucket["c"][content_hash] = ghit           # cache for future occurrences
            return ghit, None, True
    n = 1                                               # tier 3: distinct -> split
    while True:
        candidate = f"{raw_id}__{_slug(source)}__{n}"
        if candidate not in assigned:
            bucket["c"][content_hash] = candidate
            if geom_hash is not None:
                bucket["g"][geom_hash] = candidate
            assigned.add(candidate)
            return candidate, raw_id, False
        n += 1


def _lod_level(root):
    """Detect highest LOD from a Building element."""
    for lod in ("4", "3", "2", "1", "0"):
        for geom_type in ("Solid", "MultiSurface"):
            tag = (
                f"{{http://www.opengis.net/citygml/building/2.0}}"
                f"lod{lod}{geom_type}"
            )
            if root.find(f".//{tag}") is not None:
                return f"LOD{lod}"
    return "LOD0"


def _lod_level_generic(root, ns_prefix):
    """Detect highest LOD from any module feature element."""
    ns_uri = NS.get(ns_prefix, "")
    for lod in ("4", "3", "2", "1", "0"):
        for suffix in ("Solid", "MultiSurface", "Geometry", "ImplicitRepresentation"):
            tag = f"{{{ns_uri}}}lod{lod}{suffix}"
            if root.find(f".//{tag}") is not None:
                return f"LOD{lod}"
    return "LOD0"


def _format_version(raw_xml_bytes):
    m = GML_ROOT_NS_RE.search(
        raw_xml_bytes[:2000].decode("utf-8", errors="ignore")
    )
    if m:
        ver_m = re.search(r"citygml/([\d.]+)", m.group(1))
        if ver_m:
            return f"CityGML {ver_m.group(1)}"
    return "CityGML"


def _read_citymodel_header(gml_path):
    """Read root CityModel-level metadata (gml:name, gml:description,
    xsi:schemaLocation) for lossless re-export. Streams only the first few
    elements, so it is cheap even for large files."""
    XSI = "http://www.w3.org/2001/XMLSchema-instance"
    name = desc = schema_loc = None
    context = etree.iterparse(str(gml_path), events=("start", "end"))
    root = None
    for event, el in context:
        ln = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if event == "start" and root is None:
            root = el
            schema_loc = el.get(f"{{{XSI}}}schemaLocation")
        elif event == "end" and root is not None and el.getparent() is root:
            if ln == "name" and name is None and el.text:
                name = el.text.strip()
            elif ln == "description" and desc is None and el.text:
                desc = el.text.strip()
            elif ln in ("cityObjectMember", "featureMember", "appearanceMember"):
                break  # past the header; stop streaming
    del context
    return name, desc, schema_loc


# Structural / identity node property keys that a generic (gen:*) attribute must
# never overwrite. A gen:*Attribute name is slugged with _attr_key and used as the
# storage key; without this guard a real dataset attribute such as Helsinki's
# gen:stringAttribute name="ID" slugs to "id" and, when merged in _collect_*,
# silently clobbers the gml:id-based node id (which also detaches the feature's
# geometry, since child edges still point at the correct id). Colliding keys are
# remapped to a namespaced "gen_<key>"; the original name is preserved in the
# gen_map so export re-emits the attribute unchanged.
RESERVED_NODE_KEYS = frozenset({
    # identity / provenance / structure
    "id", "name", "source", "source_gml_id", "id_synthesized", "feature_type",
    "description", "_gen_attrs_map", "gtype", "predicate", "composite_surface_id",
    # bbox / spatial anchor
    "bbox_lower_corner", "bbox_upper_corner", "bbox_wkt", "has_bbox_envelope",
    "bbox_min_x", "bbox_min_y", "bbox_min_z", "bbox_max_x", "bbox_max_y", "bbox_max_z",
    "center_x", "center_y", "ground_z", "location",
    # lossless common-extras subtrees
    "termination_date", "metadata_xml", "location_xml", "generalizes_to_xml",
    "external_reference_xml", "ade_xml", "address_xml",
    # typed AbstractBuilding / feature scalars
    "measured_height", "storeys_above_ground", "storeys_below_ground",
    "storey_heights_above_ground", "storey_heights_below_ground",
    "roof_type_code", "function_code", "class_code", "usage_code",
    "creation_date", "year_of_construction", "relative_to_terrain",
    "external_information_system", "external_object_name",
    "gml_name", "gml_description",
})


_GEN_ATTR_TYPES = (
    ("gen:stringAttribute",  "string"),
    ("gen:intAttribute",     "int"),
    ("gen:doubleAttribute",  "double"),
    ("gen:dateAttribute",    "date"),
    ("gen:uriAttribute",     "uri"),
    ("gen:measureAttribute", "measure"),
)


def _extract_gen_attrs(el):
    """Collect all gen:*Attribute values of a feature, including those nested in
    gen:genericAttributeSet wrappers (recursively — e.g. Helsinki wraps its data-
    integration attributes in one set per building; dropping them was a lossless
    round-trip violation). Values are stored FLAT on the node; set membership is
    recorded in gen_map so export can rebuild the wrapper elements.

    gen_map[key] = {"name": original-name,
                    "type": "string|int|double|date|uri|measure"
                    [, "set": [{"name": set-name, "i": sibling-index
                                [, "codeSpace": …]}, …]]}   # root→leaf wrapper path
    The per-parent sibling index keeps two same-named sibling sets distinct on
    re-emit. A duplicate attribute name within one feature gets a numeric key
    suffix so both values survive (each gen_map entry re-emits with the original
    name). Keys colliding with RESERVED_NODE_KEYS are namespaced to gen_<key> so a
    source attribute like Helsinki's name="ID" can never overwrite the node id.
    """
    props   = {}
    gen_map = {}

    def _add(attr_el, gtype, set_path):
        name   = attr_el.get("name", "").strip()
        val_el = attr_el.find("gen:value", NS)
        # an EMPTY <gen:value/> is still a real attribute (Helsinki has 291, e.g.
        # blank REPAIRED flags) — store "" so the element round-trips; only skip
        # when the value element itself is absent.
        if not (name and val_el is not None):
            return
        key = _attr_key(name)
        if key in RESERVED_NODE_KEYS:
            # would clobber a structural node property: namespace it so the
            # value survives without corrupting id/name/source/bbox/… .
            base, n = f"gen_{key}", 2
            key = base
            while key in RESERVED_NODE_KEYS or key in props:
                key = f"{base}_{n}"
                n += 1
        elif key in props:
            # same attribute name occurs more than once on this feature (e.g. a
            # direct attribute and a set member): keep both under distinct keys.
            base, n = key, 2
            while key in props:
                key = f"{base}_{n}"
                n += 1
        props[key]   = (val_el.text or "").strip()
        meta = {"name": name, "type": gtype}
        if set_path:
            meta["set"] = set_path
        gen_map[key] = meta
        uom = val_el.get("uom", "") if gtype == "measure" else ""
        if uom:
            props[f"{key}_uom"] = uom

    def _walk(container, set_path):
        for tag, gtype in _GEN_ATTR_TYPES:
            for attr_el in container.findall(tag, NS):
                _add(attr_el, gtype, set_path)
        for i, set_el in enumerate(container.findall("gen:genericAttributeSet", NS)):
            entry = {"name": set_el.get("name", "").strip(), "i": i}
            cs = set_el.get("codeSpace")
            if cs:
                entry["codeSpace"] = cs
            _walk(set_el, set_path + [entry])

    _walk(el, [])
    return props, gen_map


def _resolve_municipality(gemeinde_key, filename_stem):
    if gemeinde_key:
        if gemeinde_key in GEMEINDE_MAP:
            return GEMEINDE_MAP[gemeinde_key]
        for prefix_len in (8, 5):
            prefix = gemeinde_key[:prefix_len]
            if prefix in GEMEINDE_MAP:
                return GEMEINDE_MAP[prefix]
    return (filename_stem, filename_stem)


# ---------------------------------------------------------------------------
# Spatial R-tree (neo4j-spatial plugin)
# ---------------------------------------------------------------------------

def _ensure_spatial_layer(session, layer_name="features"):
    try:
        existing = {
            r["name"]
            for r in session.run(
                "CALL spatial.layers() YIELD name RETURN name"
            )
        }
        if layer_name not in existing:
            session.run(
                "CALL spatial.addWKTLayer($layer, 'bbox_wkt')",
                layer=layer_name,
            )
            print(f"  Spatial layer '{layer_name}' created.")
        else:
            print(f"  Spatial layer '{layer_name}' already exists.")
        return True
    except Exception as exc:
        print(f"  WARNING: spatial layer setup failed ({exc})")
        return False


def _derive_code_hubs(sess, source_filter=None):
    """Derive the BuildingFunction / RoofType vocabulary HUB nodes and their
    HAS_FUNCTION / HAS_ROOF_TYPE edges from the scalar codes already stored on
    the Building nodes (`function_code` / `roof_type_code` — the authoritative,
    lossless round-trip representation).

    Hubs are a handful of shared nodes per city (e.g. a few roof-form classes
    for 388k Hamburg buildings), so they are never written from the parallel
    batch hot path (a concurrent MERGE on the same hub id can duplicate it, and
    every edge-create serializes on the hub's write lock). This derivation runs
    either once per file (``source_filter=<filename>``, in the single post-pass
    thread of that ingest) or once per tiled Dataset (``source_filter=None``,
    one global single-threaded pass after all tiles). Hub node content is
    identical to the historical model: {id: fn_/rt_<slug(code)>, name, code}
    plus the Entity label. Edge rows are sorted by code so any concurrent
    per-file passes acquire hub locks in a consistent order."""
    for codeprop, label, prefix, names, rel in (
        ("function_code",  "BuildingFunction", "fn", BUILDING_FUNCTION_NAMES, "HAS_FUNCTION"),
        ("roof_type_code", "RoofType",         "rt", ROOF_TYPE_NAMES,         "HAS_ROOF_TYPE"),
    ):
        where_src = "b.source = $src AND " if source_filter else ""
        codes = [r["c"] for r in sess.run(
            f"MATCH (b:Building) WHERE {where_src}b.{codeprop} IS NOT NULL "
            f"RETURN DISTINCT b.{codeprop} AS c", src=source_filter)]
        if not codes:
            continue
        with _SHARED_NODE_LOCK:
            for c in sorted(codes):
                _run_retry(sess,
                    f"MERGE (n:{label} {{id: $id}}) "
                    f"SET n.name = $name, n.code = $code, n:Entity",
                    id=f"{prefix}_{_slug(c)}", name=names.get(c, c), code=c)
        rows = sess.run(
            f"MATCH (b:Building) WHERE {where_src}b.{codeprop} IS NOT NULL "
            f"AND NOT (b)-[:{rel}]->() RETURN b.id AS id, b.{codeprop} AS code",
            src=source_filter).data()
        rows.sort(key=lambda r: (r["code"], r["id"]))
        for i in range(0, len(rows), 1000):
            _run_retry(sess,
                f"UNWIND $rows AS row MATCH (b:Building {{id: row.id}}) "
                f"MATCH (f:{label} {{code: row.code}}) MERGE (b)-[:{rel}]->(f)",
                rows=rows[i:i + 1000])


def _index_features_spatial(session, layer_name="features", batch_size=25000):
    """
    Index all top-level feature bounding-box polygons in the neo4j-spatial R-tree.
    Covers both Building and CityObject nodes that carry a bbox_wkt property.
    R-tree insertions MUST be serial — concurrent writes corrupt the tree during
    rebalancing.  This is slow (~1–3 ms/node) but the resulting index supports
    spatial.intersects / bbox / withinDistance queries on full bbox polygons.

    Inserts in serial id-indexed batches (never one `collect(n)` over the whole
    label): a single addNodes over ~1M nodes materializes every node + geometry
    in one transaction and trips `dbms.memory.transaction.total.max` (~70 % of
    heap) on city-scale graphs (NYC 1.08M buildings). Batches keep each
    transaction small; addNodes is idempotent and the batches run sequentially on
    the one session, so the tree stays serially built and correct.
    """
    try:
        print("  Indexing all features in R-tree …")
        # Collect ids first (cheap: no geometry materialized), then re-match each
        # batch through the Entity(id) index — a single scan, index-backed lookups.
        ids = [r["id"] for r in session.run(
            "MATCH (n) WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NOT NULL "
            "RETURN n.id AS id"
        )]
        total = 0
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            result = session.run(
                "UNWIND $ids AS nid MATCH (n:Entity {id: nid}) "
                "WITH collect(n) AS nodes "
                "CALL spatial.addNodes($layer, nodes) YIELD count RETURN count AS n",
                ids=batch, layer=layer_name,
            ).single()
            total += result["n"] if result else 0
        return total
    except Exception as exc:
        print(f"  WARNING: spatial indexing failed ({exc})")
        return 0


def _backfill_cityobject_bbox_from_geometry(session, batch_size=2000):
    """
    Compute bbox_wkt for CityObject nodes that lack one, from their already-stored
    graph geometry (reprojected in place when --target-crs was used), so the
    non-Building modules (roads, bridges, city furniture, vegetation, water) enter
    the spatial R-tree too.

    `_collect_city_object` derives a CityObject bbox only from a per-feature
    `gml:boundedBy/gml:Envelope`; sources that omit it (e.g. Tokyo/PLATEAU
    tran/brid/frn/veg/wtr, which ship geometry but no per-feature envelope) would
    otherwise leave those features with no bbox and thus outside the R-tree. Reads
    coordinates from the graph so it is CRS-correct regardless of reprojection.
    pos_list convention: "x y z …" (x=idx0, y=idx1, stride 3); only non-degenerate
    boxes are written. Serial, idempotent (SET only), safe to re-run.
    """
    def _bbox(pos_lists):
        minx = miny = minz = float("inf")
        maxx = maxy = maxz = float("-inf")
        ok = False
        for pl in pos_lists:
            v = pl.split()
            if len(v) < 3 or len(v) % 3 != 0:
                continue
            f = [float(t) for t in v]
            xs, ys, zs = f[0::3], f[1::3], f[2::3]
            minx, maxx = min(minx, min(xs)), max(maxx, max(xs))
            miny, maxy = min(miny, min(ys)), max(maxy, max(ys))
            minz, maxz = min(minz, min(zs)), max(maxz, max(zs))
            ok = True
        if not ok or not (maxx > minx and maxy > miny):
            return None
        return minx, miny, minz, maxx, maxy, maxz

    try:
        ids = [r["id"] for r in session.run(
            "MATCH (c:CityObject) WHERE c.bbox_wkt IS NULL RETURN c.id AS id")]
        if not ids:
            return 0
        print(f"  Backfilling bbox for {len(ids)} CityObject(s) from graph geometry …")
        written = 0
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            rows = session.run(
                "UNWIND $ids AS cid MATCH (c:Entity {id: cid}) "
                "OPTIONAL MATCH (c)-[*1..6]->(g) "
                "WHERE (g:GeometryRing OR g:GeometryLineString) AND g.pos_list IS NOT NULL "
                "RETURN cid AS id, collect(g.pos_list) AS pl", ids=batch).data()
            writes = []
            for r in rows:
                if not r["pl"]:
                    continue
                bb = _bbox(r["pl"])
                if bb is None:
                    continue
                minx, miny, minz, maxx, maxy, maxz = bb
                writes.append({
                    "id": r["id"],
                    "wkt": (f"POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, "
                            f"{minx} {maxy}, {minx} {miny}))"),
                    "minx": minx, "miny": miny, "minz": minz,
                    "maxx": maxx, "maxy": maxy, "maxz": maxz,
                    "cx": (minx + maxx) / 2.0, "cy": (miny + maxy) / 2.0})
            if writes:
                session.run(
                    "UNWIND $rows AS row MATCH (n:Entity {id: row.id}) "
                    "SET n.bbox_wkt=row.wkt, n.bbox_min_x=row.minx, n.bbox_min_y=row.miny, "
                    "n.bbox_min_z=row.minz, n.bbox_max_x=row.maxx, n.bbox_max_y=row.maxy, "
                    "n.bbox_max_z=row.maxz, n.center_x=row.cx, n.center_y=row.cy",
                    rows=writes)
                written += len(writes)
        return written
    except Exception as exc:
        print(f"  WARNING: CityObject bbox backfill failed ({exc})")
        return 0


def _create_proximity_edges_native(session, radius_m=100.0, batch_size=5000):
    """
    Proximity edges via Python KD-tree (scipy) + batched UNWIND writes.
    O(N log N) construction + O(N*k) lookup vs the prior O(N²) Cypher scan.
    Coordinates must be in a projected CRS (metres) stored as center_x/center_y.
    """
    try:
        import numpy as np
        from scipy.spatial import cKDTree
    except ImportError:
        print("  WARNING: scipy not installed — skipping proximity edges. Run: pip install scipy")
        return 0

    try:
        print("  Fetching building locations …")
        rows = session.run(
            "MATCH (b:Building) WHERE b.center_x IS NOT NULL AND b.center_y IS NOT NULL "
            "RETURN b.id AS id, b.center_x AS x, b.center_y AS y"
        ).data()
        if not rows:
            print("  No buildings with coordinates found.")
            return 0

        ids    = [r["id"] for r in rows]
        coords = np.array([[r["x"], r["y"]] for r in rows], dtype=np.float64)
        print(f"  Building KD-tree for {len(ids):,} buildings …")

        tree     = cKDTree(coords)
        pair_arr = tree.query_pairs(radius_m, output_type="ndarray")
        if len(pair_arr) == 0:
            print("  No nearby pairs found.")
            return 0

        print(f"  Found {len(pair_arr):,} nearby pairs — inserting edges …")

        dists = np.linalg.norm(coords[pair_arr[:, 0]] - coords[pair_arr[:, 1]], axis=1)
        edges = [
            {"a": ids[int(i)], "b": ids[int(j)], "d": round(float(dist), 1)}
            for (i, j), dist in zip(pair_arr, dists)
        ]

        total = len(edges)
        for start in range(0, total, batch_size):
            chunk = edges[start : start + batch_size]
            session.run(
                """
                UNWIND $edges AS e
                MATCH (a:Building {id: e.a})
                MATCH (b:Building {id: e.b})
                MERGE (a)-[r:NEARBY_BUILDING]->(b)
                SET r.distance_m = e.d
                """,
                edges=chunk,
            )
            done = min(start + batch_size, total)
            if done % 50000 < batch_size or done == total:
                print(f"    {done:,}/{total:,} edges inserted")

        print(f"  NEARBY_BUILDING edges created: {total:,}")
        return total
    except Exception as exc:
        print(f"  WARNING: could not create proximity edges ({exc})")
        return 0


# ---------------------------------------------------------------------------
# Batch accumulator
# ---------------------------------------------------------------------------

def _new_batch():
    return {
        # Building-specific
        "buildings":          [],
        # (BuildingFunction/RoofType hubs + HAS_FUNCTION/HAS_ROOF_TYPE edges are
        #  derived post-parse from the Building scalars — see _derive_code_hubs —
        #  never from batches, so parallel writers can't race on the hub nodes.)
        "solid_rels":         [],   # (Building|Room) → GeometrySolid {generic id match}
        "boundary_rels":      [],   # (Building|Room|Installation) → BoundarySurface {order}
        "bldg_terrain_rels":  [],
        "bldg_surface_rels":  [],   # Building → GeometryMultiSurface (footprint/roofedge/lodN MS)
        # LoD4 interior thematic features
        "rooms":              [],   # Room nodes
        "installations":      [],   # (Int)BuildingInstallation nodes
        "furniture":          [],   # BuildingFurniture nodes
        "interior_room_rels": [],   # Building → Room {order}
        "room_inst_rels":     [],   # Room → BuildingInstallation {order}
        "room_furn_rels":     [],   # Room → BuildingFurniture {order}
        "bldg_inst_rels":     [],   # Building → BuildingInstallation (outer) {order}
        "bldg_int_inst_rels": [],   # Building → BuildingInstallation (interior) {order}
        "building_parts":     [],   # BuildingPart nodes (recursive AbstractBuilding)
        "bldg_part_rels":     [],   # Building|BuildingPart → BuildingPart {order}
        "bldg_multicurve_rels": [], # Building|BuildingPart → GeometryLineString {lod, order}
        "feature_geom_rels":  [],   # feature → GeometryMultiSurface (lod*Geometry) {lod, role}
        # Generic city objects (all non-building top-level features)
        "city_objects":       [],
        "co_solid_rels":      [],   # CityObject → GeometrySolid
        "co_surface_rels":    [],   # CityObject → GeometryMultiSurface
        "co_boundary_rels":   [],   # CityObject → BoundarySurface
        "group_member_rels":  [],   # CityObjectGroup → member feature (xlink) {order, role}
        "nested_features":    [],   # Bridge/Tunnel installations & construction elements
        "nested_rels":        [],   # parent CityObject → NestedFeature {prop, order}
        # Shared geometry nodes
        "solids":             [],
        "multi_surfaces":     [],
        "composite_surfaces": [],   # GeometryCompositeSurface (XLink-referenced gml:CompositeSurface)
        "surfaces":           [],
        "openings":           [],   # Door / Window
        "surf_opening_rels":  [],   # BoundarySurface → Opening {order}
        "opening_ms_rels":    [],   # Opening → GeometryMultiSurface {lod}
        "polygons":           [],
        "rings":              [],
        "terrain_ints":       [],
        "linestrings":        [],
        # Geometry membership relationships. XLink references are marked
        # via_xlink=True (and orientation for gml:OrientableSurface) so the exact
        # xlink:href can be reconstructed on export.
        "surface_member_rels":[],   # GeometrySolid → (GeometryPolygon|GeometryCompositeSurface) {order, via_xlink, orientation} (generic id match)
        "composite_poly_rels":[],   # GeometryCompositeSurface → GeometryPolygon {order}
        "ms_poly_rels":       [],   # GeometryMultiSurface → GeometryPolygon {order, via_xlink, orientation}
        "surf_poly_rels":     [],   # BoundarySurface → GeometryPolygon {order, via_xlink, orientation}
        "poly_ext_ring_rels": [],
        "poly_int_ring_rels": [],
        "terrain_line_rels":  [],
    }


def _flush_batch(sess, batch):
    has_buildings = bool(batch["buildings"])
    has_objects   = bool(batch["city_objects"])
    if not has_buildings and not has_objects:
        return

    # ── Building nodes ─────────────────────────────────────────────────────
    if has_buildings:
        sess.run(
            "UNWIND $rows AS row MERGE (n:Building {id: row.id}) "
            "SET n += row, n:Entity, n.location = point({x: row.center_x, y: row.center_y})",
            rows=batch["buildings"],
        )
    if batch["building_parts"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:BuildingPart {id: row.id}) SET n += row, n:Entity",
            rows=batch["building_parts"],
        )
    # NOTE: BuildingFunction / RoofType hub nodes are no longer written here —
    # see _collect_building: they are derived post-parse from the Building
    # scalars, so parallel writers never race on the shared vocabulary nodes.

    # ── Generic CityObject nodes (with APOC dynamic label) ─────────────────
    if has_objects:
        try:
            sess.run(
                "UNWIND $rows AS row "
                "MERGE (n:CityObject {id: row.id}) SET n += row, n:Entity "
                "WITH n, row CALL apoc.create.addLabels(n, [row.feature_type]) YIELD node "
                "WITH node WHERE node.center_x IS NOT NULL "
                "SET node.location = point({x: node.center_x, y: node.center_y})",
                rows=batch["city_objects"],
            )
        except Exception:
            # Fallback without APOC: just CityObject label
            sess.run(
                "UNWIND $rows AS row MERGE (n:CityObject {id: row.id}) SET n += row, n:Entity "
                "WITH n WHERE n.center_x IS NOT NULL "
                "SET n.location = point({x: n.center_x, y: n.center_y})",
                rows=batch["city_objects"],
            )

    # ── Shared geometry nodes ───────────────────────────────────────────────
    if batch["solids"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:GeometrySolid {id: row.id}) SET n += row, n:Entity",
            rows=batch["solids"],
        )
    if batch["multi_surfaces"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:GeometryMultiSurface {id: row.id}) SET n += row, n:Entity",
            rows=batch["multi_surfaces"],
        )
    if batch["composite_surfaces"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:GeometryCompositeSurface {id: row.id}) SET n += row, n:Entity",
            rows=batch["composite_surfaces"],
        )
    if batch["surfaces"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:BoundarySurface {id: row.id}) SET n += row, n:Entity",
            rows=batch["surfaces"],
        )
    if batch["openings"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:Opening {id: row.id}) SET n += row, n:Entity",
            rows=batch["openings"],
        )
    # LoD4 interior feature nodes (created before any relationship flush so the
    # generic id-matched edges below resolve).
    if batch["rooms"]:
        sess.run("UNWIND $rows AS row MERGE (n:Room {id: row.id}) SET n += row, n:Entity",
                 rows=batch["rooms"])
    if batch["installations"]:
        sess.run("UNWIND $rows AS row MERGE (n:BuildingInstallation {id: row.id}) SET n += row, n:Entity",
                 rows=batch["installations"])
    if batch["furniture"]:
        sess.run("UNWIND $rows AS row MERGE (n:BuildingFurniture {id: row.id}) SET n += row, n:Entity",
                 rows=batch["furniture"])
    if batch["nested_features"]:
        sess.run("UNWIND $rows AS row MERGE (n:NestedFeature {id: row.id}) SET n += row, n:Entity",
                 rows=batch["nested_features"])
    if batch["polygons"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:GeometryPolygon {id: row.id}) SET n.source = row.source, n:Entity",
            rows=batch["polygons"],
        )
    if batch["rings"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:GeometryRing {id: row.id}) SET n += row",
            rows=batch["rings"],
        )
    if batch["terrain_ints"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:TerrainIntersection {id: row.id}) SET n += row",
            rows=batch["terrain_ints"],
        )
    if batch["linestrings"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:GeometryLineString {id: row.id}) SET n += row",
            rows=batch["linestrings"],
        )

    # ── Building relationships ──────────────────────────────────────────────
    # HAS_FUNCTION and HAS_ROOF_TYPE are done in a serial post-pass (deadlock avoidance).
    if batch["solid_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Entity {id: row.src}), (b:GeometrySolid {id: row.tgt}) "
            "MERGE (a)-[:HAS_LOD_SOLID]->(b)",
            rows=batch["solid_rels"],
        )
    if batch["boundary_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Entity {id: row.src}), (b:BoundarySurface {id: row.tgt}) "
            "MERGE (a)-[r:HAS_BOUNDARY]->(b) SET r.order = row.order",
            rows=batch["boundary_rels"],
        )
    # LoD4 interior relationships (feature nodes already created above)
    if batch["interior_room_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}), (b:Room {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_INTERIOR_ROOM]->(b) SET r.order = row.order",
                 rows=batch["interior_room_rels"])
    if batch["room_inst_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Room {id: row.src}), (b:BuildingInstallation {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_ROOM_INSTALLATION]->(b) SET r.order = row.order",
                 rows=batch["room_inst_rels"])
    if batch["room_furn_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Room {id: row.src}), (b:BuildingFurniture {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_INTERIOR_FURNITURE]->(b) SET r.order = row.order",
                 rows=batch["room_furn_rels"])
    if batch["bldg_inst_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}), (b:BuildingInstallation {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_OUTER_INSTALLATION]->(b) SET r.order = row.order",
                 rows=batch["bldg_inst_rels"])
    if batch["bldg_int_inst_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}), (b:BuildingInstallation {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_INTERIOR_INSTALLATION]->(b) SET r.order = row.order",
                 rows=batch["bldg_int_inst_rels"])
    if batch["bldg_part_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}), (b:BuildingPart {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_BUILDING_PART]->(b) SET r.order = row.order",
                 rows=batch["bldg_part_rels"])
    if batch["bldg_multicurve_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}), (b:GeometryLineString {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_LOD_MULTICURVE]->(b) SET r.lod = row.lod, r.order = row.order",
                 rows=batch["bldg_multicurve_rels"])
    if batch["feature_geom_rels"]:
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}), (b:GeometryMultiSurface {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_LOD_GEOMETRY]->(b) SET r.lod = row.lod, r.role = row.role",
                 rows=batch["feature_geom_rels"])
    if batch["bldg_terrain_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Entity {id: row.src}), (b:TerrainIntersection {id: row.tgt}) "
            "MERGE (a)-[:HAS_TERRAIN_INTERSECTION]->(b)",
            rows=batch["bldg_terrain_rels"],
        )
    if batch["bldg_surface_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Entity {id: row.src}), (b:GeometryMultiSurface {id: row.tgt}) "
            "MERGE (a)-[r:HAS_LOD_SURFACE]->(b) SET r.lod = row.lod, r.role = row.role",
            rows=batch["bldg_surface_rels"],
        )

    # ── CityObject relationships ────────────────────────────────────────────
    if batch["co_solid_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:CityObject {id: row.src}), (b:GeometrySolid {id: row.tgt}) "
            "MERGE (a)-[:HAS_LOD_SOLID]->(b)",
            rows=batch["co_solid_rels"],
        )
    if batch["co_surface_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:CityObject {id: row.src}), (b:GeometryMultiSurface {id: row.tgt}) "
            "MERGE (a)-[r:HAS_LOD_SURFACE]->(b) SET r.lod = row.lod",
            rows=batch["co_surface_rels"],
        )
    if batch["co_boundary_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:CityObject {id: row.src}), (b:BoundarySurface {id: row.tgt}) "
            "MERGE (a)-[r:HAS_BOUNDARY]->(b) SET r.order = row.order",
            rows=batch["co_boundary_rels"],
        )
    if batch["nested_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Entity {id: row.src}), (b:NestedFeature {id: row.tgt}) "
            "MERGE (a)-[r:HAS_NESTED {prop: row.prop, order: row.order}]->(b)",
            rows=batch["nested_rels"],
        )
    if batch["group_member_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:CityObject {id: row.src}), (b:Entity {id: row.tgt}) "
            "MERGE (a)-[r:HAS_GROUP_MEMBER {order: row.order}]->(b) "
            "SET r.via_xlink = true, r.role = row.role",
            rows=batch["group_member_rels"],
        )

    # ── Shared geometry relationships ───────────────────────────────────────
    # Solid members may target a polygon or a (shared) composite surface, and may
    # be xlink references — generic id match, with via_xlink / orientation kept.
    if batch["surface_member_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Entity {id: row.src}), (b:Entity {id: row.tgt}) "
            "MERGE (a)-[r:HAS_SURFACE_MEMBER {order: row.order}]->(b) "
            "SET r.via_xlink = row.via_xlink, r.orientation = row.orientation",
            rows=batch["surface_member_rels"],
        )
    if batch["composite_poly_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:GeometryCompositeSurface {id: row.src}), (b:GeometryPolygon {id: row.tgt}) "
            "MERGE (a)-[r:HAS_SURFACE_MEMBER {order: row.order}]->(b)",
            rows=batch["composite_poly_rels"],
        )
    if batch["ms_poly_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:GeometryMultiSurface {id: row.src}), (b:GeometryPolygon {id: row.tgt}) "
            "MERGE (a)-[r:HAS_SURFACE_MEMBER {order: row.order}]->(b) "
            "SET r.via_xlink = row.via_xlink, r.orientation = row.orientation",
            rows=batch["ms_poly_rels"],
        )
    if batch["surf_poly_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:BoundarySurface {id: row.src}), (b:GeometryPolygon {id: row.tgt}) "
            "MERGE (a)-[r:HAS_POLYGON {order: row.order}]->(b) "
            "SET r.via_xlink = row.via_xlink, r.orientation = row.orientation",
            rows=batch["surf_poly_rels"],
        )
    if batch["surf_opening_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:BoundarySurface {id: row.src}), (b:Opening {id: row.tgt}) "
            "MERGE (a)-[r:HAS_OPENING]->(b) SET r.order = row.order",
            rows=batch["surf_opening_rels"],
        )
    if batch["opening_ms_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:Opening {id: row.src}), (b:GeometryMultiSurface {id: row.tgt}) "
            "MERGE (a)-[r:HAS_LOD_SURFACE]->(b) SET r.lod = row.lod",
            rows=batch["opening_ms_rels"],
        )
    if batch["poly_ext_ring_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:GeometryPolygon {id: row.src}), (b:GeometryRing {id: row.tgt}) "
            "MERGE (a)-[:HAS_EXTERIOR_RING]->(b)",
            rows=batch["poly_ext_ring_rels"],
        )
    if batch["poly_int_ring_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:GeometryPolygon {id: row.src}), (b:GeometryRing {id: row.tgt}) "
            "MERGE (a)-[r:HAS_INTERIOR_RING]->(b) SET r.ring_index = row.ring_index",
            rows=batch["poly_int_ring_rels"],
        )
    if batch["terrain_line_rels"]:
        sess.run(
            "UNWIND $rows AS row "
            "MATCH (a:TerrainIntersection {id: row.src}), (b:GeometryLineString {id: row.tgt}) "
            "MERGE (a)-[r:HAS_LINE]->(b) SET r.order = row.order",
            rows=batch["terrain_line_rels"],
        )


# ---------------------------------------------------------------------------
# Element-level data collection (pure Python, no DB calls)
# ---------------------------------------------------------------------------

def _ring_coords(ring_el):
    """
    Return (pos_list, srs_dimension) for a gml:LinearRing, supporting both
    coordinate encodings: a single gml:posList, or a sequence of gml:pos
    elements (as emitted by IFC→CityGML converters, e.g. FZK-Haus LoD2–4).
    gml:pos values are concatenated into the canonical posList form; coordinate
    values are preserved verbatim (never rounded or reordered).
    """
    pos_el = ring_el.find("gml:posList", NS)
    if pos_el is not None and pos_el.text:
        srs_dim = int(pos_el.get("srsDimension", "3"))
        return _reproject_poslist(pos_el.text.strip(), srs_dim), srs_dim

    pos_els = ring_el.findall("gml:pos", NS)
    if pos_els:
        srs_dim = int(pos_els[0].get("srsDimension", "3"))
        coords = " ".join(p.text.strip() for p in pos_els if p.text and p.text.strip())
        return _reproject_poslist(coords, srs_dim), srs_dim

    return "", 3


def _collect_polygon(poly_el, source, batch):
    """Collect GeometryPolygon + GeometryRing data into batch. Returns poly_id."""
    poly_id = poly_el.get(GML_ID) or f"poly_{uuid.uuid4().hex[:16]}"
    batch["polygons"].append({"id": poly_id, "source": source})

    ring_el = poly_el.find("gml:exterior/gml:LinearRing", NS)
    if ring_el is not None:
        pos_list, srs_dim = _ring_coords(ring_el)
        ring_id  = ring_el.get(GML_ID) or f"ring_ext_{poly_id}"
        batch["rings"].append({
            "id": ring_id, "pos_list": pos_list, "srs_dimension": srs_dim,
            "ring_type": "exterior", "source": source,
        })
        batch["poly_ext_ring_rels"].append({"src": poly_id, "tgt": ring_id})

    for int_idx, int_ring_el in enumerate(
        poly_el.findall("gml:interior/gml:LinearRing", NS)
    ):
        pos_list, srs_dim = _ring_coords(int_ring_el)
        ring_id  = int_ring_el.get(GML_ID) or f"ring_int_{poly_id}_{int_idx}"
        batch["rings"].append({
            "id": ring_id, "pos_list": pos_list, "srs_dimension": srs_dim,
            "ring_type": "interior", "source": source,
        })
        batch["poly_int_ring_rels"].append(
            {"src": poly_id, "tgt": ring_id, "ring_index": int_idx}
        )

    return poly_id


def _record_member(sm_el, container_id, order, source, batch, rel_key, orientation=None):
    """Resolve one surfaceMember (or baseSurface) and append a membership edge to
    batch[rel_key]. XLink references (xlink:href) are marked via_xlink=True so the
    href can be re-emitted on export; gml:OrientableSurface wrappers record their
    orientation on the edge; inline gml:Polygon / gml:CompositeSurface members are
    materialized as nodes. The edge target may be a GeometryPolygon or a
    GeometryCompositeSurface, resolved by id at flush time."""
    href = sm_el.get(XLINK_HREF, "")
    if href:
        batch[rel_key].append({"src": container_id, "tgt": href.lstrip("#"),
                               "order": order, "via_xlink": True,
                               "orientation": orientation})
        return
    os_el = sm_el.find("gml:OrientableSurface", NS)
    if os_el is not None:
        base = os_el.find("gml:baseSurface", NS)
        if base is not None:
            _record_member(base, container_id, order, source, batch, rel_key,
                           orientation=os_el.get("orientation", "+"))
        return
    poly_el = sm_el.find("gml:Polygon", NS)
    if poly_el is not None:
        pid = _collect_polygon(poly_el, source, batch)
        batch[rel_key].append({"src": container_id, "tgt": pid, "order": order,
                               "via_xlink": False, "orientation": orientation})
        return
    cs_el = sm_el.find("gml:CompositeSurface", NS)
    if cs_el is not None:
        cs_id = cs_el.get(GML_ID) or f"cs_{container_id}_{order}"
        batch["composite_surfaces"].append({"id": cs_id, "source": source})
        for i, m in enumerate(cs_el.findall("gml:surfaceMember", NS)):
            _record_member(m, cs_id, i, source, batch, "composite_poly_rels")
        batch[rel_key].append({"src": container_id, "tgt": cs_id, "order": order,
                               "via_xlink": False, "orientation": orientation})


def _collect_ms_node(ms_el, ms_id, lod, source, batch):
    """Create a GeometryMultiSurface node for ms_el and collect its members as
    explicit HAS_SURFACE_MEMBER edges (ms_poly_rels), marking xlink references and
    orientation. An inner gml:CompositeSurface that carries a gml:id is also
    materialized as a node so it can be referenced as an XLink target."""
    ms_props = {"id": ms_id, "lod": lod, "source": source}
    cs_el = ms_el.find("gml:surfaceMember/gml:CompositeSurface", NS)
    if cs_el is not None and cs_el.get(GML_ID):
        ms_props["composite_surface_id"] = cs_el.get(GML_ID)
        batch["composite_surfaces"].append({"id": cs_el.get(GML_ID), "source": source})
    if ms_el.find("gml:surfaceMember/gml:OrientableSurface", NS) is not None:
        for i, sm in enumerate(ms_el.findall("gml:surfaceMember", NS)):
            _record_member(sm, ms_id, i, source, batch, "ms_poly_rels")
    else:
        for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
            pid = _collect_polygon(poly_el, source, batch)
            batch["ms_poly_rels"].append({"src": ms_id, "tgt": pid, "order": p_idx,
                                          "via_xlink": False, "orientation": None})
            if cs_el is not None and cs_el.get(GML_ID):
                batch["composite_poly_rels"].append(
                    {"src": cs_el.get(GML_ID), "tgt": pid, "order": p_idx})
    batch["multi_surfaces"].append(ms_props)
    return ms_id


def _collect_surface_openings(surface_el, surf_id, source, batch):
    """Collect bldg:opening → Door/Window for a boundary surface."""
    for op_order, op_el in enumerate(surface_el.findall("bldg:opening", NS)):
        o_el = op_type = None
        for cand in ("Window", "Door"):
            found = op_el.find(f"bldg:{cand}", NS)
            if found is not None:
                o_el, op_type = found, cand
                break
        if o_el is None:
            continue
        o_id = o_el.get(GML_ID) or f"{surf_id}_opening_{op_order}"
        o_gen_props, o_gen_map = _extract_gen_attrs(o_el)
        op_props = {
            "id": o_id, "opening_type": op_type, "source": source,
            "_gen_attrs_map": json.dumps(o_gen_map) if o_gen_map else "{}",
        }
        o_name = _text(o_el, "gml:name")
        o_desc = _text(o_el, "gml:description")
        o_cd   = _text(o_el, "cg:creationDate")
        o_rt   = _text(o_el, "cg:relativeToTerrain")
        o_rw   = _text(o_el, "cg:relativeToWater")
        if o_name: op_props["gml_name"] = o_name
        if o_desc: op_props["gml_description"] = o_desc
        if o_cd:   op_props["creation_date"] = o_cd
        if o_rt:   op_props["relative_to_terrain"] = o_rt
        if o_rw:   op_props["relative_to_water"] = o_rw
        o_env = o_el.find("gml:boundedBy/gml:Envelope", NS)
        if o_env is not None:
            op_props["has_bbox_envelope"] = True
            olc = _text(o_env, "gml:lowerCorner"); ouc = _text(o_env, "gml:upperCorner")
            if olc: op_props["bbox_lower_corner"] = olc
            if ouc: op_props["bbox_upper_corner"] = ouc
        op_props.update(o_gen_props)
        batch["openings"].append(op_props)
        batch["surf_opening_rels"].append(
            {"src": surf_id, "tgt": o_id, "order": op_order}
        )
        for lodc in ("2", "3", "4"):
            oms_el = o_el.find(f"bldg:lod{lodc}MultiSurface/gml:MultiSurface", NS)
            if oms_el is None:
                continue
            oms_id = oms_el.get(GML_ID) or f"ms_{o_id}_lod{lodc}"
            _collect_ms_node(oms_el, oms_id, lodc, source, batch)
            batch["opening_ms_rels"].append({"src": o_id, "tgt": oms_id, "lod": lodc})
            break


def _collect_boundary_surface(bb_el, parent_id, boundary_order, source, batch):
    """Collect a bldg:boundedBy surface for a Building, Room, or installation.
    The HAS_BOUNDARY edge is created from parent_id (any feature node)."""
    bldg_id = parent_id   # used only for synthesising deterministic ids
    surface_el   = None
    surface_type = None
    for st in (
        "GroundSurface", "WallSurface", "RoofSurface", "ClosureSurface",
        "FloorSurface", "InteriorWallSurface", "CeilingSurface",
        "OuterFloorSurface", "OuterCeilingSurface",
    ):
        el = bb_el.find(f"bldg:{st}", NS)
        if el is not None:
            surface_el   = el
            surface_type = st
            break

    if surface_el is None:
        return

    # gml:id is optional on boundary surfaces (e.g. FZK-Haus LoD4). Synthesise a
    # deterministic id when absent so the surface — and its polygons — are still
    # ingested; deterministic keeps MERGE-based re-ingest idempotent.
    surf_id       = surface_el.get(GML_ID) or \
        f"bsurf_{_slug(bldg_id)}_{boundary_order}_{surface_type.lower()}"
    creation_date = _text(surface_el, "cg:creationDate")
    surf_name     = _text(surface_el, "gml:name")
    surf_desc     = _text(surface_el, "gml:description")
    surf_relterr  = _text(surface_el, "cg:relativeToTerrain")
    surf_relwatr  = _text(surface_el, "cg:relativeToWater")
    surf_env      = surface_el.find("gml:boundedBy/gml:Envelope", NS)
    surf_env_lc   = _text(surf_env, "gml:lowerCorner") if surf_env is not None else None
    surf_env_uc   = _text(surf_env, "gml:upperCorner") if surf_env is not None else None
    gen_props, gen_map = _extract_gen_attrs(surface_el)

    lod              = "2"
    multi_surface_id = ""
    composite_id     = ""        # inner gml:CompositeSurface id, if the MS wraps one
    uses_orientable  = False

    for lod_candidate in ("2", "3", "4"):
        ms_el = surface_el.find(
            f"bldg:lod{lod_candidate}MultiSurface/gml:MultiSurface", NS
        )
        if ms_el is not None:
            lod              = lod_candidate
            multi_surface_id = ms_el.get(GML_ID) or ""
            if ms_el.find("gml:surfaceMember/gml:OrientableSurface", NS) is not None:
                # Surface geometry references polygons via OrientableSurface
                # wrappers (orientation + baseSurface href). Each becomes a
                # HAS_POLYGON edge marked via_xlink with its orientation.
                uses_orientable = True
                for i, sm in enumerate(ms_el.findall("gml:surfaceMember", NS)):
                    _record_member(sm, surf_id, i, source, batch, "surf_poly_rels")
            else:
                # Some files wrap the surface polygons in a single
                # gml:CompositeSurface inside the MultiSurface (FZK-Haus LoD3/4);
                # preserve its id and materialize it so the lodNSolid that
                # references it by xlink can do so through an explicit edge.
                cs_el = ms_el.find("gml:surfaceMember/gml:CompositeSurface", NS)
                if cs_el is not None and cs_el.get(GML_ID):
                    composite_id = cs_el.get(GML_ID)
                    batch["composite_surfaces"].append(
                        {"id": composite_id, "source": source})
                for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
                    pid = _collect_polygon(poly_el, source, batch)
                    batch["surf_poly_rels"].append(
                        {"src": surf_id, "tgt": pid, "order": p_idx,
                         "via_xlink": False, "orientation": None})
                    if composite_id:
                        batch["composite_poly_rels"].append(
                            {"src": composite_id, "tgt": pid, "order": p_idx})
            break

    surf_props = {
        "id":             surf_id,
        "surface_type":   surface_type,
        "lod":            lod,
        "source":         source,
        "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}",
    }
    if multi_surface_id:
        surf_props["multi_surface_id"] = multi_surface_id
    if composite_id:
        surf_props["composite_surface_id"] = composite_id
    if uses_orientable:
        surf_props["uses_orientable"] = True
    if creation_date:
        surf_props["creation_date"] = creation_date
    if surf_name:
        surf_props["gml_name"] = surf_name
    if surf_desc:
        surf_props["gml_description"] = surf_desc
    if surf_relterr:
        surf_props["relative_to_terrain"] = surf_relterr
    if surf_relwatr:
        surf_props["relative_to_water"] = surf_relwatr
    if surf_env is not None:
        surf_props["has_bbox_envelope"] = True
        if surf_env_lc: surf_props["bbox_lower_corner"] = surf_env_lc
        if surf_env_uc: surf_props["bbox_upper_corner"] = surf_env_uc
    surf_props.update(gen_props)
    _capture_common_extras(surface_el, surf_props)

    batch["surfaces"].append(surf_props)
    batch["boundary_rels"].append(
        {"src": parent_id, "tgt": surf_id, "order": boundary_order}
    )

    _collect_surface_openings(surface_el, surf_id, source, batch)


def _collect_solid(b_el, parent_id, source, batch, ns_prefix="bldg"):
    """Collect ALL lod*Solid geometries of a Building/Room (parent_id owns them).
    A feature may carry one solid per LOD simultaneously (Helsinki buildings ship
    both lod1Solid and lod2Solid; keeping only the first lost the lod2 solid and
    its xlink member list for 2 666 buildings). Each solid's exterior
    CompositeSurface members become explicit HAS_SURFACE_MEMBER edges
    (surface_member_rels) marked with via_xlink / orientation, so the exact
    xlink:href member list round-trips. Returns the first solid id or None."""
    first_id = None
    for lod_candidate in ("1", "2", "3", "4"):
        solid_el = b_el.find(f"{ns_prefix}:lod{lod_candidate}Solid/gml:Solid", NS)
        if solid_el is None:
            continue

        solid_id     = solid_el.get(GML_ID) or f"solid_{parent_id}_lod{lod_candidate}"
        cs_el        = solid_el.find("gml:exterior/gml:CompositeSurface", NS)
        composite_id = cs_el.get(GML_ID) if cs_el is not None else ""

        if cs_el is not None:
            for sm_order, sm_el in enumerate(cs_el.findall("gml:surfaceMember", NS)):
                _record_member(sm_el, solid_id, sm_order, source, batch,
                               "surface_member_rels")

        batch["solids"].append({
            "id":                   solid_id,
            "lod":                  lod_candidate,
            "composite_surface_id": composite_id or "",
            "source":               source,
        })
        batch["solid_rels"].append({"src": parent_id, "tgt": solid_id})
        if first_id is None:
            first_id = solid_id
    return first_id


def _collect_building_multisurfaces(b_el, bldg_id, source, batch):
    """Collect MultiSurface geometry attached directly to a Building:
    bldg:lod0FootPrint, bldg:lod0RoofEdge, and bldg:lod{1..4}MultiSurface.
    Each is stored as a GeometryMultiSurface; the property role and LOD are kept
    on the HAS_LOD_SURFACE relationship for faithful re-export."""
    geom_props = [
        ("lod0FootPrint", "0", "footprint"),
        ("lod0RoofEdge",  "0", "roofedge"),
    ] + [(f"lod{n}MultiSurface", n, "multisurface") for n in ("1", "2", "3", "4")]

    for prop_name, lod, role in geom_props:
        prop_el = b_el.find(f"bldg:{prop_name}", NS)
        if prop_el is None:
            continue
        ms_el = prop_el.find("gml:MultiSurface", NS)
        if ms_el is None:
            continue
        ms_id = ms_el.get(GML_ID) or f"ms_{bldg_id}_{role}_lod{lod}"
        batch["multi_surfaces"].append({"id": ms_id, "lod": lod, "source": source})
        batch["bldg_surface_rels"].append(
            {"src": bldg_id, "tgt": ms_id, "lod": lod, "role": role}
        )
        for p_idx, poly_el in enumerate(ms_el.findall("gml:surfaceMember/gml:Polygon", NS)):
            pid = _collect_polygon(poly_el, source, batch)
            batch["ms_poly_rels"].append({"src": ms_id, "tgt": pid, "order": p_idx})


def _add_feature_scalars(el, props):
    """Capture common AbstractCityObject/feature scalars (gml:name/description,
    creationDate, class/function/usage + codeSpace) into props for Room /
    BuildingFurniture / (Int)BuildingInstallation nodes."""
    name = _text(el, "gml:name")
    desc = _text(el, "gml:description")
    cd   = _text(el, "cg:creationDate")
    rt   = _text(el, "cg:relativeToTerrain")
    rw   = _text(el, "cg:relativeToWater")
    if name: props["gml_name"] = name
    if desc: props["gml_description"] = desc
    if cd:   props["creation_date"] = cd
    if rt:   props["relative_to_terrain"] = rt
    if rw:   props["relative_to_water"] = rw
    env = el.find("gml:boundedBy/gml:Envelope", NS)
    if env is not None:
        props["has_bbox_envelope"] = True
        lc = _text(env, "gml:lowerCorner"); uc = _text(env, "gml:upperCorner")
        if lc: props["bbox_lower_corner"] = lc
        if uc: props["bbox_upper_corner"] = uc
    for attr, key in (("class", "class_code"), ("function", "function_code"),
                      ("usage", "usage_code")):
        sub = el.find(f"bldg:{attr}", NS)
        if sub is not None and sub.text:
            props[key] = sub.text.strip()
            cs = sub.get("codeSpace")
            if cs:
                props[f"{key.replace('_code', '')}_codespace"] = cs
    _capture_common_extras(el, props)


def _capture_common_extras(el, props):
    """Capture AbstractCityObject/AbstractFeature/AbstractGML content that is
    preserved verbatim for a lossless round-trip rather than modeled natively:

      - cg:terminationDate           → scalar property ``termination_date``
      - gml:metaDataProperty         → ``metadata_xml``           (JSON list)
      - gml:location                 → ``location_xml``           (single fragment)
      - cg:generalizesTo             → ``generalizes_to_xml``     (JSON list)
      - cg:externalReference         → ``external_reference_xml`` (JSON list)
      - ADE / gml:genericADEElement  → ``ade_xml``                (JSON list)

    XML subtrees are stored as serialized fragments and re-emitted on export —
    the same proven, fully lossless pattern already used for Building.address_xml.
    ADE content is detected structurally: any direct-child element in a namespace
    this ingester does not model natively (see ``_KNOWN_NS``)."""
    term = _text(el, "cg:terminationDate")
    if term:
        props["termination_date"] = term

    def _frags(matches):
        return [etree.tostring(m, encoding="unicode") for m in matches]

    meta = _frags(el.findall("gml:metaDataProperty", NS))
    if meta:
        props["metadata_xml"] = json.dumps(meta)
    loc = el.find("gml:location", NS)
    if loc is not None:
        props["location_xml"] = etree.tostring(loc, encoding="unicode")
    gens = _frags(el.findall("cg:generalizesTo", NS))
    if gens:
        props["generalizes_to_xml"] = json.dumps(gens)
    extrefs = _frags(el.findall("cg:externalReference", NS))
    if extrefs:
        props["external_reference_xml"] = json.dumps(extrefs)

    ade = []
    for child in el:
        tag = child.tag
        if not isinstance(tag, str) or not tag.startswith("{"):
            continue  # comments / PIs / un-namespaced
        uri = tag[1:tag.index("}")]
        if uri not in _KNOWN_NS:
            ade.append(etree.tostring(child, encoding="unicode"))
    if ade:
        props["ade_xml"] = json.dumps(ade)


def _collect_lod_geometry(el, feature_id, source, batch):
    """Collect bldg:lod{1..4}Geometry (a gml:MultiSurface) for furniture or an
    installation. Stored as GeometryMultiSurface linked via HAS_LOD_GEOMETRY."""
    for lod in ("1", "2", "3", "4"):
        prop = el.find(f"bldg:lod{lod}Geometry", NS)
        if prop is None:
            continue
        ms_el = prop.find("gml:MultiSurface", NS)
        if ms_el is None:
            return
        ms_id = ms_el.get(GML_ID) or f"ms_{feature_id}_lod{lod}geom"
        cs_el = ms_el.find("gml:surfaceMember/gml:CompositeSurface", NS)
        ms_props = {"id": ms_id, "lod": lod, "source": source}
        if cs_el is not None and cs_el.get(GML_ID):
            ms_props["composite_surface_id"] = cs_el.get(GML_ID)
        batch["multi_surfaces"].append(ms_props)
        batch["feature_geom_rels"].append(
            {"src": feature_id, "tgt": ms_id, "lod": lod, "role": "geometry"})
        for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
            pid = _collect_polygon(poly_el, source, batch)
            batch["ms_poly_rels"].append({"src": ms_id, "tgt": pid, "order": p_idx})
        return


def _collect_furniture(f_el, room_id, order, source, batch):
    fid = f_el.get(GML_ID) or f"{room_id}_furn_{order}"
    gen_props, gen_map = _extract_gen_attrs(f_el)
    props = {"id": fid, "source": source,
             "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}"}
    _add_feature_scalars(f_el, props)
    props.update(gen_props)
    batch["furniture"].append(props)
    batch["room_furn_rels"].append({"src": room_id, "tgt": fid, "order": order})
    _collect_lod_geometry(f_el, fid, source, batch)


def _collect_installation(i_el, parent_id, order, source, batch, parent_kind):
    iid  = i_el.get(GML_ID) or f"{parent_id}_inst_{order}"
    kind = etree.QName(i_el).localname   # IntBuildingInstallation | BuildingInstallation
    gen_props, gen_map = _extract_gen_attrs(i_el)
    props = {"id": iid, "source": source, "installation_kind": kind,
             "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}"}
    _add_feature_scalars(i_el, props)
    props.update(gen_props)
    batch["installations"].append(props)
    if parent_kind == "room":
        batch["room_inst_rels"].append({"src": parent_id, "tgt": iid, "order": order})
    elif parent_kind == "building_interior":
        batch["bldg_int_inst_rels"].append({"src": parent_id, "tgt": iid, "order": order})
    else:
        batch["bldg_inst_rels"].append({"src": parent_id, "tgt": iid, "order": order})
    _collect_lod_geometry(i_el, iid, source, batch)
    for b_order, bb_el in enumerate(i_el.findall("bldg:boundedBy", NS)):
        _collect_boundary_surface(bb_el, iid, b_order, source, batch)


def _collect_room(room_el, bldg_id, order, source, batch):
    room_id = room_el.get(GML_ID) or f"{bldg_id}_room_{order}"
    gen_props, gen_map = _extract_gen_attrs(room_el)
    props = {"id": room_id, "source": source,
             "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}"}
    _add_feature_scalars(room_el, props)
    props.update(gen_props)
    batch["rooms"].append(props)
    batch["interior_room_rels"].append({"src": bldg_id, "tgt": room_id, "order": order})

    for b_order, bb_el in enumerate(room_el.findall("bldg:boundedBy", NS)):
        _collect_boundary_surface(bb_el, room_id, b_order, source, batch)
    _collect_solid(room_el, room_id, source, batch)
    for f_order, ff in enumerate(
            room_el.findall("bldg:interiorFurniture/bldg:BuildingFurniture", NS)):
        _collect_furniture(ff, room_id, f_order, source, batch)
    for i_order, ii in enumerate(
            room_el.findall("bldg:roomInstallation/bldg:IntBuildingInstallation", NS)):
        _collect_installation(ii, room_id, i_order, source, batch, "room")


def _collect_building_interior(b_el, bldg_id, source, batch):
    """Collect bldg:interiorRoom → Room and bldg:outerBuildingInstallation."""
    for r_order, room_el in enumerate(
            b_el.findall("bldg:interiorRoom/bldg:Room", NS)):
        _collect_room(room_el, bldg_id, r_order, source, batch)
    for i_order, inst_el in enumerate(
            b_el.findall("bldg:outerBuildingInstallation/bldg:BuildingInstallation", NS)):
        _collect_installation(inst_el, bldg_id, i_order, source, batch, "building")
    for i_order, inst_el in enumerate(
            b_el.findall("bldg:interiorBuildingInstallation/bldg:IntBuildingInstallation", NS)):
        _collect_installation(inst_el, bldg_id, i_order, source, batch, "building_interior")


def _collect_terrain_intersection(b_el, bldg_id, source, batch):
    for lod_candidate in ("1", "2", "3", "4"):
        ti_parent = b_el.find(f"bldg:lod{lod_candidate}TerrainIntersection", NS)
        if ti_parent is None:
            continue

        ti_id = f"{bldg_id}_terrain_lod{lod_candidate}"
        batch["terrain_ints"].append({"id": ti_id, "lod": lod_candidate, "source": source})
        batch["bldg_terrain_rels"].append({"src": bldg_id, "tgt": ti_id})

        mc_el = ti_parent.find("gml:MultiCurve", NS)
        if mc_el is not None:
            for ls_order, cm_el in enumerate(mc_el.findall("gml:curveMember", NS)):
                ls_el = cm_el.find("gml:LineString", NS)
                if ls_el is None:
                    continue
                ls_id    = ls_el.get(GML_ID) or f"{ti_id}_ls_{ls_order}"
                pos_el   = ls_el.find("gml:posList", NS)
                srs_dim  = int(pos_el.get("srsDimension", "3")) if pos_el is not None else 3
                pos_list = _reproject_poslist(pos_el.text.strip(), srs_dim) \
                    if (pos_el is not None and pos_el.text) else ""
                batch["linestrings"].append({
                    "id": ls_id, "pos_list": pos_list,
                    "srs_dimension": srs_dim, "source": source,
                })
                batch["terrain_line_rels"].append(
                    {"src": ti_id, "tgt": ls_id, "order": ls_order}
                )
        break


def _extract_abstract_building_props(b_el):
    """All AbstractBuilding / _CityObject / _GML scalar, coded, measure, verbatim
    and generic-attribute properties shared by bldg:Building and bldg:BuildingPart
    (both are AbstractBuilding). Excludes the bbox/spatial fields and the search
    ``description``, which the caller adds. Returns
    (props, function_code, roof_type_code, gemeinde_key)."""
    props = {}
    creation_date  = _text(b_el, "cg:creationDate")
    class_code     = _text(b_el, "bldg:class")
    function_code  = _text(b_el, "bldg:function")
    usage_code     = _text(b_el, "bldg:usage")
    roof_type_code = _text(b_el, "bldg:roofType")

    def _codespace(xpath):
        el = b_el.find(xpath, NS)
        return el.get("codeSpace") if el is not None else None
    class_cs    = _codespace("bldg:class")
    function_cs = _codespace("bldg:function")
    usage_cs    = _codespace("bldg:usage")
    rooftype_cs = _codespace("bldg:roofType")

    gml_name          = _text(b_el, "gml:name")
    gml_description   = _text(b_el, "gml:description")
    year_construction = _text(b_el, "bldg:yearOfConstruction")
    year_demolition   = _text(b_el, "bldg:yearOfDemolition")
    relative_terrain  = _text(b_el, "cg:relativeToTerrain")
    relative_water    = _text(b_el, "cg:relativeToWater")

    # Address: store ALL bldg:address subtrees verbatim as a JSON list (xAL is
    # unbounded AND bldg:address itself is unbounded — Helsinki buildings carry
    # up to several; keeping only the first lost 2 605 of 5 577 addresses).
    addr_els = b_el.findall("bldg:address", NS)
    address_xml = (json.dumps([etree.tostring(a, encoding="unicode") for a in addr_els])
                   if addr_els else None)

    mh_el               = b_el.find("bldg:measuredHeight", NS)
    measured_height     = None
    measured_height_uom = None
    if mh_el is not None and mh_el.text:
        try:
            measured_height = float(mh_el.text.strip())
        except ValueError:
            pass
        measured_height_uom = mh_el.get("uom")

    def _int_el(xpath):
        el = b_el.find(xpath, NS)
        if el is not None and el.text:
            try:
                return int(el.text.strip())
            except ValueError:
                pass
        return None
    storeys_above = _int_el("bldg:storeysAboveGround")
    storeys_below = _int_el("bldg:storeysBelowGround")

    # storeyHeights*Ground are gml:MeasureOrNullList — preserve the value list
    # verbatim plus its uom (lossless; never reformat the numbers).
    def _measure_list(xpath):
        el = b_el.find(xpath, NS)
        if el is not None and el.text:
            return el.text.strip(), el.get("uom")
        return None, None
    sh_above, sh_above_uom = _measure_list("bldg:storeyHeightsAboveGround")
    sh_below, sh_below_uom = _measure_list("bldg:storeyHeightsBelowGround")

    ext_info_sys = _text(b_el, "cg:externalReference/cg:informationSystem")
    ext_obj_name = _text(b_el, "cg:externalReference/cg:externalObject/cg:name")

    gen_props, gen_map = _extract_gen_attrs(b_el)
    gemeinde_key = gen_props.get("gemeindeschluessel", "")

    if creation_date:               props["creation_date"]               = creation_date
    if class_code:                  props["class_code"]                  = class_code
    if function_code:               props["function_code"]               = function_code
    if usage_code:                  props["usage_code"]                  = usage_code
    if roof_type_code:              props["roof_type_code"]              = roof_type_code
    if class_cs:                    props["class_codespace"]             = class_cs
    if function_cs:                 props["function_codespace"]          = function_cs
    if usage_cs:                    props["usage_codespace"]             = usage_cs
    if rooftype_cs:                 props["roof_type_codespace"]         = rooftype_cs
    if gml_name:                    props["gml_name"]                    = gml_name
    if gml_description:             props["gml_description"]             = gml_description
    if year_construction:           props["year_of_construction"]        = year_construction
    if year_demolition:             props["year_of_demolition"]          = year_demolition
    if relative_terrain:            props["relative_to_terrain"]         = relative_terrain
    if relative_water:              props["relative_to_water"]           = relative_water
    if address_xml:                 props["address_xml"]                 = address_xml
    if measured_height is not None: props["measured_height"]             = measured_height
    if measured_height_uom:         props["measured_height_uom"]         = measured_height_uom
    if storeys_above is not None:   props["storeys_above_ground"]        = storeys_above
    if storeys_below is not None:   props["storeys_below_ground"]        = storeys_below
    if sh_above:                    props["storey_heights_above_ground"]     = sh_above
    if sh_above_uom:                props["storey_heights_above_ground_uom"] = sh_above_uom
    if sh_below:                    props["storey_heights_below_ground"]     = sh_below
    if sh_below_uom:                props["storey_heights_below_ground_uom"] = sh_below_uom
    if ext_info_sys:                props["external_information_system"] = ext_info_sys
    if ext_obj_name:                props["external_object_name"]        = ext_obj_name
    props.update(gen_props)
    props["_gen_attrs_map"] = json.dumps(gen_map) if gen_map else "{}"
    # terminationDate + verbatim metaDataProperty/location/generalizesTo/
    # externalReference/ADE (lossless, order-independent on export).
    _capture_common_extras(b_el, props)
    return props, function_code, roof_type_code, gemeinde_key


def _collect_building_part(bp_el, parent_id, order, source, batch):
    """Collect a bldg:BuildingPart — itself an AbstractBuilding — as a BuildingPart
    node linked from its parent via HAS_BUILDING_PART {order}. Reuses the full
    building geometry pipeline (boundaries, solid, terrain, multicurve,
    multisurfaces, interior) and recurses into nested building parts."""
    part_id, orig_gml_id, is_dup = _resolve_feature_id(
        bp_el.get(GML_ID) or f"{parent_id}_part_{order}",
        _content_hash(bp_el), _geom_hash(bp_el), source)
    if is_dup:
        # identical part already collected; still ensure this parent links to it.
        batch["bldg_part_rels"].append({"src": parent_id, "tgt": part_id, "order": order})
        return
    props, _fn, _rt, _gk = _extract_abstract_building_props(bp_el)
    props["id"]     = part_id
    props["name"]   = part_id
    props["source"] = source
    if orig_gml_id is not None:
        props["source_gml_id"] = orig_gml_id
        props["id_synthesized"] = True

    # A BuildingPart's gml:boundedBy/Envelope is optional; preserve it verbatim
    # for round-trip when present (parts are not registered in the R-tree).
    env = bp_el.find("gml:boundedBy/gml:Envelope", NS)
    if env is not None:
        props["has_bbox_envelope"] = True
        lc = _text(env, "gml:lowerCorner"); uc = _text(env, "gml:upperCorner")
        if lc: props["bbox_lower_corner"] = lc
        if uc: props["bbox_upper_corner"] = uc

    batch["building_parts"].append(props)
    batch["bldg_part_rels"].append({"src": parent_id, "tgt": part_id, "order": order})

    for b_order, bb_el in enumerate(bp_el.findall("bldg:boundedBy", NS)):
        _collect_boundary_surface(bb_el, part_id, b_order, source, batch)
    _collect_solid(bp_el, part_id, source, batch)
    _collect_terrain_intersection(bp_el, part_id, source, batch)
    _collect_building_multicurve(bp_el, part_id, source, batch)
    _collect_building_multisurfaces(bp_el, part_id, source, batch)
    _collect_building_interior(bp_el, part_id, source, batch)
    _collect_building_parts(bp_el, part_id, source, batch)


def _collect_building_parts(b_el, parent_id, source, batch):
    """Collect bldg:consistsOfBuildingPart/bldg:BuildingPart children of a
    Building or BuildingPart."""
    for order, bp_el in enumerate(
            b_el.findall("bldg:consistsOfBuildingPart/bldg:BuildingPart", NS)):
        _collect_building_part(bp_el, parent_id, order, source, batch)


def _collect_building_multicurve(b_el, parent_id, source, batch):
    """Collect bldg:lod{2..4}MultiCurve curves on an AbstractBuilding (distinct
    from lodNTerrainIntersection). Each gml:curveMember LineString becomes a
    GeometryLineString linked via HAS_LOD_MULTICURVE {lod, order}; coordinates
    are stored verbatim like every other posList."""
    for lod in ("2", "3", "4"):
        mc_parent = b_el.find(f"bldg:lod{lod}MultiCurve", NS)
        if mc_parent is None:
            continue
        mc_el = mc_parent.find("gml:MultiCurve", NS)
        if mc_el is None:
            continue
        for ls_order, cm_el in enumerate(mc_el.findall("gml:curveMember", NS)):
            ls_el = cm_el.find("gml:LineString", NS)
            if ls_el is None:
                continue
            ls_id   = ls_el.get(GML_ID) or f"{parent_id}_mc_lod{lod}_{ls_order}"
            pos_el  = ls_el.find("gml:posList", NS)
            srs_dim = int(pos_el.get("srsDimension", "3")) if pos_el is not None else 3
            pos     = _reproject_poslist(pos_el.text.strip(), srs_dim) \
                if (pos_el is not None and pos_el.text) else ""
            batch["linestrings"].append({
                "id": ls_id, "pos_list": pos,
                "srs_dimension": srs_dim, "source": source,
            })
            batch["bldg_multicurve_rels"].append(
                {"src": parent_id, "tgt": ls_id, "lod": lod, "order": ls_order})


def _collect_building(b_el, source, batch, seen_functions, seen_roof_types):
    """
    Extract all data from a Building element into batch dicts (no DB calls).
    Returns (bldg_id, gemeinde_key) or (None, None) if the building is invalid.
    """
    raw_id = b_el.get(GML_ID)
    if not raw_id:
        return None, None
    bldg_id, orig_gml_id, is_dup = _resolve_feature_id(
        raw_id, _content_hash(b_el), _geom_hash(b_el), source)
    if is_dup:
        # exact or geometric duplicate of an already-collected building (e.g. a
        # ward-replicated PLATEAU tile, verbatim or ADE-metadata-only differing);
        # the same physical building is already a node, so skip this occurrence.
        return None, None

    b_env = b_el.find("gml:boundedBy/gml:Envelope", NS)
    had_envelope = b_env is not None
    if b_env is not None:
        b_lc_text = _text(b_env, "gml:lowerCorner")
        b_uc_text = _text(b_env, "gml:upperCorner")
    else:
        # CityGML makes the per-feature gml:boundedBy/Envelope optional; derive the
        # bbox from the building's own geometry when it is absent (e.g. FZK-Haus).
        b_lc_text, b_uc_text = _bbox_from_geometry(b_el)
    if not b_lc_text or not b_uc_text:
        print(f"WARNING: missing bbox corners for {bldg_id}")
        return None, None
    b_lc = _parse_corner(b_lc_text)
    b_uc = _parse_corner(b_uc_text)
    if b_uc[0] <= b_lc[0] or b_uc[1] <= b_lc[1]:
        print(f"WARNING: invalid bbox for {bldg_id}")
        return None, None

    center_x = (b_lc[0] + b_uc[0]) / 2.0
    center_y = (b_lc[1] + b_uc[1]) / 2.0
    bbox_wkt = (
        f"POLYGON(("
        f"{b_lc[0]} {b_lc[1]}, "
        f"{b_uc[0]} {b_lc[1]}, "
        f"{b_uc[0]} {b_uc[1]}, "
        f"{b_lc[0]} {b_uc[1]}, "
        f"{b_lc[0]} {b_lc[1]}"
        f"))"
    )

    scalar_props, function_code, roof_type_code, gemeinde_key = \
        _extract_abstract_building_props(b_el)

    fn_label        = BUILDING_FUNCTION_NAMES.get(function_code or "", "Building")
    rt_label        = ROOF_TYPE_NAMES.get(roof_type_code or "", "")
    measured_height = scalar_props.get("measured_height")
    storeys_above   = scalar_props.get("storeys_above_ground")
    height_str  = f", {measured_height} m tall" if measured_height is not None else ""
    storeys_str = f", {storeys_above} storeys" if storeys_above is not None else ""
    roof_str    = f", {rt_label} roof" if rt_label else ""
    description = (
        f"{fn_label}{height_str}{storeys_str}{roof_str}"
        f", at ({center_x:.1f}, {center_y:.1f})"
    )

    props = {
        "id":                bldg_id,
        "name":              bldg_id,
        "source":            source,
        "bbox_lower_corner": b_lc_text,
        "bbox_upper_corner": b_uc_text,
        "bbox_min_x":        b_lc[0],
        "bbox_min_y":        b_lc[1],
        "bbox_min_z":        b_lc[2],
        "bbox_max_x":        b_uc[0],
        "bbox_max_y":        b_uc[1],
        "bbox_max_z":        b_uc[2],
        "center_x":          center_x,
        "center_y":          center_y,
        "ground_z":          b_lc[2],
        "bbox_wkt":          bbox_wkt,
        "description":       description,
        "has_bbox_envelope": had_envelope,
    }
    props.update(scalar_props)
    if orig_gml_id is not None:
        # id was synthesized to avoid a MERGE collapse; keep the authoritative
        # gml:id so export re-emits it (faithful round-trip of duplicate-id sources).
        props["source_gml_id"] = orig_gml_id
        props["id_synthesized"] = True

    batch["buildings"].append(props)

    # BuildingFunction / RoofType HUB nodes (a handful of shared vocabulary nodes
    # for the whole city, e.g. a few roof-form classes for 388k Hamburg buildings)
    # are deliberately NOT written from the parse/writer hot path: concurrent
    # `MERGE (n {id})` on the same hub id from parallel tiles' writers can create
    # duplicate hubs (no uniqueness constraint), and every edge-create serializes
    # on the hub's write lock. The authoritative codes are already stored as
    # scalar properties on each Building row (`function_code`/`roof_type_code`,
    # the lossless round-trip representation); the hubs and their HAS_FUNCTION /
    # HAS_ROOF_TYPE edges are DERIVED from those scalars after parsing, in the
    # per-file post-pass (single-file ingest) or in one global single-threaded
    # pass (tiled ingest) — race-free by construction.

    for b_order, bb_el in enumerate(b_el.findall("bldg:boundedBy", NS)):
        _collect_boundary_surface(bb_el, bldg_id, b_order, source, batch)

    _collect_solid(b_el, bldg_id, source, batch)
    _collect_terrain_intersection(b_el, bldg_id, source, batch)
    _collect_building_multicurve(b_el, bldg_id, source, batch)
    _collect_building_multisurfaces(b_el, bldg_id, source, batch)
    _collect_building_interior(b_el, bldg_id, source, batch)
    _collect_building_parts(b_el, bldg_id, source, batch)

    return bldg_id, gemeinde_key


# ---------------------------------------------------------------------------
# Generic city object collectors (all non-building modules)
# ---------------------------------------------------------------------------

def _collect_lod_solid_generic(el, feature_id, ns_prefix, source, batch, lod_range):
    """Collect ALL lod*Solid geometries for any module (a feature may carry one
    solid per LOD simultaneously). Returns the first lod string or None."""
    first_lod = None
    for lod in lod_range:
        solid_el = el.find(f"{ns_prefix}:lod{lod}Solid/gml:Solid", NS)
        if solid_el is None:
            continue
        solid_id     = solid_el.get(GML_ID) or f"solid_{feature_id}_lod{lod}"
        cs_el        = solid_el.find("gml:exterior/gml:CompositeSurface", NS)
        composite_id = cs_el.get(GML_ID) if cs_el is not None else ""
        batch["solids"].append({
            "id": solid_id, "lod": lod,
            "composite_surface_id": composite_id or "", "source": source,
        })
        batch["co_solid_rels"].append({"src": feature_id, "tgt": solid_id})
        if cs_el is not None:
            for sm_order, sm_el in enumerate(cs_el.findall("gml:surfaceMember", NS)):
                _record_member(sm_el, solid_id, sm_order, source, batch,
                               "surface_member_rels")
        if first_lod is None:
            first_lod = lod
    return first_lod


def _collect_lod_multi_surface_generic(el, feature_id, ns_prefix, source, batch, lod_range):
    """Collect lod*MultiSurface for any module. Returns lod string or None."""
    for lod in lod_range:
        ms_parent = el.find(f"{ns_prefix}:lod{lod}MultiSurface", NS)
        if ms_parent is None:
            continue
        ms_el = ms_parent.find("gml:MultiSurface", NS)
        if ms_el is None:
            continue
        ms_id = ms_el.get(GML_ID) or f"ms_{feature_id}_lod{lod}"
        batch["multi_surfaces"].append({"id": ms_id, "lod": lod, "source": source})
        batch["co_surface_rels"].append({"src": feature_id, "tgt": ms_id, "lod": lod})
        for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
            pid = _collect_polygon(poly_el, source, batch)
            batch["ms_poly_rels"].append({"src": ms_id, "tgt": pid, "order": p_idx})
        return lod
    return None


def _collect_lod_geometry_generic(el, feature_id, ns_prefix, source, batch, lod_range):
    """Collect lod*Geometry (Solid or MultiSurface) for any module."""
    for lod in lod_range:
        geom_parent = el.find(f"{ns_prefix}:lod{lod}Geometry", NS)
        if geom_parent is None:
            continue
        # Try Solid first
        solid_el = geom_parent.find("gml:Solid", NS)
        if solid_el is not None:
            solid_id     = solid_el.get(GML_ID) or f"solid_{feature_id}_lod{lod}"
            cs_el        = solid_el.find("gml:exterior/gml:CompositeSurface", NS)
            composite_id = cs_el.get(GML_ID) if cs_el is not None else ""
            batch["solids"].append({
                "id": solid_id, "lod": lod,
                "composite_surface_id": composite_id or "", "source": source,
            })
            batch["co_solid_rels"].append({"src": feature_id, "tgt": solid_id})
            if cs_el is not None:
                for sm_order, sm_el in enumerate(cs_el.findall("gml:surfaceMember", NS)):
                    _record_member(sm_el, solid_id, sm_order, source, batch,
                                   "surface_member_rels")
            return lod
        # Try MultiSurface
        ms_el = geom_parent.find("gml:MultiSurface", NS)
        if ms_el is not None:
            ms_id = ms_el.get(GML_ID) or f"ms_{feature_id}_lod{lod}"
            batch["multi_surfaces"].append({"id": ms_id, "lod": lod, "source": source})
            batch["co_surface_rels"].append({"src": feature_id, "tgt": ms_id, "lod": lod})
            for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
                pid = _collect_polygon(poly_el, source, batch)
                batch["ms_poly_rels"].append({"src": ms_id, "tgt": pid, "order": p_idx})
            return lod
    return None


def _collect_implicit_geometry(el, feature_id, ns_prefix, source, batch, lod_range):
    """Collect lod{N}ImplicitRepresentation for vegetation/furniture modules.

    Stores local-space polygon rings verbatim in GeometryRing nodes; the
    transformation_matrix and reference_point are stored on GeometryMultiSurface
    so that export and 3D-Tiles conversion can reconstruct world coordinates.
    """
    for lod in lod_range:
        impl_prop = el.find(f"{ns_prefix}:lod{lod}ImplicitRepresentation", NS)
        if impl_prop is None:
            continue
        impl_el = impl_prop.find("cg:ImplicitGeometry", NS)
        if impl_el is None:
            continue

        mat_el     = impl_el.find("cg:transformationMatrix", NS)
        matrix_str = mat_el.text.strip() if (mat_el is not None and mat_el.text) else ""

        ref_el  = impl_el.find("cg:referencePoint/gml:Point/gml:pos", NS)
        ref_str = ref_el.text.strip() if (ref_el is not None and ref_el.text) else ""

        geom_el = impl_el.find("cg:relativeGMLGeometry", NS)
        if geom_el is None:
            continue

        proto_href = geom_el.get(XLINK_HREF, "").lstrip("#")

        ms_el = geom_el.find("gml:MultiSurface", NS)
        if ms_el is None:
            ms_el = geom_el.find(".//gml:MultiSurface", NS)

        ms_id = (ms_el.get(GML_ID) if ms_el is not None else None) \
                or f"impl_ms_{feature_id}_lod{lod}"

        ms_props = {
            "id":                    ms_id,
            "lod":                   lod,
            "source":                source,
            "is_implicit":           True,
            "transformation_matrix": matrix_str,
            "reference_point":       ref_str,
        }
        if proto_href:
            ms_props["prototype_href"] = proto_href

        batch["multi_surfaces"].append(ms_props)
        batch["co_surface_rels"].append({"src": feature_id, "tgt": ms_id, "lod": lod})

        if ms_el is not None:
            for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
                pid = _collect_polygon(poly_el, source, batch)
                batch["ms_poly_rels"].append({"src": ms_id, "tgt": pid, "order": p_idx})

        return lod
    return None


def _collect_relief_geometry(el, feature_id, source, batch):
    """Collect geometry from dem:ReliefFeature relief components."""
    lod_attr = el.get("lod", "")

    for comp_wrapper in el.findall("dem:reliefComponent", NS):
        for comp_el in comp_wrapper:
            comp_id = comp_el.get(GML_ID) or f"rc_{feature_id}_{uuid.uuid4().hex[:8]}"

            # TINRelief — preserve the full TIN structure so it re-exports as
            # dem:TINRelief/dem:tin/gml:TriangulatedSurface/gml:trianglePatches/
            # gml:Triangle (not a generic MultiSurface). The triangles are stored
            # as GeometryPolygon members; the MS node carries geometry_type='TIN'
            # plus the wrapper ids/metadata for faithful reconstruction.
            ts_el = comp_el.find("dem:tin/gml:TriangulatedSurface", NS)
            if ts_el is not None:
                ms_id   = comp_id
                tin_lod = _text(comp_el, "dem:lod") or lod_attr or "0"
                ms_props = {
                    "id": ms_id, "lod": tin_lod, "geometry_type": "TIN",
                    "tin_relief_id": comp_el.get(GML_ID) or "",
                    "triangulated_surface_id": ts_el.get(GML_ID) or "",
                    "source": source,
                }
                tin_cd = _text(comp_el, "cg:creationDate")
                if tin_cd:
                    ms_props["tin_creation_date"] = tin_cd
                tin_env = comp_el.find("gml:boundedBy/gml:Envelope", NS)
                if tin_env is not None:
                    lc = _text(tin_env, "gml:lowerCorner")
                    uc = _text(tin_env, "gml:upperCorner")
                    if lc and uc:
                        ms_props["tin_bbox_lower"] = lc
                        ms_props["tin_bbox_upper"] = uc
                batch["multi_surfaces"].append(ms_props)
                batch["co_surface_rels"].append(
                    {"src": feature_id, "tgt": ms_id, "lod": tin_lod}
                )
                tp_el = ts_el.find("gml:trianglePatches", NS)
                for p_idx, tri_el in enumerate(
                    (tp_el if tp_el is not None else ts_el).findall(".//gml:Triangle", NS)
                ):
                    ring_el = tri_el.find("gml:exterior/gml:LinearRing", NS)
                    if ring_el is None:
                        continue
                    fake_poly_id = f"tri_{ms_id}_{p_idx}"
                    pos_list, srs_dim = _ring_coords(ring_el)
                    ring_id  = ring_el.get(GML_ID) or f"ring_{fake_poly_id}"
                    batch["polygons"].append({"id": fake_poly_id, "source": source})
                    batch["rings"].append({
                        "id": ring_id, "pos_list": pos_list,
                        "srs_dimension": srs_dim, "ring_type": "exterior", "source": source,
                    })
                    batch["poly_ext_ring_rels"].append({"src": fake_poly_id, "tgt": ring_id})
                    batch["ms_poly_rels"].append(
                        {"src": ms_id, "tgt": fake_poly_id, "order": p_idx,
                         "via_xlink": False, "orientation": None}
                    )
                continue

            # MassPointRelief — store MultiPoint as inline geometry property (no polygon chain)
            mp_el = comp_el.find("dem:massPoints/gml:MultiPoint", NS)
            if mp_el is not None:
                # Store raw GML as a linestring-like blob (no topology needed)
                ls_id    = f"mp_{comp_id}"
                pos_list = " ".join(
                    pos.text.strip()
                    for pos in mp_el.findall(".//gml:pos", NS)
                    if pos.text
                )
                batch["linestrings"].append({
                    "id": ls_id, "pos_list": pos_list,
                    "srs_dimension": 3, "source": source,
                    "geometry_type": "MassPoint",
                })
                continue

            # BreaklineRelief — store curves as GeometryLineStrings
            for mc_el in comp_el.findall("dem:breaklines/gml:MultiCurve", NS):
                for ls_order, cm_el in enumerate(mc_el.findall("gml:curveMember", NS)):
                    ls_el = cm_el.find("gml:LineString", NS)
                    if ls_el is None:
                        continue
                    ls_id    = ls_el.get(GML_ID) or f"{comp_id}_ls_{ls_order}"
                    pos_el   = ls_el.find("gml:posList", NS)
                    srs_dim  = int(pos_el.get("srsDimension", "3")) if pos_el is not None else 3
                    pos_list = _reproject_poslist(pos_el.text.strip(), srs_dim) \
                        if (pos_el is not None and pos_el.text) else ""
                    batch["linestrings"].append({
                        "id": ls_id, "pos_list": pos_list,
                        "srs_dimension": srs_dim, "source": source,
                        "geometry_type": "Breakline",
                    })


def _collect_generic_boundary_surfaces(el, feature_id, ns_prefix, surface_types, source, batch):
    """Collect module-specific boundary surfaces (wtr:boundedBy, brid:boundedBy, tun:boundedBy)."""
    for b_order, bb_el in enumerate(el.findall(f"{ns_prefix}:boundedBy", NS)):
        for st in surface_types:
            surface_el = bb_el.find(f"{ns_prefix}:{st}", NS)
            if surface_el is None:
                continue
            surf_id = surface_el.get(GML_ID) or f"surf_{feature_id}_{b_order}"
            creation_date = _text(surface_el, "cg:creationDate")
            gen_props, gen_map = _extract_gen_attrs(surface_el)
            # per-surface metadata (BoundarySurface extends _CityObject)
            surf_meta = {}
            for nm, xp in (("gml_name", "gml:name"), ("gml_description", "gml:description"),
                           ("relative_to_terrain", "cg:relativeToTerrain"),
                           ("relative_to_water", "cg:relativeToWater")):
                v = _text(surface_el, xp)
                if v:
                    surf_meta[nm] = v
            s_env = surface_el.find("gml:boundedBy/gml:Envelope", NS)
            if s_env is not None:
                surf_meta["has_bbox_envelope"] = True
                slc = _text(s_env, "gml:lowerCorner"); suc = _text(s_env, "gml:upperCorner")
                if slc: surf_meta["bbox_lower_corner"] = slc
                if suc: surf_meta["bbox_upper_corner"] = suc

            lod              = "2"
            multi_surface_id = ""
            geom_prop        = "MultiSurface"   # or "Surface" (WaterBody lodNSurface)
            composite_id     = ""
            polys_ordered    = []

            for lod_candidate in ("2", "3", "4", "1", "0"):
                # lod{N}MultiSurface (Bridge, Tunnel, typical WaterBody)
                ms_el = surface_el.find(
                    f"{ns_prefix}:lod{lod_candidate}MultiSurface/gml:MultiSurface", NS
                )
                if ms_el is not None:
                    lod              = lod_candidate
                    multi_surface_id = ms_el.get(GML_ID) or ""
                    cs_el = ms_el.find("gml:surfaceMember/gml:CompositeSurface", NS)
                    if cs_el is not None and cs_el.get(GML_ID):
                        composite_id = cs_el.get(GML_ID)
                        batch["composite_surfaces"].append({"id": composite_id, "source": source})
                    for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
                        pid = _collect_polygon(poly_el, source, batch)
                        polys_ordered.append((pid, p_idx))
                        if composite_id:
                            batch["composite_poly_rels"].append(
                                {"src": composite_id, "tgt": pid, "order": p_idx})
                    break
                # lod{N}Surface (WaterBody WaterSurface/WaterGroundSurface) → CompositeSurface
                surf_prop_el = surface_el.find(f"{ns_prefix}:lod{lod_candidate}Surface", NS)
                if surf_prop_el is not None:
                    lod = lod_candidate
                    geom_prop = "Surface"
                    inner = surf_prop_el[0] if len(surf_prop_el) else None
                    if inner is not None:
                        multi_surface_id = inner.get(GML_ID) or ""
                        if etree.QName(inner).localname == "CompositeSurface" and inner.get(GML_ID):
                            composite_id = inner.get(GML_ID)
                            batch["composite_surfaces"].append({"id": composite_id, "source": source})
                    for p_idx, poly_el in enumerate(surf_prop_el.findall(".//gml:Polygon", NS)):
                        pid = _collect_polygon(poly_el, source, batch)
                        polys_ordered.append((pid, p_idx))
                        if composite_id:
                            batch["composite_poly_rels"].append(
                                {"src": composite_id, "tgt": pid, "order": p_idx})
                    break

            surf_props = {
                "id":             surf_id,
                "surface_type":   st,
                "lod":            lod,
                "source":         source,
                "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}",
            }
            if multi_surface_id:
                surf_props["multi_surface_id"] = multi_surface_id
            if composite_id:
                surf_props["composite_surface_id"] = composite_id
            if geom_prop == "Surface":
                surf_props["geom_prop"] = "Surface"
            if creation_date:
                surf_props["creation_date"] = creation_date
            surf_props.update(surf_meta)
            surf_props.update(gen_props)

            batch["surfaces"].append(surf_props)
            batch["boundary_rels"].append(
                {"src": feature_id, "tgt": surf_id, "order": b_order}
            )
            for pid, p_order in polys_ordered:
                batch["surf_poly_rels"].append(
                    {"src": surf_id, "tgt": pid, "order": p_order,
                     "via_xlink": False, "orientation": None})
            break  # only one surface type per boundedBy wrapper


# Nested sub-features per module: (containment property, child element name)
_NESTED_FEATURES = {
    "brid": [("outerBridgeConstruction",   "BridgeConstructionElement"),
             ("outerBridgeInstallation",   "BridgeInstallation"),
             ("interiorBridgeInstallation","BridgeInstallation")],
    "tun":  [("outerTunnelInstallation",   "TunnelInstallation"),
             ("interiorTunnelInstallation","TunnelInstallation")],
}


def _collect_nested_geometry(sub, nid, ns_prefix, source, batch):
    """Collect a nested feature's lod{N}Geometry (MultiSurface or Solid)."""
    for lod in ("4", "3", "2", "1"):
        geom = sub.find(f"{ns_prefix}:lod{lod}Geometry", NS)
        if geom is None:
            continue
        ms_el = geom.find("gml:MultiSurface", NS)
        if ms_el is not None:
            ms_id = ms_el.get(GML_ID) or f"ms_{nid}_lod{lod}geom"
            _collect_ms_node(ms_el, ms_id, lod, source, batch)
            batch["feature_geom_rels"].append(
                {"src": nid, "tgt": ms_id, "lod": lod, "role": "geometry"})
            return
        sol_el = geom.find("gml:Solid", NS)
        if sol_el is not None:
            sid  = sol_el.get(GML_ID) or f"solid_{nid}_lod{lod}"
            cs   = sol_el.find("gml:exterior/gml:CompositeSurface", NS)
            comp = cs.get(GML_ID) if cs is not None else ""
            if cs is not None:
                for o, sm in enumerate(cs.findall("gml:surfaceMember", NS)):
                    _record_member(sm, sid, o, source, batch, "surface_member_rels")
            batch["solids"].append({"id": sid, "lod": lod,
                                    "composite_surface_id": comp or "", "source": source})
            batch["solid_rels"].append({"src": nid, "tgt": sid})
            return


def _collect_nested_features(el, parent_id, ns_prefix, bsurf_types, source, batch):
    """Collect bridge/tunnel installations & construction elements as NestedFeature
    nodes attached to the parent via HAS_NESTED {prop, order}."""
    for prop, kind in _NESTED_FEATURES.get(ns_prefix, []):
        for order, wrap in enumerate(el.findall(f"{ns_prefix}:{prop}", NS)):
            sub = wrap.find(f"{ns_prefix}:{kind}", NS)
            if sub is None:
                continue
            nid = sub.get(GML_ID) or f"{parent_id}_{_slug(prop)}_{order}"
            gen_props, gen_map = _extract_gen_attrs(sub)
            props = {"id": nid, "feature_kind": kind, "ns_prefix": ns_prefix,
                     "source": source,
                     "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}"}
            b_env = sub.find("gml:boundedBy/gml:Envelope", NS)
            props["has_bbox_envelope"] = b_env is not None
            if b_env is not None:
                lc = _text(b_env, "gml:lowerCorner"); uc = _text(b_env, "gml:upperCorner")
                if lc: props["bbox_lower_corner"] = lc
                if uc: props["bbox_upper_corner"] = uc
            for nm, xp in (("gml_name", "gml:name"), ("gml_description", "gml:description"),
                           ("creation_date", "cg:creationDate"),
                           ("relative_to_terrain", "cg:relativeToTerrain"),
                           ("relative_to_water", "cg:relativeToWater")):
                v = _text(sub, xp)
                if v:
                    props[nm] = v
            for attr in ("class", "function", "usage"):
                e = sub.find(f"{ns_prefix}:{attr}", NS)
                if e is not None and e.text:
                    props[attr] = e.text.strip()
                    if e.get("codeSpace"):
                        props[f"{attr}_codespace"] = e.get("codeSpace")
            props.update(gen_props)
            batch["nested_features"].append(props)
            batch["nested_rels"].append(
                {"src": parent_id, "tgt": nid, "prop": prop, "order": order})
            _collect_nested_geometry(sub, nid, ns_prefix, source, batch)
            if bsurf_types:
                _collect_generic_boundary_surfaces(sub, nid, ns_prefix, bsurf_types, source, batch)


def _collect_city_object(el, feature_type, ns_prefix, scalar_attrs,
                         boundary_surface_types, geom_style, lod_range,
                         source, batch):
    """
    Generic collector for any non-building CityGML top-level feature.
    Returns feature_id or None if the element has no gml:id.
    """
    raw_id = el.get(GML_ID)
    if not raw_id:
        return None
    feature_id, orig_gml_id, is_dup = _resolve_feature_id(
        raw_id, _content_hash(el), _geom_hash(el), source)
    if is_dup:
        return None   # exact or geometric duplicate already collected; skip

    props: dict = {
        "id":           feature_id,
        "name":         feature_id,
        "feature_type": feature_type,
        "source":       source,
    }
    if orig_gml_id is not None:
        props["source_gml_id"] = orig_gml_id
        props["id_synthesized"] = True

    # Optional bounding box
    b_env = el.find("gml:boundedBy/gml:Envelope", NS)
    props["has_bbox_envelope"] = b_env is not None
    if b_env is not None:
        lc_el = b_env.find("gml:lowerCorner", NS)
        uc_el = b_env.find("gml:upperCorner", NS)
        if lc_el is not None and lc_el.text and uc_el is not None and uc_el.text:
            b_lc = _parse_corner(lc_el.text.strip())
            b_uc = _parse_corner(uc_el.text.strip())
            center_x = (b_lc[0] + b_uc[0]) / 2.0
            center_y = (b_lc[1] + b_uc[1]) / 2.0
            props.update({
                "bbox_lower_corner": lc_el.text.strip(),
                "bbox_upper_corner": uc_el.text.strip(),
                "bbox_min_x":  b_lc[0], "bbox_min_y":  b_lc[1], "bbox_min_z":  b_lc[2],
                "bbox_max_x":  b_uc[0], "bbox_max_y":  b_uc[1], "bbox_max_z":  b_uc[2],
                "center_x":    center_x,
                "center_y":    center_y,
            })
            if b_uc[0] > b_lc[0] and b_uc[1] > b_lc[1]:
                props["bbox_wkt"] = (
                    f"POLYGON(("
                    f"{b_lc[0]} {b_lc[1]}, "
                    f"{b_uc[0]} {b_lc[1]}, "
                    f"{b_uc[0]} {b_uc[1]}, "
                    f"{b_lc[0]} {b_uc[1]}, "
                    f"{b_lc[0]} {b_lc[1]}"
                    f"))"
                )

    creation_date = _text(el, "cg:creationDate")
    if creation_date:
        props["creation_date"] = creation_date

    # Free-text + core AbstractCityObject scalars (parity with buildings)
    gml_name = _text(el, "gml:name")
    gml_desc = _text(el, "gml:description")
    rel_terr = _text(el, "cg:relativeToTerrain")
    rel_watr = _text(el, "cg:relativeToWater")
    ext_info = _text(el, "cg:externalReference/cg:informationSystem")
    ext_objn = _text(el, "cg:externalReference/cg:externalObject/cg:name")
    if gml_name: props["gml_name"] = gml_name
    if gml_desc: props["gml_description"] = gml_desc
    if rel_terr: props["relative_to_terrain"] = rel_terr
    if rel_watr: props["relative_to_water"] = rel_watr
    if ext_info: props["external_information_system"] = ext_info
    if ext_objn: props["external_object_name"] = ext_objn

    # Module-specific scalar attributes (+ codeSpace where present)
    for prop_name, xpath in scalar_attrs:
        sub = el.find(xpath, NS)
        if sub is not None and sub.text:
            props[prop_name] = sub.text.strip()
            cs = sub.get("codeSpace")
            if cs:
                props[f"{prop_name}_codespace"] = cs

    # Generic attributes
    gen_props, gen_map = _extract_gen_attrs(el)
    props.update(gen_props)
    props["_gen_attrs_map"] = json.dumps(gen_map) if gen_map else "{}"
    # terminationDate + verbatim metaDataProperty/location/generalizesTo/
    # externalReference/ADE — shared by every CityGML feature.
    _capture_common_extras(el, props)

    batch["city_objects"].append(props)

    # Geometry
    if geom_style == "solid_or_surface":
        _collect_lod_solid_generic(el, feature_id, ns_prefix, source, batch, lod_range)
        _collect_lod_multi_surface_generic(el, feature_id, ns_prefix, source, batch, lod_range)
    elif geom_style == "surface_only":
        _collect_lod_multi_surface_generic(el, feature_id, ns_prefix, source, batch, lod_range)
    elif geom_style == "geometry":
        found = _collect_lod_geometry_generic(el, feature_id, ns_prefix, source, batch, lod_range)
        if not found:
            _collect_implicit_geometry(el, feature_id, ns_prefix, source, batch, lod_range)
    elif geom_style == "relief":
        _collect_relief_geometry(el, feature_id, source, batch)

    # Boundary surfaces (WaterBody, Bridge, Tunnel)
    if boundary_surface_types:
        _collect_generic_boundary_surfaces(
            el, feature_id, ns_prefix, boundary_surface_types, source, batch
        )

    # Nested sub-features (bridge/tunnel installations & construction elements)
    _collect_nested_features(el, feature_id, ns_prefix, boundary_surface_types,
                             source, batch)

    # CityObjectGroup membership (xlink:href to member features)
    if feature_type == "CityObjectGroup":
        for g_order, gm in enumerate(el.findall("grp:groupMember", NS)):
            href = gm.get(XLINK_HREF, "")
            if href:
                batch["group_member_rels"].append(
                    {"src": feature_id, "tgt": href.lstrip("#"),
                     "order": g_order, "role": gm.get("role", "")})

    return feature_id


# ---------------------------------------------------------------------------
# Index creation
# ---------------------------------------------------------------------------

def _create_indexes(sess):
    for label, prop in [
        # Shared label carried by every node — lets the generic, multi-label
        # relationship MATCHes in _flush_batch (where the source/target node type
        # is not known at flush time) resolve by id through a single index
        # instead of falling back to an AllNodesScan per row.
        ("Entity",                "id"),
        ("Building",              "id"),
        ("BuildingPart",          "id"),
        ("BoundarySurface",       "id"),
        ("GeometryPolygon",       "id"),
        ("GeometryRing",          "id"),
        ("GeometrySolid",         "id"),
        ("GeometryMultiSurface",  "id"),
        ("GeometryLineString",    "id"),
        ("TerrainIntersection",   "id"),
        ("BuildingFunction",      "id"),
        ("RoofType",              "id"),
        ("Dataset",               "id"),
        ("CityObject",            "id"),
        ("CityObject",            "feature_type"),
        # `source` indexes: the per-file post-pass (PART_OF, HAS_FUNCTION,
        # HAS_ROOF_TYPE, LOCATED_IN, xlink prototype resolution) filters by the
        # tile's filename. Without these, every one of those queries is a full
        # label scan — O(total nodes) per FILE, i.e. quadratic across a tiled
        # city (the dominant slowdown on Tokyo's 2,335 tiles).
        ("Building",              "source"),
        ("CityObject",            "source"),
        ("GeometryMultiSurface",  "source"),
    ]:
        try:
            idx_name = f"{label.lower()}_{prop}_idx"
            sess.run(
                f"CREATE INDEX {idx_name} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.{prop})"
            )
        except Exception:
            pass
    try:
        sess.run(
            "CREATE POINT INDEX building_location_idx IF NOT EXISTS "
            "FOR (n:Building) ON (n.location)"
        )
    except Exception:
        pass
    try:
        sess.run(
            "CREATE POINT INDEX cityobject_location_idx IF NOT EXISTS "
            "FOR (n:CityObject) ON (n.location)"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------

def ingest(gml_path: Path, driver, num_writers: int = 3, nearby_edges: bool = True,
           dataset_id: str = None, dataset_name: str = None,
           target_crs: str = None, source_crs: str = None, id_registry: dict = None,
           skip_spatial_index: bool = False, defer_code_hubs: bool = False):
    # `source` stays per-file (tile-level provenance on every feature node);
    # `dataset_id`/`dataset_name`, when given, merge many tiles into ONE Dataset
    # node (see main()). Without them, behaviour is unchanged: one file, one Dataset.
    # `target_crs` reprojects all coordinates to a metric CRS at ingest (see the
    # reprojection primitives above); `source_crs` overrides the file's srsName.
    # `id_registry` is the feature-id resolution state (see _resolve_feature_id):
    # pass a shared registry across the sequential tiles of ONE Dataset so cross-tile
    # duplicate/collision gml:ids are deduped/disambiguated; leave None for an
    # independent dataset (each parallel ingest gets its own registry on its own thread).
    source = gml_path.name
    _reg = id_registry if id_registry is not None else _new_id_registry()
    _tls.seen_ids = _reg["seen"]
    _tls.assigned_ids = _reg["assigned"]
    # Lock is only needed when this registry is shared across parallel parser
    # threads; for a private registry it is uncontended and effectively free.
    _tls.id_lock = _reg.get("lock")

    with open(gml_path, "rb") as fh:
        header_bytes = fh.read(2000)
    fmt = _format_version(header_bytes)

    # Set up (or clear) the per-thread reprojector before any coordinate is parsed.
    reproj_target = _epsg_from_srs(target_crs) if target_crs else None
    if reproj_target:
        src_srs = source_crs or _peek_srsname(gml_path)
        if not src_srs:
            raise SystemExit(f"ERROR: --target-crs set but no source CRS found in "
                             f"{gml_path.name}; pass --source-crs.")
        _tls.reproj = _Reprojector(src_srs, reproj_target)
        print(f"  Reprojecting: {_epsg_from_srs(src_srs) or src_srs} -> {reproj_target}")
    else:
        _tls.reproj = None
    src_crs_original = None

    crs           = None
    srs_dim       = 3
    bbox_lc_text  = None
    bbox_uc_text  = None
    lod           = "LOD0"
    lod_detected  = False
    envelope_done = False

    dataset_id         = dataset_id or _slug(gml_path.stem)
    ds_display_name    = dataset_name or source
    seen_functions     = set()
    seen_roof_types    = set()
    gemeinde_key       = ""
    buildings_ingested = 0
    objects_ingested   = 0
    type_counts: dict  = {}   # feature_type → count, for summary log

    batch = _new_batch()

    with driver.session(database="neo4j") as sess:
        with _SHARED_NODE_LOCK:
            _create_indexes(sess)
            sess.run(
                "MERGE (n:Dataset {id: $id}) SET n.name = $name, n.source = $src",
                id=dataset_id, name=ds_display_name, src=ds_display_name,
            )

        # ── Writer thread pool ───────────────────────────────────────────────
        batch_queue: queue.Queue = queue.Queue(maxsize=num_writers * 2)
        flush_errors: list = []
        flush_lock = threading.Lock()

        def _writer():
            with driver.session(database="neo4j") as wsess:
                while True:
                    item = batch_queue.get()
                    if item is None:
                        batch_queue.task_done()
                        break
                    try:
                        # Batches are pure MERGE/SET (idempotent), so a whole-batch
                        # retry on transient lock conflicts (deadlocks between
                        # writer threads sharing edge endpoints) is safe.
                        for attempt in range(5):
                            try:
                                _flush_batch(wsess, item)
                                break
                            except TransientError:
                                if attempt == 4:
                                    raise
                                time.sleep(0.2 * (attempt + 1))
                    except Exception as exc:
                        with flush_lock:
                            flush_errors.append(exc)
                    finally:
                        batch_queue.task_done()

        writers = [
            threading.Thread(target=_writer, daemon=True)
            for _ in range(num_writers)
        ]
        for w in writers:
            w.start()

        parse_tags = [ENVELOPE_TAG, BLDG_TAG] + CITY_OBJECT_TAGS

        for _, el in etree.iterparse(
            str(gml_path), events=("end",), tag=parse_tags
        ):
            if el.tag == ENVELOPE_TAG:
                if not envelope_done:
                    crs     = el.get("srsName")
                    srs_dim = int(el.get("srsDimension", 3))
                    lc_el   = el.find("{http://www.opengis.net/gml}lowerCorner")
                    uc_el   = el.find("{http://www.opengis.net/gml}upperCorner")
                    if lc_el is not None and lc_el.text:
                        bbox_lc_text = lc_el.text.strip()
                    if uc_el is not None and uc_el.text:
                        bbox_uc_text = uc_el.text.strip()
                    envelope_done = True
                continue

            if el.tag == BLDG_TAG:
                if not lod_detected:
                    lod          = _lod_level(el)
                    lod_detected = True

                bldg_id, gk = _collect_building(
                    el, source, batch, seen_functions, seen_roof_types
                )
                if bldg_id:
                    buildings_ingested += 1
                    if not gemeinde_key and gk:
                        gemeinde_key = gk

            elif el.tag in MODULE_REGISTRY:
                label, ns_prefix, scalar_attrs, bsurf_types, geom_style, lod_range = \
                    MODULE_REGISTRY[el.tag]

                if not lod_detected:
                    lod          = _lod_level_generic(el, ns_prefix)
                    lod_detected = True

                co_id = _collect_city_object(
                    el, label, ns_prefix, scalar_attrs,
                    bsurf_types, geom_style, lod_range, source, batch
                )
                if co_id:
                    objects_ingested += 1
                    type_counts[label] = type_counts.get(label, 0) + 1

            total_in_batch = len(batch["buildings"]) + len(batch["city_objects"])
            if total_in_batch >= BATCH_SIZE:
                batch_queue.put(batch)
                print(
                    f"  Queued: {buildings_ingested + objects_ingested} top-level features …",
                    flush=True,
                )
                batch = _new_batch()

            el.clear()
            wrapper = el.getparent()
            if wrapper is not None:
                while wrapper.getprevious() is not None:
                    del wrapper.getparent()[0]

        if batch["buildings"] or batch["city_objects"]:
            batch_queue.put(batch)

        for _ in writers:
            batch_queue.put(None)
        for w in writers:
            w.join()

        if flush_errors:
            raise flush_errors[0]

        all_type_counts = {"Building": buildings_ingested, **type_counts}
        print(f"  Top-level features ingested: {buildings_ingested + objects_ingested}")
        for _ft, _n in sorted(all_type_counts.items()):
            if _n > 0:
                print(f"    {_ft}: {_n}")

        # ── Resolve xlink:href implicit geometry prototypes ───────────────────
        rows_proto = sess.run(
            "MATCH (ms:GeometryMultiSurface {source: $src}) "
            "WHERE ms.prototype_href IS NOT NULL "
            "RETURN ms.id AS ms_id, ms.prototype_href AS proto_id",
            src=source,
        ).data()
        if rows_proto:
            print(f"  Resolving {len(rows_proto)} xlink:href implicit geometry prototype references …")
            sess.run(
                "UNWIND $rows AS row "
                "MATCH (ms:GeometryMultiSurface {id: row.ms_id}) "
                "MATCH (proto:GeometryMultiSurface {id: row.proto_id}) "
                "MATCH (proto)-[r:HAS_SURFACE_MEMBER]->(p:GeometryPolygon) "
                "MERGE (ms)-[r2:HAS_SURFACE_MEMBER]->(p) "
                "SET r2.order = r.order",
                rows=rows_proto,
            )
            print(f"  Prototype references resolved.")

        # ── PART_OF (Building → Dataset) ─────────────────────────────────────
        _bids = [
            r["id"] for r in sess.run(
                "MATCH (b:Building {source: $src}) WHERE NOT (b)-[:PART_OF]->() RETURN b.id AS id",
                src=source,
            ).data()
        ]
        for _i in range(0, len(_bids), 1000):
            _run_retry(sess,
                "UNWIND $ids AS bid MATCH (b:Building {id: bid}) "
                "MATCH (d:Dataset {id: $did}) MERGE (b)-[:PART_OF]->(d)",
                ids=_bids[_i : _i + 1000], did=dataset_id,
            )

        # ── PART_OF (CityObject → Dataset) ───────────────────────────────────
        _co_ids = [
            r["id"] for r in sess.run(
                "MATCH (n:CityObject {source: $src}) WHERE NOT (n)-[:PART_OF]->() RETURN n.id AS id",
                src=source,
            ).data()
        ]
        for _i in range(0, len(_co_ids), 1000):
            _run_retry(sess,
                "UNWIND $ids AS coid MATCH (n:CityObject {id: coid}) "
                "MATCH (d:Dataset {id: $did}) MERGE (n)-[:PART_OF]->(d)",
                ids=_co_ids[_i : _i + 1000], did=dataset_id,
            )

        # ── HAS_FUNCTION / HAS_ROOF_TYPE (serial post-pass) ──────────────────
        # Hubs + edges are DERIVED from the Building scalars written by the
        # batches (function_code / roof_type_code). The hub MERGEs run under
        # _SHARED_NODE_LOCK because independent datasets may ingest in parallel
        # threads; each fires once per distinct code per file, so the lock is
        # held microseconds. In tiled mode (defer_code_hubs=True) this whole
        # section is skipped and main() derives hubs + edges for the entire
        # Dataset in ONE single-threaded pass after all tiles — race-free by
        # construction and free of per-hub lock contention.
        if not defer_code_hubs:
            _derive_code_hubs(sess, source_filter=source)

        # ── Dataset metadata ──────────────────────────────────────────────────
        # Scalar metadata uses coalesce and the bbox uses min/max CASE so that
        # ingesting several tiled files under one dataset_id UNIONS their
        # envelopes instead of the last tile overwriting the others. For a fresh
        # single-file dataset this reduces to plain assignment (coalesce of null,
        # CASE on null), so single-file behaviour is unchanged.
        cm_name, cm_desc, cm_schema_loc = _read_citymodel_header(gml_path)
        # When reprojecting, the stored CRS is the target; the original srsName
        # is kept as source_crs for provenance.
        stored_crs = reproj_target or crs
        _run_retry(sess,
            "MATCH (n:Dataset {id: $id}) SET "
            "n.format = coalesce(n.format, $format), "
            "n.crs = coalesce(n.crs, $crs), "
            "n.srs_dimension = coalesce(n.srs_dimension, $srs_dim), "
            "n.lod = coalesce(n.lod, $lod), "
            "n.source_crs = coalesce(n.source_crs, $scrs)",
            id=dataset_id, format=fmt, crs=stored_crs, srs_dim=srs_dim, lod=lod,
            scrs=(crs if reproj_target else None),
        )
        for _k, _v in (("gml_name", cm_name), ("gml_description", cm_desc),
                       ("schema_location", cm_schema_loc)):
            if _v:
                _run_retry(sess, f"MATCH (n:Dataset {{id: $id}}) SET n.{_k} = coalesce(n.{_k}, $v)",
                           id=dataset_id, v=_v)
        if bbox_lc_text:
            _lc = _parse_corner(bbox_lc_text)
            _run_retry(sess,
                "MATCH (n:Dataset {id: $id}) SET "
                "n.bbox_lower_corner = coalesce(n.bbox_lower_corner, $t), "
                "n.bbox_min_x = CASE WHEN n.bbox_min_x IS NULL OR $x < n.bbox_min_x THEN $x ELSE n.bbox_min_x END, "
                "n.bbox_min_y = CASE WHEN n.bbox_min_y IS NULL OR $y < n.bbox_min_y THEN $y ELSE n.bbox_min_y END, "
                "n.bbox_min_z = CASE WHEN n.bbox_min_z IS NULL OR $z < n.bbox_min_z THEN $z ELSE n.bbox_min_z END",
                id=dataset_id, t=bbox_lc_text, x=_lc[0], y=_lc[1], z=_lc[2],
            )
        if bbox_uc_text:
            _uc = _parse_corner(bbox_uc_text)
            _run_retry(sess,
                "MATCH (n:Dataset {id: $id}) SET "
                "n.bbox_upper_corner = coalesce(n.bbox_upper_corner, $t), "
                "n.bbox_max_x = CASE WHEN n.bbox_max_x IS NULL OR $x > n.bbox_max_x THEN $x ELSE n.bbox_max_x END, "
                "n.bbox_max_y = CASE WHEN n.bbox_max_y IS NULL OR $y > n.bbox_max_y THEN $y ELSE n.bbox_max_y END, "
                "n.bbox_max_z = CASE WHEN n.bbox_max_z IS NULL OR $z > n.bbox_max_z THEN $z ELSE n.bbox_max_z END",
                id=dataset_id, t=bbox_uc_text, x=_uc[0], y=_uc[1], z=_uc[2],
            )

        # ── District / City ───────────────────────────────────────────────────
        district_name, city_name = _resolve_municipality(gemeinde_key, gml_path.stem)
        district_id = _slug(district_name)
        city_id     = _slug(city_name)

        with _SHARED_NODE_LOCK:
            sess.run("MERGE (n:City {id: $id}) SET n.name = $name", id=city_id, name=city_name)
            sess.run("MERGE (n:District {id: $id}) SET n.name = $name", id=district_id, name=district_name)
        _run_retry(sess,
            "MATCH (a:District {id: $did}), (b:City {id: $cid}) MERGE (a)-[:PART_OF]->(b)",
            did=district_id, cid=city_id,
        )
        _run_retry(sess,
            "MATCH (a:Dataset {id: $dsid}), (b:District {id: $did}) MERGE (a)-[:COVERS]->(b)",
            dsid=dataset_id, did=district_id,
        )
        _run_retry(sess,
            "MATCH (b:Building {source: $src}), (d:District {id: $did}) "
            "MERGE (b)-[:LOCATED_IN]->(d)",
            src=source, did=district_id,
        )

        # ── Proximity graph (buildings only) ──────────────────────────────────
        if nearby_edges:
            print("  Creating NEARBY_BUILDING proximity edges (100 m radius) …")
            proximity_edges = _create_proximity_edges_native(sess)
        else:
            proximity_edges = 0
            print("  Skipping NEARBY_BUILDING proximity edges (nearby_edges=False).")

        # ── Spatial R-tree (all feature bboxes) ──────────────────────────────
        # _index_features_spatial re-walks EVERY Building/CityObject node (adds
        # are idempotent but the walk is O(total nodes)), so running it per file
        # in a tiled ingest is quadratic across the tile set. The tiled path
        # therefore defers it (skip_spatial_index=True) and main() runs ONE
        # final pass after all tiles.
        spatial_indexed = 0
        if skip_spatial_index:
            print("  Spatial R-tree indexing deferred to the final pass (tiled mode).")
        else:
            print("  Populating spatial R-tree index on bounding boxes …")
            spatial_ok = _ensure_spatial_layer(sess)
            if spatial_ok:
                # NB: CityObject footprints are intentionally NOT indexed here.
                # OSM fusion (ingest_osm.py) pulls candidate footprints from this
                # 'features' layer; putting non-Building CityObjects (esp. roads,
                # whose bbox rectangles span many match tiles) in it before fusion
                # makes the matcher's per-tile spatial.intersects and the per-type
                # match phases explode on a dense multi-module city (Tokyo). The
                # CityObject spatial index is added AFTER fusion instead — see
                # ingest_osm.py's post-fusion CityObject-index step (and
                # _backfill_cityobject_bbox_from_geometry).
                spatial_indexed = _index_features_spatial(sess)
            print(f"  Spatial R-tree entries: {spatial_indexed}")

        # ── GenAI vector embeddings (optional) ───────────────────────────────
        genai_key = os.environ.get("NEO4J_GENAI_KEY")
        if genai_key:
            print("  Creating vector index and encoding building descriptions …")
            try:
                sess.run(
                    """
                    CREATE VECTOR INDEX building_embeddings IF NOT EXISTS
                    FOR (b:Building) ON (b.embedding)
                    OPTIONS {indexConfig: {
                      `vector.dimensions`: 1536,
                      `vector.similarity_function`: 'cosine'
                    }}
                    """
                )
                _emb_ids = [
                    r["id"] for r in sess.run(
                        "MATCH (b:Building) WHERE b.description IS NOT NULL RETURN b.id AS id"
                    ).data()
                ]
                for _ei in range(0, len(_emb_ids), 10):
                    sess.run(
                        "UNWIND $ids AS bid MATCH (b:Building {id: bid}) "
                        "CALL genai.vector.encode(b.description, 'OpenAI', {token: $key}) "
                        "YIELD vector SET b.embedding = vector",
                        ids=_emb_ids[_ei : _ei + 10], key=genai_key,
                    )
                print("  Building embeddings created.")
            except Exception as exc:
                print(f"  WARNING: GenAI encoding failed ({exc})")

        # ── GDS Louvain community detection (buildings only) ──────────────────
        if not nearby_edges:
            print("  INFO: GDS community detection skipped (no NEARBY_BUILDING edges).")
        else:
            try:
                sess.run("CALL gds.graph.drop('building_proximity', false)")
                sess.run(
                    """
                    CALL gds.graph.project(
                      'building_proximity', 'Building',
                      {NEARBY_BUILDING: {orientation: 'UNDIRECTED'}}
                    )
                    """
                )
                sess.run(
                    """
                    CALL gds.louvain.write('building_proximity',
                      {writeProperty: 'community_id'})
                    YIELD communityCount, modularity
                    """
                )
                sess.run("CALL gds.graph.drop('building_proximity')")
                print("  GDS Louvain community detection complete.")
            except Exception as exc:
                print(f"  INFO: GDS community detection skipped ({exc})")

    return {
        "source":          source,
        "format":          fmt,
        "crs":             crs,
        "srs_dimension":   srs_dim,
        "lod":             lod,
        "features_total":  buildings_ingested + objects_ingested,
        "features_by_type": dict(sorted(
            {"Building": buildings_ingested, **type_counts}.items()
        )),
        "proximity_edges": proximity_edges,
        "spatial_indexed": spatial_indexed,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest CityGML 2.0 (all modules) into Neo4j.")
    parser.add_argument("paths", nargs="*",
                        help="One or more .gml files and/or directories of .gml files "
                             "(a directory is expanded to the .gml files it contains).")
    parser.add_argument("--dataset-name", default=None,
                        help="Merge ALL input .gml into a SINGLE Dataset with this name — "
                             "use for a tiled city (e.g. --dataset-name tokyo for the 23 ward "
                             "files, or --dataset-name nyc for the 21 DA tiles). Every feature "
                             "keeps its own file name as `source` (tile provenance), while the "
                             "Dataset node is shared and its bounding box is the union of all "
                             "tiles. Without this flag, each .gml becomes its own Dataset.")
    parser.add_argument("--recursive", action="store_true",
                        help="Recurse into sub-directories when a path is a directory.")
    parser.add_argument("--target-crs", default=None,
                        help="Reproject all coordinates to this metric CRS at ingest "
                             "(e.g. --target-crs EPSG:6677 for Tokyo, EPSG:32618 for NYC). "
                             "Required for non-metric sources so distances/areas/proximity "
                             "and OSM fusion are correct; already-metric datasets don't need it.")
    parser.add_argument("--source-crs", default=None,
                        help="Override the source CRS (default: read the srsName from the file). "
                             "Only relevant together with --target-crs.")
    parser.add_argument(
        "--nearby-edges",
        action="store_true",
        help="Build NEARBY_BUILDING proximity edges and run GDS community detection "
             "(off by default; expensive on large datasets). For a tiled city, run this "
             "once after all tiles are ingested so cross-tile neighbours are included.",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=1,
        help="Parallel tile parsers for a --dataset-name (tiled) ingest: each worker "
             "parses one .gml file at a time, all sharing ONE id-resolution registry "
             "(lock-guarded), so cross-tile duplicate gml:ids still dedup/split "
             "correctly. Default 1 = sequential (fully deterministic). With N > 1, "
             "which occurrence of a duplicate id is encountered first follows thread "
             "scheduling; byte-identical replicas are unaffected, but for "
             "geometry-identical duplicates the surviving node's ancillary attributes "
             "come from whichever copy happened to be seen first. Parsing is "
             "GIL-bound Python + C, so gains saturate around 4-12 workers.",
    )
    parser.add_argument(
        "--writers",
        type=int,
        default=min(16, os.cpu_count() or 3),
        help="Parallel Neo4j writer threads draining the batch queue (default: min(16, cpu "
             "count), matching ingest_osm/census). Batches are idempotent MERGEs with "
             "transient-lock retry, so raising this is safe. Note the practical ceiling is "
             "the single lxml parser thread that feeds the queue (tiles of one --dataset-name "
             "are parsed sequentially), so writers past what keeps up with it just idle; "
             "Neo4j commits also serialize on one tx log. The page cache, not writer count, "
             "is the dominant throughput lever.",
    )
    args = parser.parse_args()

    # Expand paths (files and/or directories) into a flat, sorted list of .gml files.
    candidates = []
    for p in args.paths:
        pp = Path(p)
        if pp.is_dir():
            candidates.extend(sorted(pp.glob("**/*.gml" if args.recursive else "*.gml")))
        elif pp.is_file():
            candidates.append(pp)
        else:
            print(f"WARNING: {pp} not found — skipping.")
    if not candidates:
        print("ERROR: No .gml input files found. Pass one or more files or a directory.")
        sys.exit(1)

    shared_id = _slug(args.dataset_name) if args.dataset_name else None
    do_nearby_edges = args.nearby_edges
    # Tiled (shared_id) path runs parse_workers concurrent tile ingests (writers
    # split among them); independent datasets each get their own full writer pool.
    parse_workers = max(1, args.parse_workers) if shared_id else 1
    writers_per = args.writers if parse_workers == 1 else max(2, args.writers // parse_workers)
    concurrent_ingests = parse_workers if shared_id else len(candidates)
    pool_size = max(20, concurrent_ingests * (writers_per + 2) + 4)

    print("Connecting to Neo4j …")
    driver = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", "neo4jneo4j"),
        max_connection_pool_size=pool_size,
        notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"},
    )
    driver.verify_connectivity()
    print("  Connected.")

    results = {}
    if shared_id:
        # Tiled city -> one Dataset. One id-resolution registry is shared across
        # ALL tiles (lock-guarded), so a gml:id repeated across tiles is deduped
        # when the feature is byte/geometry-identical (e.g. Tokyo ward-replicated
        # tiles) and split when it is a distinct feature sharing an id (e.g. a
        # placeholder like 'ID_'). Dataset/City/District singleton MERGEs are
        # serialized via _SHARED_NODE_LOCK; the Dataset bbox union is Cypher-atomic
        # (CASE min/max under the node write lock); edge batches onto shared nodes
        # carry transient-lock retry. Per-file spatial indexing is DEFERRED: one
        # final R-tree pass replaces the per-file full re-walk, which was quadratic
        # across the tile set.
        print(f"Merging {len(candidates)} file(s) into one Dataset "
              f"'{args.dataset_name}' (id={shared_id}) "
              f"[parse-workers={parse_workers}, writers/file={writers_per}] ...")
        shared_registry = _new_id_registry()

        def _ingest_tile(f):
            print(f"Ingesting {f} -> dataset '{args.dataset_name}' ...")
            return ingest(f, driver, num_writers=writers_per,
                          nearby_edges=do_nearby_edges,
                          dataset_id=shared_id, dataset_name=args.dataset_name,
                          target_crs=args.target_crs, source_crs=args.source_crs,
                          id_registry=shared_registry, skip_spatial_index=True,
                          defer_code_hubs=True)

        if parse_workers == 1:
            for f in candidates:
                try:
                    results[f.name] = _ingest_tile(f)
                except Exception as exc:
                    print(f"ERROR ingesting {f}: {exc}")
                    results[f.name] = {"error": str(exc)}
        else:
            with ThreadPoolExecutor(max_workers=parse_workers) as tile_pool:
                tile_futs = {tile_pool.submit(_ingest_tile, f): f for f in candidates}
                for fut in as_completed(tile_futs):
                    tf = tile_futs[fut]
                    try:
                        results[tf.name] = fut.result()
                    except Exception as exc:
                        print(f"ERROR ingesting {tf}: {exc}")
                        results[tf.name] = {"error": str(exc)}

        # Single final R-tree pass over the whole Dataset (replaces the per-file
        # full re-walks; spatial.addNodes is idempotent, insertions stay serial),
        # then ONE single-threaded derivation of the BuildingFunction/RoofType
        # vocabulary hubs + edges from the Building scalars (race-free: no hub
        # is ever touched by the parallel writers).
        with driver.session(database="neo4j") as sess:
            print("Populating spatial R-tree index on bounding boxes (final pass) …")
            if _ensure_spatial_layer(sess):
                # CityObject footprints are added to the R-tree AFTER OSM fusion,
                # not here — see the note at the single-file call site above and
                # ingest_osm.py's post-fusion CityObject-index step.
                _total_spatial = _index_features_spatial(sess)
                print(f"Spatial R-tree entries: {_total_spatial}")
                results["_spatial_indexed_total"] = _total_spatial
            print("Deriving BuildingFunction/RoofType hubs + edges (final pass) …")
            _derive_code_hubs(sess)

        if do_nearby_edges:
            print("NOTE: proximity edges were built per tile; for correct cross-tile "
                  "neighbours, they span all buildings already loaded at each step.")
    else:
        # Independent datasets -> parallel (unchanged behaviour).
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            futures = {}
            for f in candidates:
                print(f"Ingesting {f} ...")
                futures[pool.submit(ingest, f, driver, num_writers=args.writers,
                                    nearby_edges=do_nearby_edges,
                                    target_crs=args.target_crs, source_crs=args.source_crs)] = f
            for fut in as_completed(futures):
                gml_f = futures[fut]
                try:
                    results[gml_f.name] = fut.result()
                except Exception as exc:
                    print(f"ERROR ingesting {gml_f}: {exc}")
                    results[gml_f.name] = {"error": str(exc)}

    driver.close()
    print("Done.")
    print(json.dumps(results, indent=2))
