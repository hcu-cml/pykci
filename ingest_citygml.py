"""
CityGML 2.0 / 3.0 → Neo4j ingestion — lossless, dataset-agnostic, all modules.
Optimized for large files (GBs): streaming iterparse + batched UNWIND writes.

The codec is chosen per file from the root namespace (citygml_profiles): CityGML 3.0
files are read by the 3.0 codec (ingest_citygml3, section "CityGML 3.0 codec" below),
everything else by the 2.0 code. Both write the one version-neutral model of GRAPH.md;
see CITYGML3.md for the 3.0 additions, the 2.0 ↔ 3.0 mapping and the tests.

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

from pykci_runtime import neo4j_auth, neo4j_uri, progress

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
    # module scalars of CityObjects / sub-features / surfaces (2.0 + 3.0). Without
    # these a gen:*Attribute named e.g. "height", "function" or "class" silently
    # overwrote the authoritative module value (props.update(gen_props) runs after
    # the module scalars), and export then wrote the corrupted value twice.
    "class", "function", "usage", "class_codespace", "function_codespace",
    "usage_codespace", "class_list", "function_list", "usage_list",
    "class_codespace_list", "function_codespace_list", "usage_codespace_list",
    "class_code_list", "function_code_list", "usage_code_list",
    "roof_type_codespace", "measured_height_uom", "year_of_demolition",
    "relative_to_water", "surface_material", "surface_material_codespace",
    "species", "species_codespace", "height", "height_uom", "trunk_diameter",
    "trunk_diameter_uom", "crown_diameter", "crown_diameter_uom", "average_height",
    "average_height_uom", "min_height", "min_height_uom", "max_height",
    "max_height_uom", "root_ball_diameter", "root_ball_diameter_uom",
    "max_root_ball_depth", "max_root_ball_depth_uom", "water_level",
    "water_level_codespace", "is_movable", "granularity", "traffic_direction",
    "occupancy_json", "space_type", "volume_json", "area_json", "surface_type",
    "lod", "opening_type", "filling_type", "installation_kind", "feature_kind",
    "ns_prefix", "relation_to_construction", "is_structural_element",
    "date_of_construction", "date_of_demolition", "condition_of_construction",
    "heights_json", "height_high_reference", "height_low_reference", "height_status",
    "elevations_json", "valid_from", "valid_to", "multi_surface_id", "uses_orientable",
    "geom_prop", "gml_identifier", "gml_identifier_codespace", "gml_id",
    # provenance / plumbing (GRAPH.md provenance rules, 3.0 codec)
    "source_type", "method", "method_version", "source_files", "rtree_layer",
    "synthetic_id", "pending_refs_json", "rtree_stale", "materialized_from",
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
        _reindex_stale(session)   # 3.0 features whose bbox grew in a later tile
        # Collect ids first (cheap: no geometry materialized), then re-match each
        # batch through the Entity(id) index — a single scan, index-backed lookups.
        # A feature registered in another source's own layer (n.rtree_layer, set by
        # --rtree-layer, e.g. the street-space reconstruction) never leaks into this
        # layer (GRAPH.md: a separate R-tree layer per source). Nodes without the
        # property belong to 'features', so a 2.0-only graph indexes exactly as before.
        # spatial.addNodes is NOT idempotent (verified on neo4j-spatial 2025.10: a
        # re-add creates a second RTREE_REFERENCE and a duplicate hit), so nodes that
        # are already indexed are skipped — re-running an ingest no longer duplicates
        # every previously indexed feature.
        ids = [r["id"] for r in session.run(
            "MATCH (n) WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NOT NULL "
            "AND coalesce(n.rtree_layer, 'features') = $layer "
            "AND NOT ()-[:RTREE_REFERENCE]->(n) "
            "RETURN n.id AS id", layer=layer_name,
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
        # ── CityGML 3.0 codec (and the 2.0 TrafficArea addition) ─────────────
        # All empty for a plain 2.0 file, so the 2.0 flush is unchanged.
        "buildings3":         [],   # Building rows (bbox via bbox_union, source_files union)
        "city_objects3":      [],   # top-level CityObject rows, label = feature_type
        "bbox_union":         [],   # {id, minx..maxz} — union-merged across tiles
        "sub_features":       [],   # {labels, props}: Section/TrafficSpace/Marking/BridgePart/…
        "surface_labels":     [],   # {id, label}: extra BoundarySurface label (TrafficArea…)
        "child_rels":         [],   # {rel, src, tgt, props}: HAS_SECTION/HAS_TRAFFIC_SPACE/…
        "polygon_props":      [],   # {id, props}: srs_name / synthetic_id on GeometryPolygon
        "prototypes":         [],   # GeometryPrototype rows (implicit-geometry templates)
        "proto_rels":         [],   # {src, tgt, lod, props}: HAS_PROTOTYPE {matrix, ref point}
        "solid_member_rels":  [],   # GeometrySolid|Prototype → GeometrySolid {order}
        "line_rels":          [],   # GeometryPrototype → GeometryLineString {order}
        "multicurve_rels3":   [],   # Entity → GeometryLineString {lod, order, multi_curve_id}
        "point_rels":         [],   # Entity → GeometryLineString{geometry_type:'Point'} {lod}
        "appearances":        [],   # Appearance rows
        "surface_data":       [],   # {labels, props}: X3DMaterial/ParameterizedTexture/…
        "appearance_rels":    [],   # Entity → Appearance {order}
        "surface_data_rels":  [],   # Appearance → surface data {order}
        "sub_bbox":           [],   # {id, minx..maxz}: sub-feature / surface bbox (union-merged)
    }


# Every feature-level node (features, sub-features, thematic surfaces, openings, interior
# features, point clouds, appearances) records the CityGML version of the file it came
# from, so a graph that mixes 2.0 and 3.0 sources still says which subgraph is which and
# an exporter can tell what was written in which version. Geometry nodes are version-neutral.
_CV_SET = ", n.citygml_version = coalesce($cv, n.citygml_version)"


def _flush_batch(sess, batch, citygml_version=None):
    cv = citygml_version
    has_buildings = bool(batch["buildings"])
    has_objects   = bool(batch["city_objects"])
    # (2.0 batches are only queued when they hold buildings or city objects; a 3.0
    #  batch may end with CityModel-level appearances only)
    if not has_buildings and not has_objects and not any(v for v in batch.values()):
        return

    # ── Building nodes ─────────────────────────────────────────────────────
    if has_buildings:
        sess.run(
            "UNWIND $rows AS row MERGE (n:Building {id: row.id}) "
            "SET n += row, n:Entity, n.location = point({x: row.center_x, y: row.center_y})"
            + _CV_SET,
            rows=batch["buildings"], cv=cv,
        )
    if batch["building_parts"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:BuildingPart {id: row.id}) SET n += row, n:Entity" + _CV_SET,
            rows=batch["building_parts"], cv=cv,
        )
    # NOTE: BuildingFunction / RoofType hub nodes are no longer written here —
    # see _collect_building: they are derived post-parse from the Building
    # scalars, so parallel writers never race on the shared vocabulary nodes.

    # ── Generic CityObject nodes (with APOC dynamic label) ─────────────────
    if has_objects:
        try:
            sess.run(
                "UNWIND $rows AS row "
                "MERGE (n:CityObject {id: row.id}) SET n += row, n:Entity" + _CV_SET + " "
                "WITH n, row CALL apoc.create.addLabels(n, [row.feature_type]) YIELD node "
                "WITH node WHERE node.center_x IS NOT NULL "
                "SET node.location = point({x: node.center_x, y: node.center_y})",
                rows=batch["city_objects"], cv=cv,
            )
        except Exception:
            # Fallback without APOC: just CityObject label
            sess.run(
                "UNWIND $rows AS row MERGE (n:CityObject {id: row.id}) SET n += row, n:Entity"
                + _CV_SET + " "
                "WITH n WHERE n.center_x IS NOT NULL "
                "SET n.location = point({x: n.center_x, y: n.center_y})",
                rows=batch["city_objects"], cv=cv,
            )

    # ── CityGML 3.0 feature / sub-feature / appearance nodes ────────────────
    if batch.get("buildings3") or batch.get("city_objects3") or batch.get("sub_features"):
        _flush_v3_feature_nodes(sess, batch, cv)

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
            "UNWIND $rows AS row MERGE (n:BoundarySurface {id: row.id}) SET n += row, n:Entity" + _CV_SET,
            rows=batch["surfaces"], cv=cv,
        )
    if batch["openings"]:
        sess.run(
            "UNWIND $rows AS row MERGE (n:Opening {id: row.id}) SET n += row, n:Entity" + _CV_SET,
            rows=batch["openings"], cv=cv,
        )
    # LoD4 interior feature nodes (created before any relationship flush so the
    # generic id-matched edges below resolve).
    if batch["rooms"]:
        sess.run("UNWIND $rows AS row MERGE (n:Room {id: row.id}) SET n += row, n:Entity" + _CV_SET,
                 rows=batch["rooms"], cv=cv)
    if batch["installations"]:
        sess.run("UNWIND $rows AS row MERGE (n:BuildingInstallation {id: row.id}) "
                 "SET n += row, n:Entity" + _CV_SET, rows=batch["installations"], cv=cv)
    if batch["furniture"]:
        sess.run("UNWIND $rows AS row MERGE (n:BuildingFurniture {id: row.id}) "
                 "SET n += row, n:Entity" + _CV_SET, rows=batch["furniture"], cv=cv)
    if batch["nested_features"]:
        sess.run("UNWIND $rows AS row MERGE (n:NestedFeature {id: row.id}) "
                 "SET n += row, n:Entity" + _CV_SET, rows=batch["nested_features"], cv=cv)
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
    if batch.get("prototypes") or batch.get("surface_labels") or batch.get("polygon_props") \
            or batch.get("bbox_union") or batch.get("appearances") or batch.get("sub_bbox"):
        _flush_v3_extra_nodes(sess, batch, cv)

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
    if any(batch.get(k) for k in _V3_REL_KEYS):
        _flush_v3_rels(sess, batch)


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


def _collect_tran_areas_v2(el, feature_id, source, batch):
    """CityGML 2.0 tran:trafficArea/TrafficArea and tran:auxiliaryTrafficArea/
    AuxiliaryTrafficArea → (:BoundarySurface:TrafficArea|AuxiliaryTrafficArea) via
    HAS_BOUNDARY {order, prop}; tran:lod0Network curves → HAS_LOD_MULTICURVE
    {lod:'0', role:'network', order}. Same graph pattern the 3.0 codec uses below a
    TrafficSpace (a 2.0 graph keeps the areas on the Road; no spaces are
    synthesised)."""
    for prop, kind in (("trafficArea", "TrafficArea"),
                       ("auxiliaryTrafficArea", "AuxiliaryTrafficArea")):
        for order, wrap in enumerate(el.findall(f"tran:{prop}", NS)):
            ta = wrap.find(f"tran:{kind}", NS)
            if ta is None:
                continue
            sid = ta.get(GML_ID) or f"bsurf_{_slug(feature_id)}_{prop.lower()}_{order}"
            gen_props, gen_map = _extract_gen_attrs(ta)
            props = {"id": sid, "surface_type": kind, "source": source,
                     "_gen_attrs_map": json.dumps(gen_map) if gen_map else "{}"}
            for nm, xp in (("gml_name", "gml:name"), ("gml_description", "gml:description"),
                           ("creation_date", "cg:creationDate"),
                           ("relative_to_terrain", "cg:relativeToTerrain"),
                           ("relative_to_water", "cg:relativeToWater")):
                v = _text(ta, xp)
                if v:
                    props[nm] = v
            env = ta.find("gml:boundedBy/gml:Envelope", NS)
            if env is not None:
                props["has_bbox_envelope"] = True
                lc = _text(env, "gml:lowerCorner"); uc = _text(env, "gml:upperCorner")
                if lc: props["bbox_lower_corner"] = lc
                if uc: props["bbox_upper_corner"] = uc
            for key, local in (("class", "class"), ("function", "function"),
                               ("usage", "usage"), ("surface_material", "surfaceMaterial")):
                vals = [(c.text.strip(), c.get("codeSpace"))
                        for c in ta.findall(f"tran:{local}", NS) if c.text and c.text.strip()]
                if vals:
                    props[key] = vals[0][0]
                    if vals[0][1]:
                        props[f"{key}_codespace"] = vals[0][1]
                    if len(vals) > 1:
                        props[f"{key}_list"] = [v for v, _ in vals]
            for lod in ("2", "3", "4"):
                ms_el = ta.find(f"tran:lod{lod}MultiSurface/gml:MultiSurface", NS)
                if ms_el is None:
                    continue
                props["lod"] = lod
                if ms_el.get(GML_ID):
                    props["multi_surface_id"] = ms_el.get(GML_ID)
                for p_idx, poly_el in enumerate(ms_el.findall(".//gml:Polygon", NS)):
                    pid = _collect_polygon(poly_el, source, batch)
                    batch["surf_poly_rels"].append(
                        {"src": sid, "tgt": pid, "order": p_idx,
                         "via_xlink": False, "orientation": None})
                break
            props.update(gen_props)
            _capture_common_extras(ta, props)
            batch["surfaces"].append(props)
            batch["surface_labels"].append({"id": sid, "label": kind})
            batch["child_rels"].append({"rel": "HAS_BOUNDARY", "src": feature_id, "tgt": sid,
                                        "props": {"order": order, "prop": prop}})
    n = 0
    for net in el.findall("tran:lod0Network", NS):
        for ls_el in net.iter(f"{{{NS['gml']}}}LineString"):
            pos_el = ls_el.find("gml:posList", NS)
            if pos_el is not None and pos_el.text:
                srs_dim = int(pos_el.get("srsDimension", "3"))
                pos = _reproject_poslist(pos_el.text.strip(), srs_dim)
            else:
                pts = [p.text.strip() for p in ls_el.findall("gml:pos", NS) if p.text]
                srs_dim, pos = 3, _reproject_poslist(" ".join(pts), 3)
            ls_id = ls_el.get(GML_ID) or f"{feature_id}_network_{n}"
            batch["linestrings"].append({"id": ls_id, "pos_list": pos,
                                         "srs_dimension": srs_dim, "source": source})
            batch["multicurve_rels3"].append({"src": feature_id, "tgt": ls_id, "lod": "0",
                                              "order": n, "multi_curve_id": None,
                                              "role": "network"})
            n += 1


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

    # Transportation sub-features (tran:trafficArea / auxiliaryTrafficArea) and the
    # lod0Network, previously dropped silently. Files without
    # them produce no extra rows, so their ingest is unchanged.
    if ns_prefix == "tran":
        _collect_tran_areas_v2(el, feature_id, source, batch)

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

# ---------------------------------------------------------------------------
# Source scope of gml:ids (GRAPH.md: a node belongs to exactly one source)
# ---------------------------------------------------------------------------
# Every node is MERGEd on its id. A file whose gml:ids already exist in the graph for
# ANOTHER source (an official building id in a reconstructed file, a template id used by
# two datasets) would otherwise add to and overwrite that source's nodes. A Dataset
# therefore lists the files ingested into it (source_files); a file may reuse only ids of
# nodes whose `source` is one of them. --id-prefix gives a source that reuses another
# source's ids its own id space; the prefix is recorded on the Dataset and the exporter
# strips it again.

class IdScopeError(SystemExit):
    """A file's gml:ids belong to another source; nothing of the file was written."""


_SCOPE_LABELS = ("Entity", "GeometryRing", "GeometryLineString", "TerrainIntersection")
_REF_TEXT_LOCALS = ("target", "ring")       # appearance references written as text URIs


def _claim_dataset_file(sess, dataset_id, source):
    """Add `source` to Dataset.source_files and return True when it was not listed yet.
    A Dataset ingested before the list existed is first given the sources of its members."""
    rec = sess.run("MATCH (d:Dataset {id: $id}) RETURN d.source_files IS NULL AS missing",
                   id=dataset_id).single()
    if rec is not None and rec["missing"]:
        init = sorted({r["s"] for r in sess.run(
            "MATCH (:Dataset {id: $id})<-[:PART_OF]-(n) WHERE n.source IS NOT NULL "
            "RETURN DISTINCT n.source AS s", id=dataset_id)})
        sess.run("MATCH (d:Dataset {id: $id}) WHERE d.source_files IS NULL "
                 "SET d.source_files = $init", id=dataset_id, init=init)
    rec = sess.run("MATCH (d:Dataset {id: $id}) WITH d, NOT $src IN d.source_files AS new "
                   "SET d.source_files = CASE WHEN new THEN d.source_files + $src "
                   "ELSE d.source_files END RETURN new", id=dataset_id, src=source).single()
    return bool(rec and rec["new"])


def _release_dataset_file(sess, dataset_id, source):
    sess.run("MATCH (d:Dataset {id: $id}) "
             "SET d.source_files = [f IN coalesce(d.source_files, []) WHERE f <> $src]",
             id=dataset_id, src=source)


def _record_id_prefix(sess, dataset_id, source, prefix):
    """The id prefix is a property of the Dataset: every file of it uses the same one."""
    rec = sess.run("MATCH (d:Dataset {id: $id}) RETURN d.id_prefix AS p, "
                   "[f IN coalesce(d.source_files, []) WHERE f <> $src] AS others",
                   id=dataset_id, src=source).single()
    have = rec["p"] if rec else None
    if (prefix or None) == (have or None):
        return
    if have:
        raise IdScopeError(f"ERROR: Dataset '{dataset_id}' uses --id-prefix '{have}'; "
                           f"ingest {source} with the same prefix.")
    if rec and rec["others"]:
        raise IdScopeError(f"ERROR: Dataset '{dataset_id}' already holds files without an id "
                           f"prefix; --id-prefix '{prefix}' cannot be added for {source} alone.")
    sess.run("MATCH (d:Dataset {id: $id}) SET d.id_prefix = $p", id=dataset_id, p=prefix)


def _foreign_ids(sess, dataset_id, ids):
    """Up to 20 nodes with one of `ids` whose `source` is not a file of the Dataset."""
    for label in _SCOPE_LABELS:
        hits = sess.run(
            f"MATCH (d:Dataset {{id: $did}}) UNWIND $ids AS i MATCH (n:{label} {{id: i}}) "
            "WHERE n.source IS NOT NULL AND NOT n.source IN coalesce(d.source_files, []) "
            "RETURN n.id AS id, n.source AS source LIMIT 20", did=dataset_id, ids=ids).data()
        if hits:
            return hits
    return []


def _check_id_scope(sess, gml_path, dataset_id, id_attr, prefix="", chunk=20000):
    """Pre-flight: refuse the file (before anything is written) when one of its gml:ids
    belongs to a node of another source. Skipped when the graph holds no other Dataset."""
    others = sess.run("MATCH (d:Dataset) WHERE d.id <> $id RETURN count(d) AS c",
                      id=dataset_id).single()["c"]
    if not others:
        return
    root = None
    ids: list = []

    def check():
        hits = _foreign_ids(sess, dataset_id, ids)
        ids.clear()
        if hits:
            ex = ", ".join(f"'{h['id']}' ({h['source']})" for h in hits[:3])
            raise IdScopeError(
                f"ERROR: gml:ids of {gml_path.name} already belong to another source, e.g. {ex}. "
                "Ingesting it would add to and overwrite that source's nodes. Give this source "
                "its own id space with --id-prefix <prefix> (recorded on the Dataset; the "
                "exporter strips it), or ingest the file into the Dataset that owns these ids.")

    for ev, el in etree.iterparse(str(gml_path), events=("start", "end"), huge_tree=True):
        if ev == "start":
            if root is None:
                root = el
                continue
            gid = el.get(id_attr)
            if gid:
                ids.append(prefix + gid)
                if len(ids) >= chunk:
                    check()
        elif el.getparent() is root:
            el.clear()
            while el.getprevious() is not None:
                del root[0]
    if ids:
        check()


def _prefix_ref(ref, prefix):
    """'#id' / 'tile.gml#id' → with the prefix on the fragment; external URLs unchanged."""
    if not ref or "#" not in ref:
        return ref
    doc, frag = ref.rsplit("#", 1)
    if not frag or "://" in doc:
        return ref
    return f"{doc}#{prefix}{frag}"


def _iter_prefixed(gml_path, tags, prefix, id_attr):
    """etree.iterparse(events=('end',), tag=tags) with --id-prefix applied on the fly: every
    gml:id and every document reference (xlink:href, appearance target / ring URIs) gets
    the prefix before any collector reads the element."""
    tagset = set(tags)
    root = None
    for ev, el in etree.iterparse(str(gml_path), events=("start", "end"), huge_tree=True):
        if not isinstance(el.tag, str):
            continue
        if ev == "start":
            if root is None:             # the CityModel keeps its own id (Dataset.gml_id)
                root = el
                continue
            gid = el.get(id_attr)
            if gid:
                el.set(id_attr, prefix + gid)
            href = el.get(XLINK_HREF)
            if href:
                el.set(XLINK_HREF, _prefix_ref(href, prefix))
            if el.get("ring"):
                el.set("ring", _prefix_ref(el.get("ring"), prefix))
            continue
        if etree.QName(el).localname in _REF_TEXT_LOCALS and el.text and "#" in el.text:
            el.text = _prefix_ref(el.text.strip(), prefix)
        if el.tag in tagset:
            yield ev, el


def _ref_doc(href, current):
    """The document a reference points into: the file part of 'tile.gml#id', or the
    current file for '#id'."""
    doc = href.rsplit("#", 1)[0] if href and "#" in href else ""
    return Path(doc.replace("\\", "/")).name if doc else current



def ingest(gml_path: Path, driver, num_writers: int = 3, nearby_edges: bool = True,
           dataset_id: str = None, dataset_name: str = None,
           target_crs: str = None, source_crs: str = None, id_registry: dict = None,
           skip_spatial_index: bool = False, defer_code_hubs: bool = False,
           source_type: str = None, method: str = None, method_version: str = None,
           rtree_layer: str = None, id_prefix: str = None, check_ids: bool = True,
           district: str = None):
    # Codec selection by the root namespace: CityGML 3.0 files go to the 3.0 codec
    # (ingest_citygml3); everything else runs the unchanged 2.0 path below.
    if detect_citygml_version(gml_path) == "3.0":
        return ingest_citygml3(gml_path, driver, num_writers=num_writers,
                               nearby_edges=nearby_edges, dataset_id=dataset_id,
                               dataset_name=dataset_name, target_crs=target_crs,
                               source_crs=source_crs, skip_spatial_index=skip_spatial_index,
                               defer_code_hubs=defer_code_hubs, source_type=source_type,
                               method=method, method_version=method_version,
                               rtree_layer=rtree_layer, id_prefix=id_prefix,
                               check_ids=check_ids, district=district)
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
    # '2.0' / '1.0': recorded on every feature-level node and on the Dataset
    cg_version = fmt.split(" ", 1)[1] if fmt.startswith("CityGML ") else None

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
            claimed = _claim_dataset_file(sess, dataset_id, source)
            try:
                _record_id_prefix(sess, dataset_id, source, id_prefix)
                if check_ids:
                    _check_id_scope(sess, gml_path, dataset_id, GML_ID, id_prefix or "")
            except IdScopeError:
                if claimed:
                    _release_dataset_file(sess, dataset_id, source)
                raise

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
                                _flush_batch(wsess, item, cg_version)
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

        events = (_iter_prefixed(gml_path, parse_tags, id_prefix, GML_ID) if id_prefix else
                  etree.iterparse(str(gml_path), events=("end",), tag=parse_tags))
        for _, el in events:
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
                progress("ingest.parse", done=buildings_ingested + objects_ingested,
                         unit="features", file=source, buildings=buildings_ingested)
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
        progress("ingest.parsed", done=buildings_ingested + objects_ingested,
                 total=buildings_ingested + objects_ingested, unit="features",
                 file=source, by_type=all_type_counts, message="post-processing")

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
            "n.citygml_version = coalesce(n.citygml_version, $cgv), "
            "n.crs = coalesce(n.crs, $crs), "
            "n.srs_dimension = coalesce(n.srs_dimension, $srs_dim), "
            "n.lod = coalesce(n.lod, $lod), "
            "n.source_crs = coalesce(n.source_crs, $scrs)",
            id=dataset_id, format=fmt, cgv=cg_version, crs=stored_crs, srs_dim=srs_dim, lod=lod,
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
        if district:
            district_name = district
            city_name = city_name if gemeinde_key else district
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
        # Opt-in provenance / own R-tree layer (only when the CLI options are given;
        # without them this block is a no-op and the 2.0 graph is unchanged).
        if source_type or method or method_version:
            prov = {k: v for k, v in (("source_type", source_type), ("method", method),
                                      ("method_version", method_version)) if v}
            # features, sub-features, surfaces and openings, as in the 3.0 codec; geometry
            # nodes carry only `source`
            _run_retry(sess, "MATCH (n:Entity) WHERE n.source = $src AND NOT (n:GeometryPolygon "
                             "OR n:GeometryMultiSurface OR n:GeometrySolid OR "
                             "n:GeometryCompositeSurface) SET n += $prov", src=source, prov=prov)
            _run_retry(sess, "MATCH (d:Dataset {id: $id}) SET d += $prov", id=dataset_id, prov=prov)
        if rtree_layer and rtree_layer != "features":
            _run_retry(sess, "MATCH (n) WHERE (n:Building OR n:CityObject) AND n.source = $src "
                             "SET n.rtree_layer = $layer", src=source, layer=rtree_layer)
        spatial_indexed = 0
        if skip_spatial_index:
            print("  Spatial R-tree indexing deferred to the final pass (tiled mode).")
        else:
            print("  Populating spatial R-tree index on bounding boxes …")
            progress("ingest.spatial_index", file=source, message="building spatial R-tree")
            spatial_ok = _ensure_spatial_layer(sess, rtree_layer or "features")
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
                spatial_indexed = _index_features_spatial(sess, rtree_layer or "features")
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


# ===========================================================================
# CityGML 3.0 codec
# ===========================================================================
# Chosen by ingest() from the root namespace (citygml_profiles.detect_citygml_version).
# It writes the SAME version-neutral graph model as the 2.0 codec — Building pattern,
# BoundarySurface → GeometryPolygon → GeometryRing chain, Entity plumbing label,
# `source` on every node — plus the 3.0 concepts documented in GRAPH.md/CITYGML3.md:
# transportation spaces (Section/Intersection/TrafficSpace/…), markings, holes,
# clearance spaces, CityObjectRelations, implicit-geometry prototypes, appearances and
# pending cross-file references. Qualified names come from the profile (PROFILE_V3);
# nothing is namespace-wildcarded. Sub-features never get the CityObject label nor
# PART_OF Dataset (BuildingPart precedent).
#
# Merge policy (street-space tiles): features MERGE on gml:id. The same Road / Section
# / TrafficSpace gml:id may occur in many tile files with different children; every
# file adds its children (MERGE on deterministic ids and edge keys), the generic
# attribute map is unioned, the bbox is unioned, `source` is the last file and
# `source_files` lists all files. No id splitting (the 2.0 _resolve_feature_id tiers
# are not used: within one 3.0 file gml:id is an xs:ID and therefore unique).

from collections import Counter as _Counter, defaultdict as _defaultdict  # noqa: E402

from citygml_profiles import (PROFILE_V3 as _P3, detect_citygml_version,  # noqa: E402
                              href_fragment)

NS3 = dict(_P3.ns)
_GML3 = NS3["gml"]
GML3_ID = _P3.gml_id
_KNOWN_NS3 = _P3.known_namespaces()
_POSLIST3 = f"{{{_GML3}}}posList"
_POS3 = f"{{{_GML3}}}pos"
_RELGEOM3 = _P3.q("core", _P3.implicit_geometry_local)
_LOD_PROP_RE = re.compile(
    r"^lod([0-3])(Point|MultiSurface|MultiCurve|Solid|TerrainIntersectionCurve|"
    r"ImplicitRepresentation)$")
_SAFE_LABELS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?::[A-Za-z][A-Za-z0-9]*)*$")
_SAFE_REL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_V3_BATCH_POLYGONS = 20000   # flush a batch early when one huge feature (a Road) is big

# class/function/usage (all 3.0 modules: class 0..1, function/usage 0..*)
_CFU3 = [("class", "class", "code"), ("function", "function", "code"),
         ("usage", "usage", "code")]

# Top-level city objects: Clark tag → (feature_type, module prefix, kind, module attrs)
_V3_TOP = {_P3.q(p, l): v for (p, l), v in {
    ("tran", "Road"):      ("Road", "tran", "transport", _CFU3),
    ("tran", "Railway"):   ("Railway", "tran", "transport", _CFU3),
    ("tran", "Track"):     ("Track", "tran", "transport", _CFU3),
    ("tran", "Square"):    ("Square", "tran", "transport", _CFU3),
    ("tran", "Waterway"):  ("Waterway", "tran", "transport", _CFU3),
    ("frn", "CityFurniture"): ("CityFurniture", "frn", "space", _CFU3),
    ("veg", "SolitaryVegetationObject"): ("SolitaryVegetationObject", "veg", "space", _CFU3 + [
        ("species", "species", "code"), ("height", "height", "measure"),
        ("trunk_diameter", "trunkDiameter", "measure"),
        ("crown_diameter", "crownDiameter", "measure"),
        ("root_ball_diameter", "rootBallDiameter", "measure"),
        ("max_root_ball_depth", "maxRootBallDepth", "measure")]),
    ("veg", "PlantCover"): ("PlantCover", "veg", "space", _CFU3 + [
        ("average_height", "averageHeight", "measure"),
        ("min_height", "minHeight", "measure"), ("max_height", "maxHeight", "measure")]),
    ("con", "OtherConstruction"): ("OtherConstruction", "con", "construction", _CFU3),
    ("brid", "Bridge"):    ("Bridge", "brid", "bridge", _CFU3 + [("is_movable", "isMovable", "text")]),
    ("wtr", "WaterBody"):  ("WaterBody", "wtr", "space", _CFU3),
    ("luse", "LandUse"):   ("LandUse", "luse", "thematic", _CFU3),
    ("gen", "GenericOccupiedSpace"):   ("GenericOccupiedSpace", "gen", "space", _CFU3),
    ("gen", "GenericUnoccupiedSpace"): ("GenericUnoccupiedSpace", "gen", "space", _CFU3),
    ("gen", "GenericLogicalSpace"):    ("GenericLogicalSpace", "gen", "space", _CFU3),
    ("gen", "GenericThematicSurface"): ("GenericThematicSurface", "gen", "thematic", _CFU3),
}.items()}
_V3_BUILDING_TAG = _P3.q("bldg", "Building")

# Sub-features: local name → (module prefix, module attrs, Neo4j labels)
_V3_SUB = {
    "Section":      ("tran", [("class", "class", "code")], "Section:TransportationSpace"),
    "Intersection": ("tran", [("class", "class", "code")], "Intersection:TransportationSpace"),
    "TrafficSpace": ("tran", _CFU3 + [("granularity", "granularity", "text"),
                                      ("traffic_direction", "trafficDirection", "text")],
                     "TrafficSpace"),
    "AuxiliaryTrafficSpace": ("tran", _CFU3 + [("granularity", "granularity", "text")],
                              "AuxiliaryTrafficSpace"),
    "Hole":           ("tran", [("class", "class", "code")], "Hole"),
    "ClearanceSpace": ("tran", [("class", "class", "code")], "ClearanceSpace"),
    "Marking":        ("tran", [("class", "class", "code")], "Marking"),
}
# Thematic-surface module attributes: surface_type → (prefix, attrs)
_V3_SURF_ATTRS = {
    "TrafficArea": ("tran", _CFU3 + [("surface_material", "surfaceMaterial", "code")]),
    "AuxiliaryTrafficArea": ("tran", _CFU3 + [("surface_material", "surfaceMaterial", "code")]),
    "HoleSurface": ("tran", []),
    "WaterSurface": ("wtr", [("water_level", "waterLevel", "code")]),
    "GenericThematicSurface": ("gen", _CFU3),
}
_V3_SURF_EXTRA_LABEL = {"TrafficArea", "AuxiliaryTrafficArea", "HoleSurface"}
_GEN3_TYPES = {"StringAttribute": "string", "IntAttribute": "int", "DoubleAttribute": "double",
               "DateAttribute": "date", "UriAttribute": "uri", "MeasureAttribute": "measure",
               "CodeAttribute": "code"}

# Deferred (xlink / cross-file) edges: rel type → MERGE key properties.
_V3_DEFER_KEYS = {
    "HAS_SURFACE_MEMBER": ("order",), "HAS_POLYGON": ("order",), "HAS_SOLID_MEMBER": ("order",),
    "HAS_LOD_SURFACE": ("lod",), "HAS_LOD_SOLID": (), "HAS_PROTOTYPE": ("lod",),
    "PREDECESSOR": ("order",), "SUCCESSOR": ("order",), "RELATED_TO": ("order",),
    "TARGETS": ("order",), "HAS_BOUNDARY": (), "HAS_TRAFFIC_SPACE": (), "HAS_SECTION": (),
    "HAS_INTERSECTION": (), "HAS_MARKING": (), "HAS_HOLE": (), "HAS_CLEARANCE_SPACE": (),
    "HAS_SURFACE_DATA": (), "HAS_BUILDING_PART": (), "HAS_BRIDGE_PART": (), "HAS_DYNAMIZER": (),
    "HAS_BUILDING_UNIT": ("order",), "HAS_STOREY": ("order",),
}
_V3_REL_KEYS = ("child_rels", "proto_rels", "solid_member_rels", "line_rels",
                "multicurve_rels3", "point_rels", "appearance_rels", "surface_data_rels")
_V3_INDEX_LABELS = ("Section", "Intersection", "TransportationSpace", "TrafficSpace",
                    "AuxiliaryTrafficSpace", "Hole", "ClearanceSpace", "Marking", "BridgePart",
                    "GeometryPrototype", "Appearance", "X3DMaterial", "ParameterizedTexture",
                    "GeoreferencedTexture", "NestedFeature", "Opening", "Room",
                    "BuildingInstallation", "BuildingFurniture", "BuildingSubdivision",
                    "PointCloud", "GeometryCompositeSurface", "Dynamizer")


class _Ctx3:
    """Per-file state of the 3.0 codec."""

    def __init__(self, source, dataset_id, prov, rtree_layer, id_prefix=""):
        self.source = source
        self.dataset_id = dataset_id
        self.id_prefix = id_prefix or ""   # --id-prefix (already applied to ids and refs)
        self.prov = prov                   # {source_type, method, method_version} (non-null)
        self.rtree_layer = rtree_layer
        self.local_ids: set = set()        # gml:ids inside the current top-level feature
        self.el_bbox: dict = {}            # element → subtree bbox (current feature)
        self.deferred: list = []           # xlink / cross-file edges, resolved post-parse
        self.implicit_jobs: list = []      # (feature_id, matrix, ref point, prototype id)
        self.proto_bbox: dict = {}         # prototype id → local (min xyz, max xyz)
        self.top_ids: list = []
        self.building_ids: list = []
        self.dataset_appearances: list = []
        self.ds_bbox = [float("inf")] * 3 + [float("-inf")] * 3
        self.max_lod = -1
        self.stats = _Counter()
        self.unsupported = _Counter()
        self.gemeinde_key = ""


def _ln3(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _t3(el, xpath):
    found = el.find(xpath, NS3)
    return found.text.strip() if found is not None and found.text else None


def _first_child3(el):
    for c in el:
        if isinstance(c.tag, str):
            return c
    return None


def _hash10(el):
    # without the tail: the whitespace after an element must not change the id
    return hashlib.sha1(etree.tostring(el, with_tail=False)).hexdigest()[:10]


def _codes3(el, prefix, local):
    out = []
    for c in el.findall(f"{prefix}:{local}", NS3):
        if c.text is not None and c.text.strip() != "":
            out.append((c.text.strip(), c.get("codeSpace")))
    return out


def _set_codes3(props, key, values):
    """Coded values (gml:CodeType, 0..* in 3.0): the first value stays the queryable
    scalar `key` (+`key_codespace`), exactly like the 2.0 model; with ≥2 values the full
    ordered list is kept in `key_list` (and `key_codespace_list` if codeSpaces differ)."""
    if not values:
        return
    props[key] = values[0][0]
    if values[0][1]:
        props[f"{key}_codespace"] = values[0][1]
    if len(values) > 1:
        props[f"{key}_list"] = [v for v, _ in values]
        css = [cs or "" for _, cs in values]
        if any(c != css[0] for c in css):
            props[f"{key}_codespace_list"] = css


def _cfu_codes3(el, prefix, props):
    """class/function/usage of a building-module feature (Building, BuildingPart, Storey,
    BuildingUnit, BuildingRoom, BuildingInstallation, BuildingFurniture) → the 2.0 keys
    class_code / function_code / usage_code (+ class_codespace …); further values in
    <key>_list and, when their codeSpaces differ, <name>_codespace_list."""
    for name in ("class", "function", "usage"):
        vals = _codes3(el, prefix, name)
        if not vals:
            continue
        key = f"{name}_code"
        props[key] = vals[0][0]
        if vals[0][1]:
            props[f"{name}_codespace"] = vals[0][1]
        if len(vals) > 1:
            props[f"{key}_list"] = [v for v, _ in vals]
            css = [cs or "" for _, cs in vals]
            if any(c != css[0] for c in css):
                props[f"{name}_codespace_list"] = css


def _module_attrs3(el, prefix, specs, props):
    for key, local, kind in specs:
        if kind == "code":
            _set_codes3(props, key, _codes3(el, prefix, local))
        elif kind == "measure":
            e = el.find(f"{prefix}:{local}", NS3)
            if e is not None and e.text and e.text.strip():
                props[key] = e.text.strip()
                if e.get("uom"):
                    props[f"{key}_uom"] = e.get("uom")
        else:
            v = _t3(el, f"{prefix}:{local}")
            if v is not None:
                props[key] = v


def _gen_value3(text):
    # keep values verbatim (some sources blank-pad values to a fixed width);
    # only multi-line (pretty-printed) values are stripped
    if text is None:
        return ""
    return text.strip() if "\n" in text else text


def _extract_gen_attrs3(el):
    """3.0 generic attributes (core:genericAttribute/gen:<Type>Attribute{gen:name,
    gen:value}, gen:GenericAttributeSet{gen:name, gen:codeSpace?, gen:genericAttribute+})
    → the SAME storage as 2.0: flat string props under _attr_key(name) + _gen_attrs_map
    {key: {name, type, set?, codeSpace?}}. Type 'code' is new (codeSpace in the map);
    measures add <key>_uom. Keys colliding with RESERVED_NODE_KEYS become gen_<key>."""
    props, gen_map = {}, {}

    def _key(name):
        key = _attr_key(name)
        if key in RESERVED_NODE_KEYS:
            base, n = f"gen_{key}", 2
            key = base
            while key in RESERVED_NODE_KEYS or key in props:
                key = f"{base}_{n}"
                n += 1
        elif key in props:
            base, n = key, 2
            while key in props:
                key = f"{base}_{n}"
                n += 1
        return key

    def _walk(container, wrapper_tag, set_path):
        set_i = 0
        for ga in container.findall(wrapper_tag, NS3):
            for child in ga:
                ln = _ln3(child)
                if ln in _GEN3_TYPES:
                    name_el = child.find("gen:name", NS3)
                    val_el = child.find("gen:value", NS3)
                    name = name_el.text.strip() if name_el is not None and name_el.text else ""
                    if not name or val_el is None:
                        continue
                    key = _key(name)
                    props[key] = _gen_value3(val_el.text)
                    meta = {"name": name, "type": _GEN3_TYPES[ln]}
                    if set_path:
                        meta["set"] = set_path
                    if ln == "MeasureAttribute" and val_el.get("uom"):
                        props[f"{key}_uom"] = val_el.get("uom")
                    if ln == "CodeAttribute" and val_el.get("codeSpace"):
                        meta["codeSpace"] = val_el.get("codeSpace")
                    gen_map[key] = meta
                elif ln == "GenericAttributeSet":
                    entry = {"name": (child.findtext("gen:name", namespaces=NS3) or "").strip(),
                             "i": set_i}
                    cs = child.findtext("gen:codeSpace", namespaces=NS3)
                    if cs and cs.strip():
                        entry["codeSpace"] = cs.strip()
                    set_i += 1
                    _walk(child, "gen:genericAttribute", set_path + [entry])

    _walk(el, "core:genericAttribute", [])
    return props, gen_map


def _capture_common_extras3(el, props):
    """3.0 counterpart of _capture_common_extras: lifespan scalars, verbatim
    gml:metaDataProperty / gml:location / core:generalizesTo / core:externalReference
    fragments (self-describing: they carry their 3.0 namespace, so an exporter knows
    their version), ADE content (unknown namespace or adeOf* hooks) → ade_xml."""
    for key, xp in (("termination_date", "core:terminationDate"),
                    ("valid_from", "core:validFrom"), ("valid_to", "core:validTo")):
        v = _t3(el, xp)
        if v:
            props[key] = v

    def _frags(matches):
        return [etree.tostring(m, encoding="unicode") for m in matches]

    meta = _frags(el.findall("gml:metaDataProperty", NS3))
    if meta:
        props["metadata_xml"] = json.dumps(meta)
    loc = el.find("gml:location", NS3)
    if loc is not None:
        props["location_xml"] = etree.tostring(loc, encoding="unicode")
    gens = _frags(el.findall("core:generalizesTo", NS3))
    if gens:
        props["generalizes_to_xml"] = json.dumps(gens)
    ext = el.findall("core:externalReference", NS3)
    if ext:
        props["external_reference_xml"] = json.dumps(_frags(ext))
        er = ext[0].find("core:ExternalReference", NS3)
        if er is not None:
            info = _t3(er, "core:informationSystem")
            tgt = _t3(er, "core:targetResource")
            if info:
                props["external_information_system"] = info
            if tgt:
                props["external_object_name"] = tgt
    ade = []
    for child in el:
        tag = child.tag
        if not isinstance(tag, str) or not tag.startswith("{"):
            continue
        uri = tag[1:tag.index("}")]
        if uri not in _KNOWN_NS3 or etree.QName(child).localname.startswith("adeOf"):
            ade.append(etree.tostring(child, encoding="unicode"))
    if ade:
        props["ade_xml"] = json.dumps(ade)


def _semantic_props3(el, node_id, props, ctx, batch, top_level=False):
    """AbstractGML/Feature/CityObject/Space content shared by every 3.0 feature,
    sub-feature and thematic surface (names, dates, envelope, generic attributes,
    lifespan/extras, space type, qualified volumes/areas, relations, appearances)."""
    name = _t3(el, "gml:name")
    if name:
        props["gml_name"] = name
    desc = _t3(el, "gml:description")
    if desc:
        props["gml_description"] = desc
    ident = el.find("gml:identifier", NS3)
    if ident is not None and ident.text:
        props["gml_identifier"] = ident.text.strip()
        if ident.get("codeSpace"):
            props["gml_identifier_codespace"] = ident.get("codeSpace")
    for key, xp in (("creation_date", "core:creationDate"),
                    ("relative_to_terrain", "core:relativeToTerrain"),
                    ("relative_to_water", "core:relativeToWater"),
                    ("space_type", "core:spaceType")):
        v = _t3(el, xp)
        if v:
            props[key] = v
    env = el.find("gml:boundedBy/gml:Envelope", NS3)
    if env is not None:
        lc = _t3(env, "gml:lowerCorner")
        uc = _t3(env, "gml:upperCorner")
        props["has_bbox_envelope"] = True
        if lc:
            props["bbox_lower_corner"] = lc
        if uc:
            props["bbox_upper_corner"] = uc
    elif top_level:
        props["has_bbox_envelope"] = False
    for key, xp in (("volume_xml", "core:volume"), ("area_xml", "core:area")):
        frs = el.findall(xp, NS3)
        if frs:
            props[key] = json.dumps([etree.tostring(f, encoding="unicode") for f in frs])
    gen_props, gen_map = _extract_gen_attrs3(el)
    props.update(gen_props)
    props["_gen_attrs_map"] = json.dumps(gen_map) if gen_map else "{}"
    _capture_common_extras3(el, props)
    _collect_relations3(el, node_id, ctx)
    _collect_dynamizers3(el, node_id, ctx, batch)
    for i, ap in enumerate(el.findall("core:appearance", NS3)):
        if ap.get(XLINK_HREF):
            _defer3(ctx, "HAS_APPEARANCE_REF", node_id, ap.get(XLINK_HREF), {}, {"order": i})
            continue
        app_el = _first_child3(ap)
        if app_el is not None:
            aid = _collect_appearance3(app_el, f"{node_id}_app_{i}", ctx.source, batch, ctx)
            batch["appearance_rels"].append({"src": node_id, "tgt": aid, "order": i})


def _occupancy3(el, prefix):
    out = []
    for o in el.findall(f"{prefix}:occupancy/core:Occupancy", NS3):
        d = {"number_of_occupants": _t3(o, "core:numberOfOccupants")}
        for key, local in (("interval", "interval"), ("occupant_type", "occupantType")):
            e = o.find(f"core:{local}", NS3)
            if e is not None and e.text:
                d[key] = e.text.strip()
                if e.get("codeSpace"):
                    d[f"{key}_codespace"] = e.get("codeSpace")
        out.append(d)
    return json.dumps(out) if out else None


def _elevations3(el, prefix):
    """<prefix>:elevation/con:Elevation (con: on constructions, bldg: on storeys and
    building units) → JSON list of {reference, reference_codespace, value, srs_dimension,
    srs_name?, axis_labels?, uom_labels?} (the value is a gml:DirectPosition)."""
    elevs = []
    for e in el.findall(f"{prefix}:elevation/con:Elevation", NS3):
        ref = e.find("con:elevationReference", NS3)
        val = e.find("con:elevationValue", NS3)
        d = {"reference": ref.text.strip() if ref is not None and ref.text else None,
             "reference_codespace": ref.get("codeSpace") if ref is not None else None,
             "value": val.text.strip() if val is not None and val.text else None,
             "srs_dimension": val.get("srsDimension") if val is not None else None}
        for key, attr in (("srs_name", "srsName"), ("axis_labels", "axisLabels"),
                          ("uom_labels", "uomLabels")):
            if val is not None and val.get(attr):
                d[key] = val.get(attr)
        elevs.append(d)
    return json.dumps(elevs) if elevs else None


def _construction_props3(el, props, ctx):
    """con:AbstractConstruction block → version-neutral scalars. con:height/con:Height:
    measured_height (+_uom) is the Height with status 'measured' (else the first),
    with height_high_reference / height_low_reference / height_status; all Heights are
    kept verbatim in heights_json. dateOfConstruction/Demolition are
    kept verbatim and their year becomes year_of_construction/demolition (2.0 keys)."""
    v = _t3(el, "con:conditionOfConstruction")
    if v:
        props["condition_of_construction"] = v
    for key, ykey, xp in (("date_of_construction", "year_of_construction", "con:dateOfConstruction"),
                          ("date_of_demolition", "year_of_demolition", "con:dateOfDemolition")):
        d = _t3(el, xp)
        if d:
            props[key] = d
            m = re.match(r"^(-?\d{4})", d)
            if m:
                props[ykey] = m.group(1)
    heights = []
    for h in el.findall("con:height/con:Height", NS3):
        entry = {}
        for key, local in (("high_reference", "highReference"),
                           ("low_reference", "lowReference"), ("status", "status")):
            e = h.find(f"con:{local}", NS3)
            if e is not None and e.text:
                entry[key] = e.text.strip()
                if e.get("codeSpace"):
                    entry[f"{key}_codespace"] = e.get("codeSpace")
        val = h.find("con:value", NS3)
        if val is not None and val.text:
            entry["value"] = val.text.strip()
            if val.get("uom"):
                entry["uom"] = val.get("uom")
        heights.append(entry)
    if heights:
        pick = next((x for x in heights if x.get("status") == "measured"), heights[0])
        try:
            props["measured_height"] = float(pick.get("value"))
        except (TypeError, ValueError):
            pass
        if pick.get("uom"):
            props["measured_height_uom"] = pick["uom"]
        for key in ("high_reference", "low_reference", "status"):
            if pick.get(key):
                props[f"height_{key}"] = pick[key]
        # all Heights verbatim (value text kept exactly) for lossless re-export
        props["heights_json"] = json.dumps(heights)
    elevs = _elevations3(el, "con")
    if elevs:
        props["elevations_json"] = elevs
    occ = _occupancy3(el, "con")
    if occ:
        props["occupancy_json"] = occ
    if el.find("con:constructionEvent", NS3) is not None:
        ctx.unsupported["con:constructionEvent"] += len(el.findall("con:constructionEvent", NS3))


# ── deferred (xlink / cross-file) references ─────────────────────────────────

def _defer3(ctx, rel, src, href, key, props):
    tgt = href_fragment(href)
    if not tgt:
        return
    p = dict(props)
    if href and href != f"#{tgt}":
        p["href"] = href          # cross-document reference: keep it verbatim
    # `doc`: the target must come from the referenced document (the current file for
    # '#id'); `tgt0`: the fragment without this Dataset's --id-prefix, for a reference into
    # a Dataset with another (or no) prefix
    row = {"rel": rel, "src": src, "tgt": tgt, "key": dict(key), "props": p,
           "doc": _ref_doc(href, ctx.source)}
    if ctx.id_prefix and tgt.startswith(ctx.id_prefix):
        row["tgt0"] = tgt[len(ctx.id_prefix):]
    ctx.deferred.append(row)


def _collect_relations3(el, src_id, ctx):
    """core:relatedTo/core:CityObjectRelation → (src)-[:RELATED_TO {order,
    relation_type, relation_type_codespace, relation_id, gen_attrs_json, href?}]->(tgt),
    deferred: targets may come later in the file or live in another tile file."""
    for i, rt in enumerate(el.findall("core:relatedTo", NS3)):
        if rt.get(XLINK_HREF):
            ctx.unsupported["core:relatedTo given by reference"] += 1
            continue
        cor = rt.find("core:CityObjectRelation", NS3)
        if cor is None:
            continue
        tgt_el = cor.find("core:relatedTo", NS3)
        href = tgt_el.get(XLINK_HREF) if tgt_el is not None else None
        if not href:
            continue
        props = {"order": i, "source": ctx.source}
        rtype = cor.find("core:relationType", NS3)
        if rtype is not None and rtype.text:
            props["relation_type"] = rtype.text.strip()
            if rtype.get("codeSpace"):
                props["relation_type_codespace"] = rtype.get("codeSpace")
        if cor.get(GML3_ID):
            props["relation_id"] = cor.get(GML3_ID)
        gp, gm = _extract_gen_attrs3(cor)
        if gm:
            props["gen_attrs_json"] = json.dumps({"props": gp, "map": gm})
        _defer3(ctx, "RELATED_TO", src_id, href, {"order": i}, props)
        ctx.stats["relations"] += 1


# SensorConnection children → Dynamizer node keys (CityGML 3.0 dynamizer module)
_SENSOR_FIELDS3 = (("connection_type", "connectionType"), ("observation_property", "observationProperty"),
                   ("uom", "uom"), ("sensor_id", "sensorID"), ("sensor_name", "sensorName"),
                   ("observation_id", "observationID"), ("datastream_id", "datastreamID"),
                   ("base_url", "baseURL"), ("auth_type", "authType"), ("mqtt_server", "mqttServer"),
                   ("mqtt_topic", "mqttTopic"), ("link_to_observation", "linkToObservation"),
                   ("link_to_sensor_description", "linkToSensorDescription"))


_ATTR_NAME3 = re.compile(r"\[gen:name='([^']+)'\]/gen:value\s*$")


def _collect_dynamizers3(el, owner_id, ctx, batch):
    """core:dynamizer/dyn:Dynamizer → (owner)-[:HAS_DYNAMIZER {order}]->(:Dynamizer) — a
    sub-feature (no CityObject label, no PART_OF). attributeRef, start/end time and the
    SensorConnection fields become scalars (sensor_location = the referenced gml:id);
    dynamicData (a timeseries) is kept verbatim in dynamic_data_xml with its type,
    first/last timestamp, observation property, uom, value type and value count as query
    scalars. Live values written later by a live job use the live_* prefix."""
    for i, dp in enumerate(el.findall("core:dynamizer", NS3)):
        if dp.get(XLINK_HREF):
            _defer3(ctx, "HAS_DYNAMIZER", owner_id, dp.get(XLINK_HREF), {}, {"order": i})
            continue
        d = _first_child3(dp)
        if d is None:
            continue
        if _ln3(d) != "Dynamizer":
            ctx.unsupported[f"core:dynamizer {_ln3(d)}"] += 1
            continue
        did = d.get(GML3_ID)
        props = {"id": did or f"{owner_id}_dyn_{i}", "source": ctx.source, "feature_type": "Dynamizer",
                 **ctx.prov}
        if not did:
            props["synthetic_id"] = True
        for key, xp in (("gml_name", "gml:name"), ("gml_description", "gml:description"),
                        ("creation_date", "core:creationDate"), ("termination_date", "core:terminationDate"),
                        ("valid_from", "core:validFrom"), ("valid_to", "core:validTo"),
                        ("attribute_ref", "dyn:attributeRef"), ("start_time", "dyn:startTime"),
                        ("end_time", "dyn:endTime")):
            v = _t3(d, xp)
            if v:
                props[key] = v
        m_attr = _ATTR_NAME3.search(props.get("attribute_ref") or "")
        if m_attr:                                  # query convenience: the overridden attribute
            props["attribute_name"] = m_attr.group(1)
        sc = d.find("dyn:sensorConnection/dyn:SensorConnection", NS3)
        if sc is not None:
            for key, local in _SENSOR_FIELDS3:
                e = sc.find(f"dyn:{local}", NS3)
                if e is not None and e.text and e.text.strip():
                    props[key] = e.text.strip()
                    if e.get("codeSpace"):
                        props[f"{key}_codespace"] = e.get("codeSpace")
            loc = sc.find("dyn:sensorLocation", NS3)
            if loc is not None and loc.get(XLINK_HREF):
                props["sensor_location"] = href_fragment(loc.get(XLINK_HREF))
                props["sensor_location_href"] = loc.get(XLINK_HREF)
        dd = d.find("dyn:dynamicData", NS3)
        ts = _first_child3(dd) if dd is not None else None
        if ts is not None:
            props["dynamic_data_type"] = _ln3(ts)
            props["dynamic_data_xml"] = etree.tostring(ts, encoding="unicode")
            for key, xp in (("first_timestamp", "dyn:firstTimestamp"), ("last_timestamp", "dyn:lastTimestamp"),
                            ("ts_observation_property", "dyn:observationProperty"), ("ts_uom", "dyn:uom"),
                            ("value_type", "dyn:valueType")):
                v = _t3(ts, xp)
                if v:
                    props[key] = v
            n_pairs = len(ts.findall("dyn:timeValuePair", NS3))
            if n_pairs:
                props["n_values"] = n_pairs
        ade = [etree.tostring(c, encoding="unicode") for c in d
               if isinstance(c.tag, str) and _ln3(c).startswith("adeOf")]
        if ade:
            props["ade_xml"] = json.dumps(ade)
        batch["sub_features"].append({"labels": "Dynamizer", "props": props})
        batch["child_rels"].append({"rel": "HAS_DYNAMIZER", "src": owner_id, "tgt": props["id"],
                                    "props": {"order": i}})
        ctx.stats["sub:Dynamizer"] += 1


# ── geometry (GML 3.2) ───────────────────────────────────────────────────────

def _dim_of(pos_el, n_values):
    """srsDimension of a posList / pos: its own, else the nearest geometry ancestor's, else
    the file's (root Envelope), and only then guessed from the value count."""
    el, depth = pos_el, 0
    while el is not None and depth < 6:
        d = el.get("srsDimension")
        if d:
            try:
                return int(d)
            except ValueError:
                break
        el, depth = el.getparent(), depth + 1
    root_dim = getattr(_tls, "srs_dim3", None)
    if root_dim in (2, 3) and n_values % root_dim == 0:
        return root_dim
    if n_values % 3 == 0:
        return 3
    return 2 if n_values % 2 == 0 else 3


def _coords3(holder, reproject=True):
    """(pos_list, srs_dimension) of a LinearRing / LineString (posList or pos*)."""
    pos_el = holder.find("gml:posList", NS3)
    if pos_el is not None and pos_el.text:
        txt = pos_el.text.strip()
        dim = _dim_of(pos_el, len(txt.split()))
        return (_reproject_poslist(txt, dim) if reproject else txt), dim
    pos_els = holder.findall("gml:pos", NS3)
    if pos_els:
        txt = " ".join(p.text.strip() for p in pos_els if p.text and p.text.strip())
        dim = _dim_of(pos_els[0], len(pos_els[0].text.split()) if pos_els[0].text else 3)
        return (_reproject_poslist(txt, dim) if reproject else txt), dim
    return "", 3


def _collect_polygon3(poly_el, fallback_id, source, batch, reproject=True, patch=None):
    pid = poly_el.get(GML3_ID)
    extra = {}
    if not pid:
        pid = fallback_id
        extra["synthetic_id"] = True
    if poly_el.get("srsName"):
        extra["srs_name"] = poly_el.get("srsName")
    if patch:
        extra["patch_type"] = patch
    batch["polygons"].append({"id": pid, "source": source})
    if extra:
        batch["polygon_props"].append({"id": pid, "props": extra})
    ring_el = poly_el.find("gml:exterior/gml:LinearRing", NS3)
    if ring_el is not None:
        pos_list, dim = _coords3(ring_el, reproject)
        rid = ring_el.get(GML3_ID)
        row = {"id": rid or f"ring_ext_{pid}", "pos_list": pos_list, "srs_dimension": dim,
               "ring_type": "exterior", "source": source}
        if not rid:
            row["synthetic_id"] = True
        batch["rings"].append(row)
        batch["poly_ext_ring_rels"].append({"src": pid, "tgt": row["id"]})
    for i, int_ring in enumerate(poly_el.findall("gml:interior/gml:LinearRing", NS3)):
        pos_list, dim = _coords3(int_ring, reproject)
        rid = int_ring.get(GML3_ID)
        row = {"id": rid or f"ring_int_{pid}_{i}", "pos_list": pos_list, "srs_dimension": dim,
               "ring_type": "interior", "source": source}
        if not rid:
            row["synthetic_id"] = True
        batch["rings"].append(row)
        batch["poly_int_ring_rels"].append({"src": pid, "tgt": row["id"], "ring_index": i})
    return pid


def _member_rel3(batch, ctx, rel, row, href=None):
    """Route one member edge: xlinks whose target is not inside the current top-level
    feature go to the deferred list (their target may be flushed by another writer
    batch, later in the file, or in another tile file)."""
    if row.get("via_xlink") and row["tgt"] not in ctx.local_ids:
        _defer3(ctx, rel, row["src"], href or f"#{row['tgt']}", {"order": row["order"]},
                {"via_xlink": True, "orientation": row.get("orientation")})
        return
    key = "surf_poly_rels" if rel == "HAS_POLYGON" else "surface_member_rels"
    batch[key].append(row)


def _record_member3(sm_el, container_id, order, source, batch, ctx, rel="HAS_SURFACE_MEMBER",
                    orientation=None, reproject=True):
    href = sm_el.get(XLINK_HREF)
    if href:
        _member_rel3(batch, ctx, rel, {"src": container_id, "tgt": href_fragment(href),
                                       "order": order, "via_xlink": True,
                                       "orientation": orientation}, href)
        return
    child = _first_child3(sm_el)
    if child is None:
        return
    ln = _ln3(child)
    if ln == "OrientableSurface":
        base = child.find("gml:baseSurface", NS3)
        if base is not None:
            _record_member3(base, container_id, order, source, batch, ctx, rel,
                            orientation=child.get("orientation", "+"), reproject=reproject)
        return
    if ln == "Polygon":
        pid = _collect_polygon3(child, f"{container_id}_poly_{order}", source, batch, reproject)
        _member_rel3(batch, ctx, rel, {"src": container_id, "tgt": pid, "order": order,
                                       "via_xlink": False, "orientation": orientation})
        return
    if ln == "CompositeSurface" and rel == "HAS_SURFACE_MEMBER":
        cs_id = child.get(GML3_ID)
        row = {"id": cs_id or f"{container_id}_cs_{order}", "source": source}
        if not cs_id:
            row["synthetic_id"] = True
        batch["composite_surfaces"].append(row)
        for i, m in enumerate(child.findall("gml:surfaceMember", NS3)):
            _record_member3(m, row["id"], i, source, batch, ctx, reproject=reproject)
        batch["surface_member_rels"].append({"src": container_id, "tgt": row["id"],
                                             "order": order, "via_xlink": False,
                                             "orientation": orientation})
        return
    ctx.unsupported[f"surface member gml:{ln}"] += 1


def _collect_ms3(ms_el, ms_id, lod, source, batch, ctx, synthetic=False, reproject=True):
    props = {"id": ms_id, "lod": lod, "source": source}
    if synthetic:
        props["synthetic_id"] = True
    if ms_el.get("srsName"):
        props["srs_name"] = ms_el.get("srsName")
    batch["multi_surfaces"].append(props)
    for i, sm in enumerate(ms_el.findall("gml:surfaceMember", NS3)):
        _record_member3(sm, ms_id, i, source, batch, ctx, reproject=reproject)
    if ms_el.find("gml:surfaceMembers", NS3) is not None:
        ctx.unsupported["gml:surfaceMembers (array property)"] += 1
    ctx.stats["multi_surfaces"] += 1
    return ms_id


def _collect_solid3(solid_el, solid_id, lod, source, batch, ctx, synthetic=False, reproject=True):
    """gml:Solid (exterior gml:Shell members → HAS_SURFACE_MEMBER) or gml:CompositeSolid /
    gml:MultiSolid (solidMember Solids → HAS_SOLID_MEMBER) → GeometrySolid."""
    ln = _ln3(solid_el)
    props = {"id": solid_id, "lod": lod, "source": source, "composite_surface_id": ""}
    if synthetic:
        props["synthetic_id"] = True
    if ln == "Solid":
        shell = solid_el.find("gml:exterior/gml:Shell", NS3)
        if shell is None:
            shell = solid_el.find("gml:exterior/gml:CompositeSurface", NS3)
            if shell is not None:
                props["exterior_type"] = "CompositeSurface"
        if shell is not None:
            props["composite_surface_id"] = shell.get(GML3_ID) or ""
            for i, sm in enumerate(shell.findall("gml:surfaceMember", NS3)):
                _record_member3(sm, solid_id, i, source, batch, ctx, reproject=reproject)
        if solid_el.find("gml:interior", NS3) is not None:
            ctx.unsupported["gml:Solid interior shell"] += 1
    elif ln in ("CompositeSolid", "MultiSolid"):
        props["geometry_type"] = ln
        for i, sm in enumerate(solid_el.findall("gml:solidMember", NS3)):
            href = sm.get(XLINK_HREF)
            if href:
                tgt = href_fragment(href)
                if tgt in ctx.local_ids:
                    batch["solid_member_rels"].append({"src": solid_id, "tgt": tgt,
                                                       "order": i, "via_xlink": True})
                else:
                    _defer3(ctx, "HAS_SOLID_MEMBER", solid_id, href, {"order": i},
                            {"via_xlink": True})
                continue
            inner = _first_child3(sm)
            if inner is None:
                continue
            sid = inner.get(GML3_ID)
            _collect_solid3(inner, sid or f"{solid_id}_solid_{i}", lod, source, batch, ctx,
                            synthetic=not sid, reproject=reproject)
            batch["solid_member_rels"].append({"src": solid_id, "tgt": sid or f"{solid_id}_solid_{i}",
                                               "order": i, "via_xlink": False})
    else:
        ctx.unsupported[f"solid gml:{ln}"] += 1
    batch["solids"].append(props)
    ctx.stats["solids"] += 1
    return solid_id


def _collect_curves3(curve_parent, owner_id, lod, source, batch, ctx, rel_key, role=None,
                     reproject=True):
    """gml:MultiCurve (curveMember LineStrings) → GeometryLineString rows + edges."""
    mc = curve_parent.find("gml:MultiCurve", NS3) if _ln3(curve_parent) != "MultiCurve" else curve_parent
    if mc is None:
        ctx.unsupported["non-MultiCurve curve geometry"] += 1
        return None
    mc_id = mc.get(GML3_ID)
    for i, cm in enumerate(mc.findall("gml:curveMember", NS3)):
        ls = cm.find("gml:LineString", NS3)
        if ls is None:
            ctx.unsupported["curveMember other than inline gml:LineString"] += 1
            continue
        lid = ls.get(GML3_ID)
        pos, dim = _coords3(ls, reproject)
        row = {"id": lid or f"{owner_id}_mc_lod{lod}_{i}_{_hash10(ls)}", "pos_list": pos,
               "srs_dimension": dim, "source": source}
        if not lid:
            row["synthetic_id"] = True
        batch["linestrings"].append(row)
        if rel_key == "line_rels":
            batch["line_rels"].append({"src": owner_id, "tgt": row["id"], "order": i})
        else:
            batch["multicurve_rels3"].append({"src": owner_id, "tgt": row["id"], "lod": lod,
                                              "order": i, "multi_curve_id": mc_id, "role": role})
        ctx.stats["linestrings"] += 1
    return mc_id


def _collect_point_cloud3(prop_el, owner_id, source, batch, ctx):
    """core:pointCloud (physical spaces and thematic surfaces) → (:PointCloud) via
    HAS_POINT_CLOUD. pcl:mimeType / pointFile / pointFileSrsName are scalars; the inline
    pcl:points MultiPoint is one pos list (points_pos_list, reprojected like every other
    coordinate) with its member encoding (points_member_style 'pointMember' |
    'pointMembers'; points_n_member for the pointMember part of a mixed MultiPoint)."""
    if prop_el.get(XLINK_HREF):
        ctx.unsupported["core:pointCloud given by reference"] += 1
        return None
    pc = prop_el.find("pcl:PointCloud", NS3)
    if pc is None:
        ctx.unsupported["core:pointCloud other than pcl:PointCloud"] += 1
        return None
    pid, synth = _child_id3(pc, owner_id, "PointCloud")
    props = {"id": pid, "feature_type": "PointCloud", "source": source, **ctx.prov}
    if synth:
        props["synthetic_id"] = True
    _semantic_props3(pc, pid, props, ctx, batch)
    for key, local in (("mime_type", "mimeType"), ("point_file", "pointFile"),
                       ("point_file_srs_name", "pointFileSrsName")):
        e = pc.find(f"pcl:{local}", NS3)
        if e is not None and e.text and e.text.strip():
            props[key] = e.text.strip()
            if e.get("codeSpace"):
                props[f"{key}_codespace"] = e.get("codeSpace")
    mp = pc.find("pcl:points/gml:MultiPoint", NS3)
    if mp is not None:
        single = mp.findall("gml:pointMember/gml:Point", NS3)
        multi = mp.findall("gml:pointMembers/gml:Point", NS3)
        if len(mp.findall("gml:pointMember", NS3)) != len(single):
            ctx.unsupported["gml:pointMember given by reference"] += 1
        coords, ids, dim = [], [], None
        for p in single + multi:
            pos = p.find("gml:pos", NS3)
            txt = pos.text.strip() if pos is not None and pos.text else ""
            if not txt:
                continue
            dim = dim or _dim_of(pos, len(txt.split()))
            coords.append(txt)
            ids.append(p.get(GML3_ID) or "")
        props["points_pos_list"] = _reproject_poslist(" ".join(coords), dim or 3)
        props["points_srs_dimension"] = dim or 3
        props["points_count"] = len(coords)
        props["points_member_style"] = "pointMembers" if multi and not single else "pointMember"
        if single and multi:
            props["points_n_member"] = len(single)
        if any(ids):
            props["point_ids"] = ids
        for key, attr in (("multipoint_id", GML3_ID), ("multipoint_srs_name", "srsName"),
                          ("multipoint_srs_dimension", "srsDimension")):
            if mp.get(attr):
                props[key] = mp.get(attr)
    batch["sub_features"].append({"labels": "PointCloud", "props": props})
    _add_sub_bbox3(batch, ctx, pid, pc)
    batch["child_rels"].append({"rel": "HAS_POINT_CLOUD", "src": owner_id, "tgt": pid,
                                "props": {}})
    ctx.stats["sub:PointCloud"] += 1
    return pid


def _collect_space_geometry3(el, node_id, source, batch, ctx, ms_role=None):
    """All core LoD geometry properties of a space (or of a Marking): lod0Point,
    lod0/2/3MultiSurface, lod0/2/3MultiCurve, lod1/2/3Solid, lod1-3TerrainIntersection-
    Curve, lod1-3ImplicitRepresentation — every LoD is kept."""
    core_pref = f"{{{NS3['core']}}}"
    for child in el:
        if not isinstance(child.tag, str) or not child.tag.startswith(core_pref):
            continue
        m = _LOD_PROP_RE.match(child.tag[len(core_pref):])
        if not m:
            if child.tag == f"{core_pref}pointCloud":
                _collect_point_cloud3(child, node_id, source, batch, ctx)
            continue
        lod, kind = m.groups()
        ctx.max_lod = max(ctx.max_lod, int(lod))
        href = child.get(XLINK_HREF)
        if kind == "MultiSurface":
            if href:
                _defer3(ctx, "HAS_LOD_SURFACE", node_id, href, {"lod": lod},
                        {"via_xlink": True, "role": ms_role})
                continue
            ms_el = child.find("gml:MultiSurface", NS3)
            if ms_el is None:
                continue
            ms_id = ms_el.get(GML3_ID) or f"ms_{node_id}_lod{lod}_{_hash10(ms_el)}"
            _collect_ms3(ms_el, ms_id, lod, source, batch, ctx,
                         synthetic=not ms_el.get(GML3_ID))
            batch["bldg_surface_rels"].append({"src": node_id, "tgt": ms_id,
                                               "lod": lod, "role": ms_role})
        elif kind == "Solid":
            if href:
                _defer3(ctx, "HAS_LOD_SOLID", node_id, href, {}, {"via_xlink": True})
                continue
            s_el = _first_child3(child)
            if s_el is None:
                continue
            sid = s_el.get(GML3_ID) or f"solid_{node_id}_lod{lod}_{_hash10(s_el)}"
            _collect_solid3(s_el, sid, lod, source, batch, ctx, synthetic=not s_el.get(GML3_ID))
            batch["solid_rels"].append({"src": node_id, "tgt": sid})
        elif kind == "MultiCurve":
            if href:
                ctx.unsupported["lodNMultiCurve given by xlink"] += 1
                continue
            _collect_curves3(child, node_id, lod, source, batch, ctx, "multicurve_rels3")
        elif kind == "Point":
            pt = child.find("gml:Point", NS3)
            if pt is None:
                continue
            pos = pt.find("gml:pos", NS3)
            txt = pos.text.strip() if pos is not None and pos.text else ""
            dim = _dim_of(pos, len(txt.split())) if pos is not None else 3
            pid = pt.get(GML3_ID)
            row = {"id": pid or f"{node_id}_lod{lod}point_{_hash10(pt)}",
                   "pos_list": _reproject_poslist(txt, dim),
                   "srs_dimension": dim, "source": source, "geometry_type": "Point"}
            if not pid:
                row["synthetic_id"] = True
            batch["linestrings"].append(row)
            batch["point_rels"].append({"src": node_id, "tgt": row["id"], "lod": lod})
        elif kind == "TerrainIntersectionCurve":
            ti_id = f"{node_id}_terrain_lod{lod}"
            mc = child.find("gml:MultiCurve", NS3)
            batch["terrain_ints"].append({"id": ti_id, "lod": lod, "source": source,
                                          "multi_curve_id": mc.get(GML3_ID) if mc is not None else None})
            batch["bldg_terrain_rels"].append({"src": node_id, "tgt": ti_id})
            if mc is not None:
                for i, cm in enumerate(mc.findall("gml:curveMember", NS3)):
                    ls = cm.find("gml:LineString", NS3)
                    if ls is None:
                        continue
                    lid = ls.get(GML3_ID)
                    pos, dim = _coords3(ls)
                    row = {"id": lid or f"{ti_id}_ls_{i}_{_hash10(ls)}", "pos_list": pos,
                           "srs_dimension": dim, "source": source}
                    if not lid:
                        row["synthetic_id"] = True
                    batch["linestrings"].append(row)
                    batch["terrain_line_rels"].append({"src": ti_id, "tgt": row["id"], "order": i})
        elif kind == "ImplicitRepresentation":
            _collect_implicit3(child, node_id, lod, source, batch, ctx)


def _local_bbox3(geom_el):
    acc = [float("inf")] * 3 + [float("-inf")] * 3
    found = False
    for c in geom_el.iter(_POSLIST3, _POS3):
        if not c.text:
            continue
        vals = c.text.split()
        dim = _dim_of(c, len(vals))
        for i in range(0, len(vals) - dim + 1, dim):
            try:
                x, y = float(vals[i]), float(vals[i + 1])
                z = float(vals[i + 2]) if dim > 2 else 0.0
            except ValueError:
                continue
            found = True
            acc[0], acc[3] = min(acc[0], x), max(acc[3], x)
            acc[1], acc[4] = min(acc[1], y), max(acc[4], y)
            acc[2], acc[5] = min(acc[2], z), max(acc[5], z)
    return acc if found else None


def _collect_prototype3(geom_el, proto_id, synthetic, owner_id, source, batch, ctx):
    """An inline core:relativeGeometry template → ONE (:GeometryPrototype) (generalises
    the street-tree pattern): MultiSurface/CompositeSurface/Solid/TriangulatedSurface
    faces → HAS_SURFACE_MEMBER → GeometryPolygon; CompositeSolid/MultiSolid →
    HAS_SOLID_MEMBER → GeometrySolid; MultiCurve → HAS_LINE → GeometryLineString.
    Coordinates are template-local and are never reprojected; a srsName on template
    polygons is kept but is not meaningful."""
    ln = _ln3(geom_el)
    row = {"id": proto_id, "source": source, "geometry_type": ln, "owner_feature_id": owner_id,
           **ctx.prov}
    if synthetic:
        row["synthetic_id"] = True
    if geom_el.get("srsName"):
        row["srs_name"] = geom_el.get("srsName")
    if ln in ("MultiSurface", "CompositeSurface"):
        for i, sm in enumerate(geom_el.findall("gml:surfaceMember", NS3)):
            _record_member3(sm, proto_id, i, source, batch, ctx, reproject=False)
    elif ln == "Solid":
        shell = geom_el.find("gml:exterior/gml:Shell", NS3)
        if shell is not None:
            if shell.get(GML3_ID):
                row["shell_id"] = shell.get(GML3_ID)
            for i, sm in enumerate(shell.findall("gml:surfaceMember", NS3)):
                _record_member3(sm, proto_id, i, source, batch, ctx, reproject=False)
    elif ln in ("CompositeSolid", "MultiSolid"):
        for i, sm in enumerate(geom_el.findall("gml:solidMember", NS3)):
            inner = _first_child3(sm)
            if inner is None:
                continue
            sid = inner.get(GML3_ID)
            _collect_solid3(inner, sid or f"{proto_id}_solid_{i}", None, source, batch, ctx,
                            synthetic=not sid, reproject=False)
            batch["solid_member_rels"].append({"src": proto_id, "tgt": sid or f"{proto_id}_solid_{i}",
                                               "order": i, "via_xlink": False})
    elif ln == "MultiCurve":
        _collect_curves3(geom_el, proto_id, None, source, batch, ctx, "line_rels", reproject=False)
    elif ln == "TriangulatedSurface":
        patches = geom_el.find("gml:patches", NS3)
        tris = (patches if patches is not None else geom_el).findall(".//gml:Triangle", NS3)
        for i, tri in enumerate(tris):
            pid = _collect_polygon3(tri, f"tri_{proto_id}_{i}", source, batch, reproject=False,
                                    patch="Triangle")
            batch["surface_member_rels"].append({"src": proto_id, "tgt": pid, "order": i,
                                                 "via_xlink": False, "orientation": None})
        row["triangle_count"] = len(tris)
    else:
        ctx.unsupported[f"implicit template gml:{ln}"] += 1
    bb = _local_bbox3(geom_el)
    if bb:
        for i, ax in enumerate(("x", "y", "z")):
            row[f"local_min_{ax}"] = bb[i]
            row[f"local_max_{ax}"] = bb[i + 3]
        ctx.proto_bbox[proto_id] = bb
    batch["prototypes"].append(row)
    ctx.stats["prototypes"] += 1


def _collect_implicit3(prop_el, owner_id, lod, source, batch, ctx):
    """core:lodNImplicitRepresentation/core:ImplicitGeometry → (owner)-[:HAS_PROTOTYPE
    {lod, transformation_matrix, reference_point, reference_point_id, mime_type,
    library_object, via_xlink}]->(:GeometryPrototype). The template is stored ONCE; an
    xlink:href relativeGeometry becomes a deferred edge to the same prototype."""
    if prop_el.get(XLINK_HREF):
        ctx.unsupported["ImplicitRepresentation given by xlink"] += 1
        return
    ig = prop_el.find("core:ImplicitGeometry", NS3)
    if ig is None:
        return
    props = {"transformation_matrix": _t3(ig, "core:transformationMatrix") or ""}
    if ig.get(GML3_ID):
        props["implicit_geometry_id"] = ig.get(GML3_ID)
    mime = ig.find("core:mimeType", NS3)
    if mime is not None and mime.text:
        props["mime_type"] = mime.text.strip()
        if mime.get("codeSpace"):
            props["mime_type_codespace"] = mime.get("codeSpace")
    lib = _t3(ig, "core:libraryObject")
    if lib:
        props["library_object"] = lib
    ref = None
    pt = ig.find("core:referencePoint/gml:Point", NS3)
    if pt is not None:
        pos = pt.find("gml:pos", NS3)
        txt = pos.text.strip() if pos is not None and pos.text else ""
        dim = _dim_of(pos, len(txt.split())) if pos is not None else 3
        txt = _reproject_poslist(txt, dim)
        props["reference_point"] = txt
        props["reference_point_srs_dimension"] = dim
        if pt.get(GML3_ID):
            props["reference_point_id"] = pt.get(GML3_ID)
        try:
            vals = [float(v) for v in txt.split()]
            ref = (vals + [0.0, 0.0, 0.0])[:3]
        except ValueError:
            ref = None
    rg = ig.find("core:relativeGeometry", NS3)
    proto_id = None
    rhref = rg.get(XLINK_HREF) if rg is not None else None
    if rhref:
        proto_id = href_fragment(rhref)
        props["via_xlink"] = True
    elif rg is not None and _first_child3(rg) is not None:
        geom = _first_child3(rg)
        gid = geom.get(GML3_ID)
        proto_id = gid or f"proto_{owner_id}_lod{lod}"
        _collect_prototype3(geom, proto_id, not gid, owner_id, source, batch, ctx)
        props["via_xlink"] = False
    else:
        # library object only (no GML geometry): keep the transform on a placeholder
        proto_id = f"proto_{owner_id}_lod{lod}"
        batch["prototypes"].append({"id": proto_id, "source": source, "geometry_type": None,
                                    "owner_feature_id": owner_id, "synthetic_id": True})
        props["via_xlink"] = False
    for i, ap in enumerate(ig.findall("core:appearance", NS3)):
        app_el = _first_child3(ap)
        if app_el is not None and not ap.get(XLINK_HREF):
            aid = _collect_appearance3(app_el, f"{proto_id}_app_{i}", source, batch, ctx)
            batch["appearance_rels"].append({"src": proto_id, "tgt": aid, "order": i})
    if props["via_xlink"]:
        _defer3(ctx, "HAS_PROTOTYPE", owner_id, rhref, {"lod": lod}, props)
    else:
        batch["proto_rels"].append({"src": owner_id, "tgt": proto_id, "lod": lod, "props": props})
    try:
        mvals = [float(v) for v in props["transformation_matrix"].split()]
    except ValueError:
        mvals = []
    if ref is not None and len(mvals) == 16:
        ctx.implicit_jobs.append((owner_id, mvals, ref, proto_id))
    ctx.stats["implicit_geometries"] += 1


# ── appearance ───────────────────────────────────────────────────────────────

_X3D_FIELDS = (("ambient_intensity", "ambientIntensity"), ("diffuse_color", "diffuseColor"),
               ("emissive_color", "emissiveColor"), ("specular_color", "specularColor"),
               ("shininess", "shininess"), ("transparency", "transparency"),
               ("is_smooth", "isSmooth"))
_TEX_FIELDS = (("image_uri", "imageURI"), ("texture_type", "textureType"),
               ("wrap_mode", "wrapMode"), ("border_color", "borderColor"))


def _collect_appearance3(app_el, fallback_id, source, batch, ctx):
    """app:Appearance (CityModel member, feature-level or on an ImplicitGeometry) →
    (:Appearance {id, theme}) -[:HAS_SURFACE_DATA {order}]-> (:X3DMaterial |
    :ParameterizedTexture | :GeoreferencedTexture) -[:TARGETS {order,
    texture_coordinates_json?, world_to_texture?}]-> (GeometryPolygon | …) — targets are
    resolved by gml:id after parsing (deferred)."""
    aid = app_el.get(GML3_ID)
    props = {"id": aid or fallback_id, "source": source, **ctx.prov}
    if not aid:
        props["synthetic_id"] = True
    aid = props["id"]
    theme = _t3(app_el, "app:theme")
    if theme is not None:
        props["theme"] = theme
    for key, xp in (("gml_name", "gml:name"), ("gml_description", "gml:description")):
        v = _t3(app_el, xp)
        if v:
            props[key] = v
    batch["appearances"].append(props)
    ctx.stats["appearances"] += 1
    for i, sdp in enumerate(app_el.findall("app:surfaceData", NS3)):
        if sdp.get(XLINK_HREF):
            _defer3(ctx, "HAS_SURFACE_DATA", aid, sdp.get(XLINK_HREF), {}, {"order": i})
            continue
        sd = _first_child3(sdp)
        if sd is None:
            continue
        ln = _ln3(sd)
        sid = sd.get(GML3_ID)
        sp = {"id": sid or f"{aid}_sd_{i}", "source": source, "surface_data_type": ln, **ctx.prov}
        if not sid:
            sp["synthetic_id"] = True
        sid = sp["id"]
        front = _t3(sd, "app:isFront")
        if front is not None:
            sp["is_front"] = front
        targets = []
        if ln == "X3DMaterial":
            for key, local in _X3D_FIELDS:
                v = _t3(sd, f"app:{local}")
                if v is not None:
                    sp[key] = v
            targets = [(t.text or "").strip() for t in sd.findall("app:target", NS3)]
            for j, t in enumerate(targets):
                _defer3(ctx, "TARGETS", sid, t, {"order": j}, {})
        elif ln in ("ParameterizedTexture", "GeoreferencedTexture"):
            for key, local in _TEX_FIELDS:
                v = _t3(sd, f"app:{local}")
                if v is not None:
                    sp[key] = v
            mime = sd.find("app:mimeType", NS3)
            if mime is not None and mime.text:
                sp["mime_type"] = mime.text.strip()
                if mime.get("codeSpace"):
                    sp["mime_type_codespace"] = mime.get("codeSpace")
            if ln == "ParameterizedTexture":
                for j, tp in enumerate(sd.findall("app:textureParameterization", NS3)):
                    ta = tp.find("app:TextureAssociation", NS3)
                    if ta is None:
                        continue
                    tgt = _t3(ta, "app:target")
                    ep = {}
                    if ta.get(GML3_ID):
                        ep["texture_association_id"] = ta.get(GML3_ID)
                    tcl = ta.find("app:textureParameterization/app:TexCoordList", NS3)
                    if tcl is not None:
                        coords = [" ".join((c.text or "").split())
                                  for c in tcl.findall("app:textureCoordinates", NS3)]
                        rings = [href_fragment((r.text or "").strip())
                                 for r in tcl.findall("app:ring", NS3)]
                        ep["texture_coordinates_json"] = json.dumps(
                            [[r, c] for r, c in zip(rings, coords)])
                    tcg = ta.find("app:textureParameterization/app:TexCoordGen", NS3)
                    if tcg is not None:
                        ep["world_to_texture"] = _t3(tcg, "app:worldToTexture")
                    if tgt:
                        _defer3(ctx, "TARGETS", sid, tgt, {"order": j}, ep)
                        targets.append(tgt)
            else:
                v = _t3(sd, "app:preferWorldFile")
                if v is not None:
                    sp["prefer_world_file"] = v
                pos = sd.find("app:referencePoint/gml:Point/gml:pos", NS3)
                if pos is not None and pos.text:
                    sp["reference_point"] = pos.text.strip()
                v = _t3(sd, "app:orientation")
                if v is not None:
                    sp["orientation"] = v
                targets = [(t.text or "").strip() for t in sd.findall("app:target", NS3)]
                for j, t in enumerate(targets):
                    _defer3(ctx, "TARGETS", sid, t, {"order": j}, {})
        else:
            ctx.unsupported[f"surface data app:{ln}"] += 1
            continue
        batch["surface_data"].append({"label": ln, "props": sp})
        batch["surface_data_rels"].append({"src": aid, "tgt": sid, "order": i})
        ctx.stats["surface_data"] += 1
        ctx.stats["targets"] += len(targets)
    return aid


# ── thematic surfaces, spaces, sub-features ──────────────────────────────────

def _collect_thematic_surface3(surf_el, parent_id, order, source, batch, ctx):
    """A core:boundary thematic surface (con:WallSurface…, core:ClosureSurface,
    tran:TrafficArea/AuxiliaryTrafficArea/HoleSurface, wtr:Water*Surface,
    gen:GenericThematicSurface) → (:BoundarySurface[:TrafficArea…] {surface_type, lod}).
    The highest-LoD MultiSurface is the primary geometry (HAS_POLYGON, as in 2.0); other
    LoDs → HAS_LOD_SURFACE {lod}; lod0MultiCurve → HAS_LOD_MULTICURVE {lod:'0'};
    con:fillingSurface (Window/DoorSurface) → Opening via HAS_OPENING."""
    st = _ln3(surf_el)
    gid = surf_el.get(GML3_ID)
    sid = gid or f"bsurf_{_slug(parent_id)}_{st.lower()}_{_hash10(surf_el)}"
    props = {"id": sid, "surface_type": st, "source": source, **ctx.prov}
    if not gid:
        props["synthetic_id"] = True
    _semantic_props3(surf_el, sid, props, ctx, batch)
    if st in _V3_SURF_ATTRS:
        pref, specs = _V3_SURF_ATTRS[st]
        _module_attrs3(surf_el, pref, specs, props)
    ms_props = {}
    for child in surf_el:
        if not isinstance(child.tag, str) or not child.tag.startswith(f"{{{NS3['core']}}}lod"):
            continue
        m = _LOD_PROP_RE.match(etree.QName(child).localname)
        if m and m.group(2) in ("MultiSurface", "MultiCurve"):
            ms_props.setdefault(m.group(2), []).append((m.group(1), child))
    surfaces = sorted(ms_props.get("MultiSurface", []), key=lambda t: t[0])
    primary = None
    for lod, child in reversed(surfaces):
        if not child.get(XLINK_HREF) and child.find("gml:MultiSurface", NS3) is not None:
            primary = (lod, child)
            break
    for lod, child in surfaces:
        ctx.max_lod = max(ctx.max_lod, int(lod))
        if primary is not None and child is primary[1]:
            ms_el = child.find("gml:MultiSurface", NS3)
            props["lod"] = lod
            if ms_el.get(GML3_ID):
                props["multi_surface_id"] = ms_el.get(GML3_ID)
            members = ms_el.findall("gml:surfaceMember", NS3)
            cs = members[0].find("gml:CompositeSurface", NS3) if len(members) == 1 else None
            if cs is not None and cs.get(GML3_ID):
                props["composite_surface_id"] = cs.get(GML3_ID)
                batch["composite_surfaces"].append({"id": cs.get(GML3_ID), "source": source})
                for i, sm in enumerate(cs.findall("gml:surfaceMember", NS3)):
                    _record_member3(sm, sid, i, source, batch, ctx, rel="HAS_POLYGON")
                    pid = sm.find("gml:Polygon", NS3)
                    if pid is not None:
                        batch["composite_poly_rels"].append(
                            {"src": cs.get(GML3_ID), "order": i,
                             "tgt": pid.get(GML3_ID) or f"{sid}_poly_{i}"})
            else:
                for i, sm in enumerate(members):
                    _record_member3(sm, sid, i, source, batch, ctx, rel="HAS_POLYGON")
            continue
        href = child.get(XLINK_HREF)
        if href:
            _defer3(ctx, "HAS_LOD_SURFACE", sid, href, {"lod": lod}, {"via_xlink": True})
            continue
        ms_el = child.find("gml:MultiSurface", NS3)
        if ms_el is None:
            continue
        mid = ms_el.get(GML3_ID) or f"ms_{sid}_lod{lod}_{_hash10(ms_el)}"
        _collect_ms3(ms_el, mid, lod, source, batch, ctx, synthetic=not ms_el.get(GML3_ID))
        batch["bldg_surface_rels"].append({"src": sid, "tgt": mid, "lod": lod, "role": None})
    for lod, child in ms_props.get("MultiCurve", []):
        _collect_curves3(child, sid, lod, source, batch, ctx, "multicurve_rels3")
    for pc in surf_el.findall("core:pointCloud", NS3):
        _collect_point_cloud3(pc, sid, source, batch, ctx)
    # openings (con:fillingSurface → con:WindowSurface | con:DoorSurface)
    for i, fs in enumerate(surf_el.findall("con:fillingSurface", NS3)):
        o = _first_child3(fs)
        if o is None:
            continue
        fln = _ln3(o)
        oid = o.get(GML3_ID) or f"{sid}_opening_{i}"
        op = {"id": oid, "opening_type": fln.replace("Surface", ""), "filling_type": fln,
              "source": source, **ctx.prov}
        if not o.get(GML3_ID):
            op["synthetic_id"] = True
        _semantic_props3(o, oid, op, ctx, batch)
        batch["openings"].append(op)
        _add_sub_bbox3(batch, ctx, oid, o)
        batch["surf_opening_rels"].append({"src": sid, "tgt": oid, "order": i})
        for child in o:
            if not isinstance(child.tag, str):
                continue
            m = _LOD_PROP_RE.match(etree.QName(child).localname)
            if not m or m.group(2) != "MultiSurface":
                continue
            ms_el = child.find("gml:MultiSurface", NS3)
            if ms_el is None:
                continue
            mid = ms_el.get(GML3_ID)
            _collect_ms3(ms_el, mid or f"ms_{oid}_lod{m.group(1)}", m.group(1), source, batch, ctx,
                         synthetic=not mid)
            batch["opening_ms_rels"].append({"src": oid, "tgt": mid or f"ms_{oid}_lod{m.group(1)}",
                                             "lod": m.group(1)})
        for pc in o.findall("core:pointCloud", NS3):
            _collect_point_cloud3(pc, oid, source, batch, ctx)
        ctx.stats["surface:" + fln] += 1
    batch["surfaces"].append(props)
    _add_sub_bbox3(batch, ctx, sid, surf_el)
    if st in _V3_SURF_EXTRA_LABEL:
        batch["surface_labels"].append({"id": sid, "label": st})
    ctx.stats["surface:" + st] += 1
    return sid


def _collect_space_common3(el, node_id, source, batch, ctx, ms_role=None, boundary_prop=None):
    """core:boundary thematic surfaces + all space LoD geometry of a space node."""
    for i, b in enumerate(el.findall("core:boundary", NS3)):
        if b.get(XLINK_HREF):
            _defer3(ctx, "HAS_BOUNDARY", node_id, b.get(XLINK_HREF), {},
                    {"order": i, "prop": boundary_prop or "boundary", "via_xlink": True})
            continue
        child = _first_child3(b)
        if child is None:
            continue
        sid = _collect_thematic_surface3(child, node_id, i, source, batch, ctx)
        if boundary_prop:
            batch["child_rels"].append({"rel": "HAS_BOUNDARY", "src": node_id, "tgt": sid,
                                        "props": {"order": i, "prop": boundary_prop}})
        else:
            batch["boundary_rels"].append({"src": node_id, "tgt": sid, "order": i})
    _collect_space_geometry3(el, node_id, source, batch, ctx, ms_role)


def _child_id3(el, parent_id, kind):
    gid = el.get(GML3_ID)
    return (gid, False) if gid else (f"{parent_id}_{kind.lower()}_{_hash10(el)}", True)


def _collect_sub_feature3(el, kind, parent_id, source, batch, ctx):
    """Section / Intersection / TrafficSpace / AuxiliaryTrafficSpace / Hole /
    ClearanceSpace / Marking → (:<labels>:Entity) — never CityObject, never PART_OF."""
    prefix, specs, labels = _V3_SUB[kind]
    sid, synth = _child_id3(el, parent_id, kind)
    props = {"id": sid, "feature_type": kind, "source": source, **ctx.prov}
    if synth:
        props["synthetic_id"] = True
    _semantic_props3(el, sid, props, ctx, batch)
    _module_attrs3(el, prefix, specs, props)
    if kind == "Marking":
        _collect_space_geometry3(el, sid, source, batch, ctx)
    else:
        _collect_space_common3(el, sid, source, batch, ctx,
                               boundary_prop="boundary" if kind not in ("Hole", "ClearanceSpace")
                               else None)
    if kind in ("Section", "Intersection"):
        _collect_transport_children3(el, sid, props, source, batch, ctx)
    if kind == "TrafficSpace":
        occ = _occupancy3(el, "tran")
        if occ:
            props["occupancy_json"] = occ
        for rel, local in (("PREDECESSOR", "predecessor"), ("SUCCESSOR", "successor")):
            for i, r in enumerate(el.findall(f"tran:{local}", NS3)):
                if r.get(XLINK_HREF):
                    _defer3(ctx, rel, sid, r.get(XLINK_HREF), {"order": i},
                            {"order": i, "source": source})
        for i, w in enumerate(el.findall("tran:clearanceSpace", NS3)):
            cs = w.find("tran:ClearanceSpace", NS3)
            if cs is None:
                continue
            cid = _collect_sub_feature3(cs, "ClearanceSpace", sid, source, batch, ctx)
            batch["child_rels"].append({"rel": "HAS_CLEARANCE_SPACE", "src": sid, "tgt": cid,
                                        "props": {"order": i}})
    batch["sub_features"].append({"labels": labels, "props": props})
    _add_sub_bbox3(batch, ctx, sid, el)
    ctx.stats["sub:" + kind] += 1
    return sid


def _collect_transport_children3(el, node_id, props, source, batch, ctx):
    """tran:AbstractTransportationSpace content of a Road/Railway/Track/Square/Waterway/
    Section/Intersection: trafficDirection, occupancy, trafficSpace*,
    auxiliaryTrafficSpace*, hole*, marking*, section*, intersection*."""
    td = _t3(el, "tran:trafficDirection")
    if td:
        props["traffic_direction"] = td
    occ = _occupancy3(el, "tran")
    if occ:
        props["occupancy_json"] = occ
    for prop, kind, rel, extra in (
            ("trafficSpace", "TrafficSpace", "HAS_TRAFFIC_SPACE", {"auxiliary": False}),
            ("auxiliaryTrafficSpace", "AuxiliaryTrafficSpace", "HAS_TRAFFIC_SPACE", {"auxiliary": True}),
            ("hole", "Hole", "HAS_HOLE", {}),
            ("marking", "Marking", "HAS_MARKING", {}),
            ("section", "Section", "HAS_SECTION", {}),
            ("intersection", "Intersection", "HAS_INTERSECTION", {})):
        for i, w in enumerate(el.findall(f"tran:{prop}", NS3)):
            if w.get(XLINK_HREF):
                _defer3(ctx, rel, node_id, w.get(XLINK_HREF), {},
                        {"order": i, "via_xlink": True, **extra})
                continue
            child = w.find(f"tran:{kind}", NS3)
            if child is None:
                continue
            cid = _collect_sub_feature3(child, kind, node_id, source, batch, ctx)
            batch["child_rels"].append({"rel": rel, "src": node_id, "tgt": cid,
                                        "props": {"order": i, **extra}})


def _collect_nested3(el, kind, prop, parent_id, order, source, batch, ctx, prefix="brid"):
    """brid:BridgeConstructiveElement / brid:BridgeInstallation /
    bldg:BuildingConstructiveElement → NestedFeature (the 2.0 bridge-nesting node, with
    ns_prefix = the module) via HAS_NESTED {prop, order}. con:filling Windows / Doors →
    Opening via HAS_OPENING {order, prop: 'filling'}."""
    nid, synth = _child_id3(el, parent_id, kind)
    props = {"id": nid, "feature_kind": kind, "ns_prefix": prefix, "source": source, **ctx.prov}
    if synth:
        props["synthetic_id"] = True
    _semantic_props3(el, nid, props, ctx, batch)
    _module_attrs3(el, prefix, _CFU3, props)
    v = _t3(el, "con:isStructuralElement")
    if v is not None:
        props["is_structural_element"] = v
    v = _t3(el, "con:relationToConstruction")
    if v is not None:
        props["relation_to_construction"] = v
    _collect_space_common3(el, nid, source, batch, ctx)
    for i, f in enumerate(el.findall("con:filling", NS3)):
        fe = _first_child3(f)
        if f.get(XLINK_HREF) or fe is None:
            ctx.unsupported["con:filling given by reference"] += 1
            continue
        _collect_filling3(fe, nid, i, source, batch, ctx)
    batch["nested_features"].append(props)
    _add_sub_bbox3(batch, ctx, nid, el)
    batch["nested_rels"].append({"src": parent_id, "tgt": nid, "prop": prop, "order": order})
    ctx.stats["sub:" + kind] += 1
    return nid


def _collect_filling3(el, parent_id, order, source, batch, ctx):
    """con:Window / con:Door (a filling element: an occupied space, not a surface) →
    the same Opening node as a 2.0 opening or a 3.0 Window/DoorSurface, told apart by
    filling_type 'Window' | 'Door' and the HAS_OPENING edge prop 'filling'."""
    kind = _ln3(el)
    oid, synth = _child_id3(el, parent_id, kind)
    op = {"id": oid, "opening_type": kind, "filling_type": kind, "source": source, **ctx.prov}
    if synth:
        op["synthetic_id"] = True
    _semantic_props3(el, oid, op, ctx, batch)
    _module_attrs3(el, "con", _CFU3, op)
    addr = el.findall("con:address", NS3)
    if addr:
        op["address_xml"] = json.dumps([etree.tostring(a, encoding="unicode") for a in addr])
    _collect_space_common3(el, oid, source, batch, ctx)
    batch["openings"].append(op)
    _add_sub_bbox3(batch, ctx, oid, el)
    batch["child_rels"].append({"rel": "HAS_OPENING", "src": parent_id, "tgt": oid,
                                "props": {"order": order, "prop": "filling"}})
    ctx.stats["sub:" + kind] += 1
    return oid


def _collect_furniture3(el, parent_id, order, in_room, source, batch, ctx):
    """bldg:BuildingFurniture → (:BuildingFurniture) as in 2.0: HAS_INTERIOR_FURNITURE
    {order} from its BuildingRoom, Storey, BuildingUnit or Building."""
    fid, synth = _child_id3(el, parent_id, "furn")
    fp = {"id": fid, "source": source, **ctx.prov}
    if synth:
        fp["synthetic_id"] = True
    _semantic_props3(el, fid, fp, ctx, batch)
    _cfu_codes3(el, "bldg", fp)
    _collect_space_common3(el, fid, source, batch, ctx)
    batch["furniture"].append(fp)
    _add_sub_bbox3(batch, ctx, fid, el)
    if in_room:
        batch["room_furn_rels"].append({"src": parent_id, "tgt": fid, "order": order})
    else:
        batch["child_rels"].append({"rel": "HAS_INTERIOR_FURNITURE", "src": parent_id,
                                    "tgt": fid, "props": {"order": order}})
    ctx.stats["sub:BuildingFurniture"] += 1
    return fid


def _collect_installation3(el, parent_id, order, in_room, source, batch, ctx):
    """bldg:BuildingInstallation → (:BuildingInstallation) as in 2.0: from a BuildingRoom
    HAS_ROOM_INSTALLATION, otherwise HAS_INTERIOR_INSTALLATION when relationToConstruction
    is 'inside' (the 2.0 IntBuildingInstallation) and HAS_OUTER_INSTALLATION else."""
    iid, synth = _child_id3(el, parent_id, "inst")
    ip = {"id": iid, "installation_kind": "BuildingInstallation", "source": source, **ctx.prov}
    if synth:
        ip["synthetic_id"] = True
    _semantic_props3(el, iid, ip, ctx, batch)
    _cfu_codes3(el, "bldg", ip)
    v = _t3(el, "con:relationToConstruction")
    if v:
        ip["relation_to_construction"] = v
    _collect_space_common3(el, iid, source, batch, ctx)
    batch["installations"].append(ip)
    _add_sub_bbox3(batch, ctx, iid, el)
    row = {"src": parent_id, "tgt": iid, "order": order}
    if in_room:
        batch["room_inst_rels"].append(row)
    elif v == "inside":
        batch["bldg_int_inst_rels"].append(row)
    else:
        batch["bldg_inst_rels"].append(row)
    ctx.stats["sub:BuildingInstallation"] += 1
    return iid


def _collect_room3(el, parent_id, order, source, batch, ctx):
    """bldg:BuildingRoom → (:Room) via HAS_INTERIOR_ROOM {order} (from a Building,
    BuildingPart, Storey or BuildingUnit) with its boundary surfaces, geometry, point
    cloud, furniture and installations; bldg:roomHeight is kept verbatim."""
    rid, synth = _child_id3(el, parent_id, "room")
    rp = {"id": rid, "source": source, **ctx.prov}
    if synth:
        rp["synthetic_id"] = True
    _semantic_props3(el, rid, rp, ctx, batch)
    _cfu_codes3(el, "bldg", rp)
    rh = el.findall("bldg:roomHeight", NS3)
    if rh:
        rp["room_height_xml"] = json.dumps([etree.tostring(h, encoding="unicode") for h in rh])
    _collect_space_common3(el, rid, source, batch, ctx)
    for i, w in enumerate(el.findall("bldg:buildingFurniture/bldg:BuildingFurniture", NS3)):
        _collect_furniture3(w, rid, i, True, source, batch, ctx)
    for i, w in enumerate(el.findall("bldg:buildingInstallation/bldg:BuildingInstallation", NS3)):
        _collect_installation3(w, rid, i, True, source, batch, ctx)
    batch["rooms"].append(rp)
    _add_sub_bbox3(batch, ctx, rid, el)
    batch["interior_room_rels"].append({"src": parent_id, "tgt": rid, "order": order})
    ctx.stats["sub:BuildingRoom"] += 1
    return rid


def _collect_building_children3(el, node_id, source, batch, ctx):
    """The bldg: children a Building, BuildingPart, Storey and BuildingUnit share:
    buildingConstructiveElement (NestedFeature), buildingInstallation, buildingRoom,
    buildingFurniture. By-reference members (xlink) are counted as unsupported."""
    n_ref = 0
    for i, w in enumerate(el.findall("bldg:buildingConstructiveElement", NS3)):
        c = w.find("bldg:BuildingConstructiveElement", NS3)
        if c is None:
            n_ref += 1
            continue
        _collect_nested3(c, "BuildingConstructiveElement", "buildingConstructiveElement",
                         node_id, i, source, batch, ctx, prefix="bldg")
    for i, w in enumerate(el.findall("bldg:buildingInstallation", NS3)):
        c = w.find("bldg:BuildingInstallation", NS3)
        if c is None:
            n_ref += 1
            continue
        _collect_installation3(c, node_id, i, False, source, batch, ctx)
    for i, w in enumerate(el.findall("bldg:buildingRoom", NS3)):
        c = w.find("bldg:BuildingRoom", NS3)
        if c is None:
            n_ref += 1
            continue
        _collect_room3(c, node_id, i, source, batch, ctx)
    for i, w in enumerate(el.findall("bldg:buildingFurniture", NS3)):
        c = w.find("bldg:BuildingFurniture", NS3)
        if c is None:
            n_ref += 1
            continue
        _collect_furniture3(c, node_id, i, False, source, batch, ctx)
    if n_ref:
        ctx.unsupported["bldg: building member given by reference"] += n_ref


def _collect_subdivision3(el, parent_id, order, source, batch, ctx):
    """bldg:Storey / bldg:BuildingUnit (bldg:buildingSubdivision; no 2.0 equivalent) →
    (:BuildingSubdivision:Storey|BuildingUnit) via HAS_SUBDIVISION {order}. Their
    boundary surfaces, rooms, constructive elements, installations and furniture hang
    below them with the same edges as below a Building; elevation and sortKey are kept,
    and the Storey ↔ BuildingUnit cross references (bldg:buildingUnit / bldg:storey)
    become deferred HAS_BUILDING_UNIT / HAS_STOREY {order} edges."""
    kind = _ln3(el)
    sid, synth = _child_id3(el, parent_id, kind)
    props = {"id": sid, "feature_type": kind, "source": source, **ctx.prov}
    if synth:
        props["synthetic_id"] = True
    _semantic_props3(el, sid, props, ctx, batch)
    _cfu_codes3(el, "bldg", props)
    elevs = _elevations3(el, "bldg")
    if elevs:
        props["elevations_json"] = elevs
    v = _t3(el, "bldg:sortKey")
    if v is not None:
        props["sort_key"] = v
    if kind == "BuildingUnit":
        addr = el.findall("bldg:address", NS3)
        if addr:
            props["address_xml"] = json.dumps([etree.tostring(a, encoding="unicode") for a in addr])
    _collect_space_common3(el, sid, source, batch, ctx)
    _collect_building_children3(el, sid, source, batch, ctx)
    for rel, local in (("HAS_BUILDING_UNIT", "buildingUnit"), ("HAS_STOREY", "storey")):
        for i, r in enumerate(el.findall(f"bldg:{local}", NS3)):
            if r.get(XLINK_HREF):
                _defer3(ctx, rel, sid, r.get(XLINK_HREF), {"order": i}, {"order": i})
            else:
                ctx.unsupported[f"bldg:{local} given inline"] += 1
    batch["sub_features"].append({"labels": f"BuildingSubdivision:{kind}", "props": props})
    _add_sub_bbox3(batch, ctx, sid, el)
    batch["child_rels"].append({"rel": "HAS_SUBDIVISION", "src": parent_id, "tgt": sid,
                                "props": {"order": order}})
    ctx.stats["sub:" + kind] += 1
    return sid


def _collect_bridge_body3(el, node_id, props, source, batch, ctx):
    _construction_props3(el, props, ctx)
    _collect_space_common3(el, node_id, source, batch, ctx)
    for prop, kind in (("bridgeConstructiveElement", "BridgeConstructiveElement"),
                       ("bridgeInstallation", "BridgeInstallation")):
        for i, w in enumerate(el.findall(f"brid:{prop}", NS3)):
            child = w.find(f"brid:{kind}", NS3)
            if child is not None:
                _collect_nested3(child, kind, prop, node_id, i, source, batch, ctx)
    for prop in ("bridgeRoom", "bridgeFurniture"):
        n = len(el.findall(f"brid:{prop}", NS3))
        if n:
            ctx.unsupported[f"brid:{prop}"] += n
    addr = el.findall("brid:address", NS3)
    if addr:
        props["address_xml"] = json.dumps([etree.tostring(a, encoding="unicode") for a in addr])
    for i, w in enumerate(el.findall("brid:bridgePart", NS3)):
        bp = w.find("brid:BridgePart", NS3)
        if bp is None:
            continue
        pid, synth = _child_id3(bp, node_id, "BridgePart")
        pprops = {"id": pid, "feature_type": "BridgePart", "source": source, **ctx.prov}
        if synth:
            pprops["synthetic_id"] = True
        _semantic_props3(bp, pid, pprops, ctx, batch)
        _module_attrs3(bp, "brid", _CFU3 + [("is_movable", "isMovable", "text")], pprops)
        _collect_bridge_body3(bp, pid, pprops, source, batch, ctx)
        batch["sub_features"].append({"labels": "BridgePart", "props": pprops})
        _add_sub_bbox3(batch, ctx, pid, bp)
        batch["child_rels"].append({"rel": "HAS_BRIDGE_PART", "src": node_id, "tgt": pid,
                                    "props": {"order": i}})
        ctx.stats["sub:BridgePart"] += 1


def _bbox_map3(root):
    """One post-order pass over a top-level feature: the coordinate bbox of every
    element (union of its subtree, template-local relativeGeometry excluded), keyed by
    the lxml element. Sub-features and boundary surfaces look their own element up, so
    a TrafficSpace's bbox is the union of its boundaries and lod0 curves and a Section's
    the union of its spaces — computed once per feature, not once per nesting level."""
    xf = getattr(_tls, "reproj", None)
    out = {}
    gml_pref = f"{{{_GML3}}}"

    def walk(e):
        tag = e.tag
        if not isinstance(tag, str) or tag == _RELGEOM3:
            return None
        if tag == _POSLIST3 or tag == _POS3:
            if not e.text:
                return None
            vals = e.text.split()
            dim = _dim_of(e, len(vals))
            bb = None
            for i in range(0, len(vals) - dim + 1, dim):
                try:
                    x, y = float(vals[i]), float(vals[i + 1])
                    z = float(vals[i + 2]) if dim > 2 else 0.0
                except ValueError:
                    continue
                if xf is not None:
                    x, y, z = xf.point(x, y, z)
                if bb is None:
                    bb = [x, y, z, x, y, z]
                else:
                    if x < bb[0]: bb[0] = x
                    if y < bb[1]: bb[1] = y
                    if z < bb[2]: bb[2] = z
                    if x > bb[3]: bb[3] = x
                    if y > bb[4]: bb[4] = y
                    if z > bb[5]: bb[5] = z
            return bb
        bb = None
        for c in e:
            cb = walk(c)
            if cb is None:
                continue
            if bb is None:
                bb = list(cb)
            else:
                bb = [min(bb[0], cb[0]), min(bb[1], cb[1]), min(bb[2], cb[2]),
                      max(bb[3], cb[3]), max(bb[4], cb[4]), max(bb[5], cb[5])]
        if bb is not None and not tag.startswith(gml_pref):
            out[e] = bb
        return bb

    walk(root)
    return out


def _add_sub_bbox3(batch, ctx, nid, el):
    bb = ctx.el_bbox.get(el)
    if bb is not None:
        batch["sub_bbox"].append({"id": nid, "minx": bb[0], "miny": bb[1], "minz": bb[2],
                                  "maxx": bb[3], "maxy": bb[4], "maxz": bb[5]})


def _feature_bbox3(el, props, ctx=None):
    """Feature bbox: the envelope when present, else every coordinate of the feature
    except template-local implicit geometry (implicit instances are added post-parse
    from prototype extent × matrix + reference point)."""
    if props.get("bbox_lower_corner") and props.get("bbox_upper_corner"):
        lc = _parse_corner(props["bbox_lower_corner"])
        uc = _parse_corner(props["bbox_upper_corner"])
        return [lc[0], lc[1], lc[2], uc[0], uc[1], uc[2]]
    if ctx is not None and el in ctx.el_bbox:
        return list(ctx.el_bbox[el])
    acc = [float("inf")] * 3 + [float("-inf")] * 3
    found = False
    xf = getattr(_tls, "reproj", None)
    stack = [el]
    while stack:
        e = stack.pop()
        tag = e.tag
        if not isinstance(tag, str) or tag == _RELGEOM3:
            continue
        if tag == _POSLIST3 or tag == _POS3:
            if not e.text:
                continue
            vals = e.text.split()
            dim = _dim_of(e, len(vals))
            for i in range(0, len(vals) - dim + 1, dim):
                try:
                    x, y = float(vals[i]), float(vals[i + 1])
                    z = float(vals[i + 2]) if dim > 2 else 0.0
                except ValueError:
                    continue
                if xf is not None:
                    x, y, z = xf.point(x, y, z)
                found = True
                acc[0], acc[3] = min(acc[0], x), max(acc[3], x)
                acc[1], acc[4] = min(acc[1], y), max(acc[4], y)
                acc[2], acc[5] = min(acc[2], z), max(acc[5], z)
            continue
        stack.extend(e)
    return acc if found else None


def _add_bbox3(batch, ctx, fid, bb):
    if bb is None:
        return
    batch["bbox_union"].append({"id": fid, "minx": bb[0], "miny": bb[1], "minz": bb[2],
                                "maxx": bb[3], "maxy": bb[4], "maxz": bb[5]})
    for i in range(3):
        ctx.ds_bbox[i] = min(ctx.ds_bbox[i], bb[i])
        ctx.ds_bbox[i + 3] = max(ctx.ds_bbox[i + 3], bb[i + 3])


def _collect_building3(el, source, batch, ctx, parent_id=None, order=0):
    """bldg:Building / bldg:BuildingPart (3.0) → the SAME graph pattern as 2.0
    buildings (Building|BuildingPart node, HAS_BOUNDARY → BoundarySurface → HAS_POLYGON,
    HAS_LOD_SOLID, HAS_LOD_SURFACE {lod, role}, HAS_TERRAIN_INTERSECTION,
    HAS_LOD_MULTICURVE, HAS_BUILDING_PART, HAS_OUTER/INTERIOR_INSTALLATION,
    HAS_INTERIOR_ROOM, HAS_INTERIOR_FURNITURE) plus what only 3.0 has: HAS_SUBDIVISION
    (Storey / BuildingUnit), HAS_NESTED (BuildingConstructiveElement, with its con:filling
    Windows / Doors via HAS_OPENING) and HAS_POINT_CLOUD."""
    is_part = parent_id is not None
    gid = el.get(GML3_ID)
    if not gid:
        if not is_part:
            ctx.unsupported["top-level feature without gml:id (skipped)"] += 1
            return None
        gid = f"{parent_id}_part_{_hash10(el)}"
    props = {"id": gid, "name": gid, "source": source, **ctx.prov}
    if is_part and not el.get(GML3_ID):
        props["synthetic_id"] = True
    _semantic_props3(el, gid, props, ctx, batch, top_level=not is_part)
    _cfu_codes3(el, "bldg", props)
    vals = _codes3(el, "bldg", "roofType")
    if vals:
        props["roof_type_code"] = vals[0][0]
        if vals[0][1]:
            props["roof_type_codespace"] = vals[0][1]
        if len(vals) > 1:
            props["roof_type_code_list"] = [v for v, _ in vals]
    for key, local in (("storeys_above_ground", "storeysAboveGround"),
                       ("storeys_below_ground", "storeysBelowGround")):
        v = _t3(el, f"bldg:{local}")
        if v is not None:
            try:
                props[key] = int(v)
            except ValueError:
                pass
    for key, local in (("storey_heights_above_ground", "storeyHeightsAboveGround"),
                       ("storey_heights_below_ground", "storeyHeightsBelowGround")):
        e = el.find(f"bldg:{local}", NS3)
        if e is not None and e.text:
            props[key] = e.text.strip()
            if e.get("uom"):
                props[f"{key}_uom"] = e.get("uom")
    _construction_props3(el, props, ctx)
    addr = el.findall("bldg:address", NS3)
    if addr:
        props["address_xml"] = json.dumps([etree.tostring(a, encoding="unicode") for a in addr])
    if not ctx.gemeinde_key and props.get("gemeindeschluessel"):
        ctx.gemeinde_key = props["gemeindeschluessel"]
    _collect_space_common3(el, gid, source, batch, ctx, ms_role="multisurface")
    _collect_building_children3(el, gid, source, batch, ctx)
    for i, w in enumerate(el.findall("bldg:buildingSubdivision", NS3)):
        sub = _first_child3(w)
        if w.get(XLINK_HREF) or sub is None:
            ctx.unsupported["bldg:buildingSubdivision given by reference"] += 1
            continue
        _collect_subdivision3(sub, gid, i, source, batch, ctx)
    for i, w in enumerate(el.findall("bldg:buildingPart", NS3)):
        bp = w.find("bldg:BuildingPart", NS3)
        if bp is not None:
            _collect_building3(bp, source, batch, ctx, parent_id=gid, order=i)
    if is_part:
        batch["building_parts"].append(props)
        _add_sub_bbox3(batch, ctx, gid, el)
        batch["bldg_part_rels"].append({"src": parent_id, "tgt": gid, "order": order})
        ctx.stats["sub:BuildingPart"] += 1
        return gid
    bb = _feature_bbox3(el, props, ctx)
    if bb is not None:
        cx, cy = (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0
        fn_label = BUILDING_FUNCTION_NAMES.get(props.get("function_code") or "", "Building")
        rt_label = ROOF_TYPE_NAMES.get(props.get("roof_type_code") or "", "")
        mh, st = props.get("measured_height"), props.get("storeys_above_ground")
        props["description"] = (
            f"{fn_label}{f', {mh} m tall' if mh is not None else ''}"
            f"{f', {st} storeys' if st is not None else ''}"
            f"{f', {rt_label} roof' if rt_label else ''}, at ({cx:.1f}, {cy:.1f})")
    props["rtree_layer"] = ctx.rtree_layer
    batch["buildings3"].append(props)
    _add_bbox3(batch, ctx, gid, bb)
    ctx.top_ids.append(gid)
    ctx.building_ids.append(gid)
    ctx.stats["top:Building"] += 1
    return gid


def _collect_city_object3(el, spec, source, batch, ctx):
    ft, prefix, kind, attrs = spec
    fid = el.get(GML3_ID)
    if not fid:
        ctx.unsupported["top-level feature without gml:id (skipped)"] += 1
        return None
    props = {"id": fid, "name": fid, "feature_type": ft, "source": source,
             "rtree_layer": ctx.rtree_layer, **ctx.prov}
    _semantic_props3(el, fid, props, ctx, batch, top_level=True)
    _module_attrs3(el, prefix, attrs, props)
    if kind == "transport":
        _collect_space_common3(el, fid, source, batch, ctx, boundary_prop="boundary")
        _collect_transport_children3(el, fid, props, source, batch, ctx)
    elif kind == "construction":
        _construction_props3(el, props, ctx)
        _collect_space_common3(el, fid, source, batch, ctx)
    elif kind == "bridge":
        _collect_bridge_body3(el, fid, props, source, batch, ctx)
    elif kind == "thematic":
        _collect_space_geometry3(el, fid, source, batch, ctx)
    else:
        _collect_space_common3(el, fid, source, batch, ctx)
    batch["city_objects3"].append(props)
    _add_bbox3(batch, ctx, fid, _feature_bbox3(el, props, ctx))
    ctx.top_ids.append(fid)
    ctx.stats["top:" + ft] += 1
    return fid


def _collect_member3(feat_el, source, batch, ctx):
    ctx.local_ids = {e.get(GML3_ID) for e in feat_el.iter()
                     if isinstance(e.tag, str) and e.get(GML3_ID)}
    ctx.el_bbox = _bbox_map3(feat_el)
    if feat_el.tag == _V3_BUILDING_TAG:
        return _collect_building3(feat_el, source, batch, ctx)
    spec = _V3_TOP.get(feat_el.tag)
    if spec is not None:
        return _collect_city_object3(feat_el, spec, source, batch, ctx)
    q = etree.QName(feat_el)
    pref = next((p for p, u in NS3.items() if u == q.namespace), q.namespace)
    ctx.unsupported[f"top-level {pref}:{q.localname} (skipped)"] += 1
    return None


# ── flush helpers for the 3.0 batch keys (called from _flush_batch) ─────────

_SOURCE_FILES_SET = (
    "SET n.source_files = CASE WHEN row.source IN coalesce(n.source_files, []) "
    "THEN n.source_files ELSE coalesce(n.source_files, []) + row.source END")


def _merge_existing_gen_maps(sess, rows):
    """Union the stored _gen_attrs_map with the incoming one: the same Road/Section/
    TrafficSpace gml:id arrives from several tile files and must accumulate."""
    ids = [r["id"] for r in rows]
    existing = {r["id"]: r["m"] for r in sess.run(
        "UNWIND $ids AS i MATCH (n:Entity {id: i}) "
        "WHERE n._gen_attrs_map IS NOT NULL AND n._gen_attrs_map <> '{}' "
        "RETURN n.id AS id, n._gen_attrs_map AS m", ids=ids)}
    for r in rows:
        old = existing.get(r["id"])
        if old and old != r.get("_gen_attrs_map"):
            try:
                merged = json.loads(old)
                merged.update(json.loads(r.get("_gen_attrs_map") or "{}"))
                r["_gen_attrs_map"] = json.dumps(merged)
            except ValueError:
                pass


def _flush_v3_feature_nodes(sess, batch, cv=None):
    if batch.get("buildings3"):
        rows = batch["buildings3"]
        _merge_existing_gen_maps(sess, rows)
        sess.run("UNWIND $rows AS row MERGE (n:Building {id: row.id}) SET n += row, n:Entity"
                 + _CV_SET + " " + _SOURCE_FILES_SET, rows=rows, cv=cv)
    if batch.get("city_objects3"):
        groups = _defaultdict(list)
        for r in batch["city_objects3"]:
            groups[r["feature_type"]].append(r)
        for ft, rows in groups.items():
            if not _SAFE_LABELS_RE.match(ft):
                continue
            _merge_existing_gen_maps(sess, rows)
            sess.run(f"UNWIND $rows AS row MERGE (n:CityObject {{id: row.id}}) "
                     f"SET n += row, n:Entity, n:{ft}" + _CV_SET + " " + _SOURCE_FILES_SET,
                     rows=rows, cv=cv)
    if batch.get("sub_features"):
        groups = _defaultdict(list)
        for r in batch["sub_features"]:
            groups[r["labels"]].append(r["props"])
        for labels, rows in groups.items():
            if not _SAFE_LABELS_RE.match(labels):
                continue
            first, *rest = labels.split(":")
            extra = "".join(f", n:{lab}" for lab in rest)
            _merge_existing_gen_maps(sess, rows)
            sess.run(f"UNWIND $rows AS row MERGE (n:{first} {{id: row.id}}) "
                     f"SET n += row, n:Entity{extra}" + _CV_SET + " " + _SOURCE_FILES_SET,
                     rows=rows, cv=cv)


def _flush_v3_extra_nodes(sess, batch, cv=None):
    if batch.get("prototypes"):
        sess.run("UNWIND $rows AS row MERGE (n:GeometryPrototype {id: row.id}) "
                 "SET n += row, n:Entity", rows=batch["prototypes"])
    if batch.get("appearances"):
        sess.run("UNWIND $rows AS row MERGE (n:Appearance {id: row.id}) SET n += row, n:Entity"
                 + _CV_SET, rows=batch["appearances"], cv=cv)
    if batch.get("surface_data"):
        groups = _defaultdict(list)
        for r in batch["surface_data"]:
            groups[r["label"]].append(r["props"])
        for label, rows in groups.items():
            if _SAFE_LABELS_RE.match(label):
                sess.run(f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) "
                         f"SET n += row, n:Entity" + _CV_SET, rows=rows, cv=cv)
    if batch.get("surface_labels"):
        groups = _defaultdict(list)
        for r in batch["surface_labels"]:
            groups[r["label"]].append(r["id"])
        for label, ids in groups.items():
            if _SAFE_LABELS_RE.match(label):
                sess.run(f"UNWIND $ids AS i MATCH (n:BoundarySurface {{id: i}}) SET n:{label}",
                         ids=ids)
    if batch.get("polygon_props"):
        sess.run("UNWIND $rows AS row MATCH (n:GeometryPolygon {id: row.id}) SET n += row.props",
                 rows=batch["polygon_props"])
    if batch.get("bbox_union"):
        _apply_bbox_union(sess, batch["bbox_union"])
    if batch.get("sub_bbox"):
        _apply_sub_bbox(sess, batch["sub_bbox"])


_SUB_BBOX_SET = (
    "SET n.bbox_min_x = CASE WHEN n.bbox_min_x IS NULL OR row.minx < n.bbox_min_x THEN row.minx ELSE n.bbox_min_x END, "
    "    n.bbox_min_y = CASE WHEN n.bbox_min_y IS NULL OR row.miny < n.bbox_min_y THEN row.miny ELSE n.bbox_min_y END, "
    "    n.bbox_min_z = CASE WHEN n.bbox_min_z IS NULL OR row.minz < n.bbox_min_z THEN row.minz ELSE n.bbox_min_z END, "
    "    n.bbox_max_x = CASE WHEN n.bbox_max_x IS NULL OR row.maxx > n.bbox_max_x THEN row.maxx ELSE n.bbox_max_x END, "
    "    n.bbox_max_y = CASE WHEN n.bbox_max_y IS NULL OR row.maxy > n.bbox_max_y THEN row.maxy ELSE n.bbox_max_y END, "
    "    n.bbox_max_z = CASE WHEN n.bbox_max_z IS NULL OR row.maxz > n.bbox_max_z THEN row.maxz ELSE n.bbox_max_z END")


def _apply_sub_bbox(sess, rows):
    """bbox_min/max_x/y/z on sub-features and boundary surfaces (no center / WKT /
    R-tree: they are not spatially indexed), union-merged like the top-level bbox so a
    Section or TrafficSpace continued in another tile grows with it."""
    sess.run("UNWIND $rows AS row MATCH (n:Entity {id: row.id}) " + _SUB_BBOX_SET, rows=rows)


def _apply_bbox_union(sess, rows):
    """Union-merge feature bboxes (a Road spanning tiles grows with every tile) and
    derive center/location/bbox_wkt (+ ground_z on Buildings) from the union."""
    sess.run(
        "UNWIND $rows AS row MATCH (n:Entity {id: row.id}) "
        "WITH n, row, [n.bbox_min_x, n.bbox_min_y, n.bbox_max_x, n.bbox_max_y] AS old "
        "SET n.bbox_min_x = CASE WHEN n.bbox_min_x IS NULL OR row.minx < n.bbox_min_x THEN row.minx ELSE n.bbox_min_x END, "
        "    n.bbox_min_y = CASE WHEN n.bbox_min_y IS NULL OR row.miny < n.bbox_min_y THEN row.miny ELSE n.bbox_min_y END, "
        "    n.bbox_min_z = CASE WHEN n.bbox_min_z IS NULL OR row.minz < n.bbox_min_z THEN row.minz ELSE n.bbox_min_z END, "
        "    n.bbox_max_x = CASE WHEN n.bbox_max_x IS NULL OR row.maxx > n.bbox_max_x THEN row.maxx ELSE n.bbox_max_x END, "
        "    n.bbox_max_y = CASE WHEN n.bbox_max_y IS NULL OR row.maxy > n.bbox_max_y THEN row.maxy ELSE n.bbox_max_y END, "
        "    n.bbox_max_z = CASE WHEN n.bbox_max_z IS NULL OR row.maxz > n.bbox_max_z THEN row.maxz ELSE n.bbox_max_z END "
        "WITH n, old "
        # an already indexed feature whose extent grew (a Road continued in the next
        # tile) is flagged; the R-tree passes remove and re-add it
        "SET n.rtree_stale = CASE WHEN old[0] IS NOT NULL "
        "      AND old <> [n.bbox_min_x, n.bbox_min_y, n.bbox_max_x, n.bbox_max_y] "
        "      AND EXISTS { MATCH ()-[:RTREE_REFERENCE]->(n) } THEN true ELSE n.rtree_stale END "
        "WITH n, old "
        "SET n.center_x = (n.bbox_min_x + n.bbox_max_x) / 2.0, "
        "    n.center_y = (n.bbox_min_y + n.bbox_max_y) / 2.0, "
        "    n.location = point({x: (n.bbox_min_x + n.bbox_max_x) / 2.0, "
        "                        y: (n.bbox_min_y + n.bbox_max_y) / 2.0}), "
        "    n.ground_z = CASE WHEN n:Building THEN n.bbox_min_z ELSE n.ground_z END "
        # a degenerate extent (axis-parallel fence, single pole) gets a ±0.5 mm padded
        # WKT so it still enters the R-tree; bbox_min/max stay exact
        # (bbox_wkt is only rewritten when the extent changed: neo4j-spatial normalises
        #  the WKT text when it indexes a node, and re-ingest must not touch it)
        "WITH n, old, CASE WHEN n.bbox_max_x > n.bbox_min_x THEN 0.0 ELSE 0.0005 END AS px, "
        "        CASE WHEN n.bbox_max_y > n.bbox_min_y THEN 0.0 ELSE 0.0005 END AS py "
        "WHERE n.bbox_wkt IS NULL OR old <> [n.bbox_min_x, n.bbox_min_y, n.bbox_max_x, n.bbox_max_y] "
        "SET n.bbox_wkt = 'POLYGON((' + toString(n.bbox_min_x - px) + ' ' + toString(n.bbox_min_y - py) + ', ' "
        "        + toString(n.bbox_max_x + px) + ' ' + toString(n.bbox_min_y - py) + ', ' "
        "        + toString(n.bbox_max_x + px) + ' ' + toString(n.bbox_max_y + py) + ', ' "
        "        + toString(n.bbox_min_x - px) + ' ' + toString(n.bbox_max_y + py) + ', ' "
        "        + toString(n.bbox_min_x - px) + ' ' + toString(n.bbox_min_y - py) + '))'",
        rows=rows)


def _flush_v3_rels(sess, batch):
    if batch.get("child_rels"):
        groups = _defaultdict(list)
        for r in batch["child_rels"]:
            groups[r["rel"]].append(r)
        for rel, rows in groups.items():
            if _SAFE_REL_RE.match(rel):
                sess.run(f"UNWIND $rows AS row MATCH (a:Entity {{id: row.src}}) "
                         f"MATCH (b:Entity {{id: row.tgt}}) MERGE (a)-[r:{rel}]->(b) "
                         f"SET r += row.props", rows=rows)
    if batch.get("proto_rels"):
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}) "
                 "MATCH (b:GeometryPrototype {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_PROTOTYPE {lod: row.lod}]->(b) SET r += row.props",
                 rows=batch["proto_rels"])
    if batch.get("solid_member_rels"):
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}) "
                 "MATCH (b:GeometrySolid {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_SOLID_MEMBER {order: row.order}]->(b) "
                 "SET r.via_xlink = row.via_xlink", rows=batch["solid_member_rels"])
    if batch.get("line_rels"):
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}) "
                 "MATCH (b:GeometryLineString {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_LINE {order: row.order}]->(b)", rows=batch["line_rels"])
    if batch.get("multicurve_rels3"):
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}) "
                 "MATCH (b:GeometryLineString {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_LOD_MULTICURVE]->(b) SET r.lod = row.lod, "
                 "r.order = row.order, r.multi_curve_id = row.multi_curve_id, r.role = row.role",
                 rows=batch["multicurve_rels3"])
    if batch.get("point_rels"):
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}) "
                 "MATCH (b:GeometryLineString {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_LOD_POINT]->(b) SET r.lod = row.lod", rows=batch["point_rels"])
    if batch.get("appearance_rels"):
        sess.run("UNWIND $rows AS row MATCH (a:Entity {id: row.src}) "
                 "MATCH (b:Appearance {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_APPEARANCE]->(b) SET r.order = row.order",
                 rows=batch["appearance_rels"])
    if batch.get("surface_data_rels"):
        sess.run("UNWIND $rows AS row MATCH (a:Appearance {id: row.src}) "
                 "MATCH (b:Entity {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_SURFACE_DATA]->(b) SET r.order = row.order",
                 rows=batch["surface_data_rels"])


# ── deferred / pending references ─────────────────────────────────────────────

def _resolve_refs3(sess, rows):
    """MERGE every deferred edge whose two endpoints exist; return the unresolved rows.
    An Appearance given by reference on a feature (HAS_APPEARANCE_REF) resolves to
    HAS_APPEARANCE."""
    unresolved = []
    # A geometry-property xlink (e.g. veg:PlantCover core:lod0MultiSurface
    # xlink:href="#<MultiSurface of an AuxiliaryTrafficArea>") may target the primary
    # MultiSurface of a BoundarySurface, which the model stores as multi_surface_id +
    # HAS_POLYGON (no node). Materialise exactly those referenced MultiSurfaces as a
    # GeometryMultiSurface with the same members (BoundarySurface -[:HAS_LOD_SURFACE
    # {lod, role:'primary'}]-> it), so the xlink becomes a normal edge; exporters write
    # the surface inline once and the referencing feature as xlink:href.
    ms_tgts = sorted({r["tgt"] for r in rows if r["rel"] in ("HAS_LOD_SURFACE",)})
    for j in range(0, len(ms_tgts), 2000):
        sess.run(
            "UNWIND $tgts AS t MATCH (s:BoundarySurface {multi_surface_id: t}) "
            "WHERE NOT EXISTS { MATCH (:GeometryMultiSurface {id: t}) } "
            "MERGE (m:GeometryMultiSurface {id: t}) "
            "SET m:Entity, m.lod = s.lod, m.source = s.source, m.materialized_from = s.id "
            "MERGE (s)-[r:HAS_LOD_SURFACE {lod: s.lod}]->(m) SET r.role = 'primary' "
            "WITH s, m MATCH (s)-[hp:HAS_POLYGON]->(p) "
            "MERGE (m)-[r2:HAS_SURFACE_MEMBER {order: hp.order}]->(p) "
            "SET r2.via_xlink = hp.via_xlink, r2.orientation = hp.orientation",
            tgts=ms_tgts[j:j + 2000])
    groups = _defaultdict(list)
    for i, r in enumerate(rows):
        rel = "HAS_APPEARANCE" if r["rel"] == "HAS_APPEARANCE_REF" else r["rel"]
        groups[(rel, tuple(sorted(r["key"])))].append(dict(r, i=i))
    for (rel, keys), grp in groups.items():
        if not _SAFE_REL_RE.match(rel):
            unresolved.extend(grp)
            continue
        keymap = ", ".join(f"{k}: row.key.{k}" for k in keys)
        keymap = f" {{{keymap}}}" if keymap else ""
        # the target must come from the referenced document (rows parked before `doc`
        # was recorded resolve as before)
        stmt = (f"UNWIND $rows AS row MATCH (a:Entity {{id: row.src}}) "
                f"MATCH (b:Entity {{id: row.tgt}}) "
                f"WHERE row.doc IS NULL OR b.source = row.doc "
                f"OR row.doc IN coalesce(b.source_files, []) "
                f"MERGE (a)-[r:{rel}{keymap}]->(b) "
                f"SET r += row.props RETURN DISTINCT row.i AS i")
        done = set()
        for j in range(0, len(grp), 2000):
            chunk = grp[j:j + 2000]
            for attempt in range(5):
                try:
                    done.update(rec["i"] for rec in sess.run(stmt, rows=chunk))
                    break
                except TransientError:
                    time.sleep(0.2 * (attempt + 1))
        unresolved.extend(r for r in grp if r["i"] not in done)
    for r in unresolved:
        r.pop("i", None)
    # a reference into a Dataset with another id prefix (or none): try the fragment with
    # the target Dataset's prefix instead of ours
    alt = [r for r in unresolved if r.get("tgt0") and r.get("doc") and not r.get("_alt")]
    if alt:
        docs = sorted({r["doc"] for r in alt})
        pref = {rec["doc"]: rec["p"] or "" for rec in sess.run(
            "UNWIND $docs AS doc MATCH (d:Dataset) WHERE doc IN coalesce(d.source_files, []) "
            "RETURN doc, d.id_prefix AS p", docs=docs)}
        retry = [dict(r, tgt=pref[r["doc"]] + r["tgt0"], _alt=True) for r in alt
                 if r["doc"] in pref and pref[r["doc"]] + r["tgt0"] != r["tgt"]]
        if retry:
            def _k(r):
                return (r["src"], r["rel"], r.get("tgt0"), r.get("doc"),
                        json.dumps(r["key"], sort_keys=True))
            still = {_k(r) for r in _resolve_refs3(sess, retry)}
            done = {_k(r) for r in retry} - still
            unresolved = [r for r in unresolved if _k(r) not in done]
    return unresolved


def _store_pending3(sess, unresolved):
    """Unresolved references (target not in the graph yet — typically a feature in a
    tile file that is ingested later) are parked on the source node as
    pending_refs_json + the plumbing label PendingRefs; resolve_pending_refs() retries
    them after every 3.0 ingest (and via `ingest_citygml.py --resolve-pending`)."""
    if not unresolved:
        return
    by_src = _defaultdict(list)
    for r in unresolved:
        by_src[r["src"]].append({k: r[k] for k in ("rel", "tgt", "key", "props", "doc", "tgt0")
                                 if k in r})
    ids = list(by_src)
    existing = {rec["id"]: rec["j"] for rec in sess.run(
        "UNWIND $ids AS i MATCH (n:Entity {id: i}) RETURN n.id AS id, n.pending_refs_json AS j",
        ids=ids)}
    rows = []
    for src, refs in by_src.items():
        if src not in existing:
            continue                       # source node missing: nothing to attach to
        merged = json.loads(existing[src]) if existing[src] else []
        seen = {(m["rel"], m["tgt"], json.dumps(m["key"], sort_keys=True)) for m in merged}
        for ref in refs:
            k = (ref["rel"], ref["tgt"], json.dumps(ref["key"], sort_keys=True))
            if k not in seen:
                merged.append(ref)
                seen.add(k)
        rows.append({"id": src, "j": json.dumps(merged)})
    for j in range(0, len(rows), 2000):
        sess.run("UNWIND $rows AS row MATCH (n:Entity {id: row.id}) "
                 "SET n:PendingRefs, n.pending_refs_json = row.j", rows=rows[j:j + 2000])


def resolve_pending_refs(sess, doc=None):
    """Retry parked (pending) cross-file references: all of them, or with `doc` only those
    into that file (after it was ingested; references parked before `doc` was recorded
    are retried too). Returns (resolved, remaining) of the references retried."""
    recs = sess.run("MATCH (n:PendingRefs) RETURN n.id AS id, n.pending_refs_json AS j").data()
    if not recs:
        return 0, 0
    rows, kept = [], _defaultdict(list)
    for rec in recs:
        for ref in json.loads(rec["j"] or "[]"):
            if doc is None or ref.get("doc") in (None, doc):
                rows.append({"src": rec["id"], **ref})
            else:
                kept[rec["id"]].append(ref)
    if not rows:
        return 0, 0
    retried = {r["src"] for r in rows}
    unresolved = _resolve_refs3(sess, rows)
    left = _defaultdict(list)
    for r in unresolved:
        left[r["src"]].append({k: r[k] for k in ("rel", "tgt", "key", "props", "doc", "tgt0")
                               if k in r})
    upd = [{"id": i, "j": json.dumps(kept[i] + left[i])} for i in retried if kept[i] or left[i]]
    clear = [i for i in retried if not (kept[i] or left[i])]
    if upd:
        sess.run("UNWIND $rows AS row MATCH (n:PendingRefs {id: row.id}) "
                 "SET n.pending_refs_json = row.j", rows=upd)
    if clear:
        sess.run("UNWIND $ids AS i MATCH (n:PendingRefs {id: i}) "
                 "REMOVE n:PendingRefs, n.pending_refs_json", ids=clear)
    return len(rows) - len(unresolved), len(unresolved)


def _backfill_bbox3(sess, dataset_id):
    """Top-level features of the Dataset that still have no bbox — their only geometry
    is a geometry-property xlink (e.g. PlantCover lod0MultiSurface → an
    AuxiliaryTrafficArea's MultiSurface), possibly resolved only now from a later tile —
    get it from the resolved graph geometry (HAS_LOD_SURFACE/HAS_LOD_SOLID/HAS_BOUNDARY →
    polygons; never HAS_PROTOTYPE, whose rings are template-local). Returns their ids."""
    ids = [r["id"] for r in sess.run(
        "MATCH (d:Dataset {id: $did})<-[:PART_OF]-(n) "
        "WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NULL RETURN n.id AS id",
        did=dataset_id)]
    done = []
    for j in range(0, len(ids), 500):
        rows = []
        for rec in sess.run(
                "UNWIND $ids AS i MATCH (n:Entity {id: i}) "
                "MATCH (n)-[:HAS_LOD_SURFACE|HAS_LOD_SOLID|HAS_BOUNDARY]->(g)"
                "-[:HAS_SURFACE_MEMBER|HAS_POLYGON*1..2]->(:GeometryPolygon)"
                "-[:HAS_EXTERIOR_RING|HAS_INTERIOR_RING]->(r) "
                "RETURN i AS id, collect([r.pos_list, coalesce(r.srs_dimension, 3)]) AS pl",
                ids=ids[j:j + 500]):
            acc = [float("inf")] * 3 + [float("-inf")] * 3
            for pl, dim in rec["pl"]:
                vals = (pl or "").split()
                for k in range(0, len(vals) - dim + 1, dim):
                    try:
                        x, y = float(vals[k]), float(vals[k + 1])
                        z = float(vals[k + 2]) if dim > 2 else 0.0
                    except ValueError:
                        continue
                    acc = [min(acc[0], x), min(acc[1], y), min(acc[2], z),
                           max(acc[3], x), max(acc[4], y), max(acc[5], z)]
            if acc[0] != float("inf"):
                rows.append({"id": rec["id"], "minx": acc[0], "miny": acc[1], "minz": acc[2],
                             "maxx": acc[3], "maxy": acc[4], "maxz": acc[5]})
        if rows:
            _apply_bbox_union(sess, rows)
            done.extend(r["id"] for r in rows)
    return done


# containment edges below a top-level feature (sub-features, surfaces, openings)
_BBOX_CHILD_RELS = ("HAS_SECTION", "HAS_INTERSECTION", "HAS_TRAFFIC_SPACE", "HAS_MARKING",
                    "HAS_HOLE", "HAS_CLEARANCE_SPACE", "HAS_BOUNDARY", "HAS_OPENING",
                    "HAS_NESTED", "HAS_BRIDGE_PART", "HAS_BUILDING_PART",
                    "HAS_OUTER_INSTALLATION", "HAS_INTERIOR_INSTALLATION", "HAS_INTERIOR_ROOM",
                    "HAS_ROOM_INSTALLATION", "HAS_INTERIOR_FURNITURE", "HAS_SUBDIVISION",
                    "HAS_POINT_CLOUD")
_BBOX_GEOM_Q = (
    "UNWIND $ids AS i MATCH (x:Entity {id: i}) "
    "CALL (x) { "
    "  MATCH (x)-[:HAS_POLYGON]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING|HAS_INTERIOR_RING]->(r) "
    "  RETURN r.pos_list AS pl, r.srs_dimension AS d "
    "  UNION ALL "
    "  MATCH (x)-[h:HAS_LOD_SURFACE|HAS_LOD_GEOMETRY]->(m:GeometryMultiSurface) "
    "  WHERE coalesce(h.role, '') <> 'primary' AND NOT coalesce(m.is_implicit, false) "
    "  MATCH (m)-[:HAS_SURFACE_MEMBER*1..2]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING|HAS_INTERIOR_RING]->(r) "
    "  RETURN r.pos_list AS pl, r.srs_dimension AS d "
    "  UNION ALL "
    "  MATCH (x)-[:HAS_LOD_SOLID]->(:GeometrySolid)-[:HAS_SURFACE_MEMBER|HAS_SOLID_MEMBER*1..3]->"
    "        (:GeometryPolygon)-[:HAS_EXTERIOR_RING|HAS_INTERIOR_RING]->(r) "
    "  RETURN r.pos_list AS pl, r.srs_dimension AS d "
    "  UNION ALL "
    "  MATCH (x)-[:HAS_LOD_MULTICURVE|HAS_LOD_POINT]->(l:GeometryLineString) "
    "  RETURN l.pos_list AS pl, l.srs_dimension AS d "
    "  UNION ALL "
    "  MATCH (x)-[:HAS_TERRAIN_INTERSECTION]->()-[:HAS_LINE]->(l:GeometryLineString) "
    "  RETURN l.pos_list AS pl, l.srs_dimension AS d "
    "} "
    "RETURN i AS id, collect([pl, d]) AS geo")
_BBOX_CHILD_Q = (
    "UNWIND $ids AS i MATCH (x:Entity {id: i})-[:" + "|".join(_BBOX_CHILD_RELS) + "]->(c) "
    "WHERE c.bbox_min_x IS NOT NULL "
    "RETURN i AS id, min(c.bbox_min_x) AS x0, min(c.bbox_min_y) AS y0, min(c.bbox_min_z) AS z0, "
    "       max(c.bbox_max_x) AS x1, max(c.bbox_max_y) AS y1, max(c.bbox_max_z) AS z1")


def backfill_subfeature_bbox(sess, dataset_id=None, top_batch=200, chunk=1000, log=print):
    """--backfill-bbox: bbox_min/max_x/y/z for sub-features (Section, Intersection,
    TrafficSpace, AuxiliaryTrafficSpace, Hole, ClearanceSpace, Marking, BridgePart,
    NestedFeature, BuildingPart, installations, rooms) and BoundarySurfaces/Openings of a
    graph ingested before the 3.0 codec stored them — the same values the codec now
    writes at ingest: own geometry (polygons, MultiSurfaces, solids, curves, points,
    terrain curves; never template-local HAS_PROTOTYPE geometry) ∪ the bbox of the
    contained sub-features/surfaces.

    Scope: the Dataset `dataset_id`, else every CityGML 3.0 Dataset. Work is done per
    batch of `top_batch` top-level features that have sub-features; inside a batch the
    missing nodes are processed DEEPEST FIRST (a child is always deeper than its parent),
    so every value written is final. Only nodes still missing bbox_min_x are touched:
    idempotent and resumable (an interrupted run continues where it stopped). Each
    query handles ≤ `chunk` nodes, so it runs with a 2 GB Neo4j heap."""
    if dataset_id:
        dids = [dataset_id]
    else:
        dids = [r["id"] for r in sess.run(
            "MATCH (d:Dataset) WHERE d.citygml_version STARTS WITH '3' RETURN d.id AS id")]
    rels = "|".join(_BBOX_CHILD_RELS)
    stats = _Counter()
    t0 = time.perf_counter()
    for did in dids:
        tops = [r["id"] for r in sess.run(
            "MATCH (d:Dataset {id: $did})<-[:PART_OF]-(f) "
            f"WHERE EXISTS {{ MATCH (f)-[:{rels}]->() }} RETURN f.id AS id ORDER BY id",
            did=did)]
        log(f"  Dataset {did}: {len(tops)} top-level features with sub-features")
        for bi in range(0, len(tops), top_batch):
            missing = sess.run(
                "UNWIND $tops AS t MATCH (f:Entity {id: t}) "
                f"MATCH p = (f)-[:{rels}*1..8]->(x) WHERE x.bbox_min_x IS NULL "
                "RETURN x.id AS id, max(length(p)) AS depth",
                tops=tops[bi:bi + top_batch]).data()
            by_depth = _defaultdict(list)
            for r in missing:
                by_depth[r["depth"]].append(r["id"])
            for depth in sorted(by_depth, reverse=True):
                ids = by_depth[depth]
                for ci in range(0, len(ids), chunk):
                    part = ids[ci:ci + chunk]
                    box = {}
                    for rec in sess.run(_BBOX_GEOM_Q, ids=part):
                        acc = None
                        for pl, dim in rec["geo"]:
                            vals = (pl or "").split()
                            dim = int(dim or 3)
                            for k in range(0, len(vals) - dim + 1, dim):
                                try:
                                    x, y = float(vals[k]), float(vals[k + 1])
                                    z = float(vals[k + 2]) if dim > 2 else 0.0
                                except ValueError:
                                    continue
                                if acc is None:
                                    acc = [x, y, z, x, y, z]
                                else:
                                    acc = [min(acc[0], x), min(acc[1], y), min(acc[2], z),
                                           max(acc[3], x), max(acc[4], y), max(acc[5], z)]
                        if acc is not None:
                            box[rec["id"]] = acc
                    for rec in sess.run(_BBOX_CHILD_Q, ids=part):
                        cb = [rec["x0"], rec["y0"], rec["z0"], rec["x1"], rec["y1"], rec["z1"]]
                        if any(v is None for v in cb):
                            continue
                        acc = box.get(rec["id"])
                        box[rec["id"]] = cb if acc is None else [
                            min(acc[0], cb[0]), min(acc[1], cb[1]), min(acc[2], cb[2]),
                            max(acc[3], cb[3]), max(acc[4], cb[4]), max(acc[5], cb[5])]
                    rows = [{"id": i, "minx": b[0], "miny": b[1], "minz": b[2],
                             "maxx": b[3], "maxy": b[4], "maxz": b[5]} for i, b in box.items()]
                    if rows:
                        _run_retry(sess, "UNWIND $rows AS row MATCH (n:Entity {id: row.id}) "
                                         "WHERE n.bbox_min_x IS NULL " + _SUB_BBOX_SET, rows=rows)
                    stats["written"] += len(rows)
                    stats["no_geometry"] += len(part) - len(rows)
            done = min(bi + top_batch, len(tops))
            if done % (top_batch * 25) < top_batch or done == len(tops):
                log(f"    {done}/{len(tops)} top-level features, {stats['written']:,} bboxes "
                    f"written ({time.perf_counter() - t0:.0f}s)")
    log(f"  Backfill: {stats['written']:,} sub-feature/surface bboxes written, "
        f"{stats['no_geometry']:,} without geometry, {time.perf_counter() - t0:.0f}s")
    return dict(stats)


def _create_indexes3(sess):
    for label in _V3_INDEX_LABELS:
        try:
            sess.run(f"CREATE INDEX {label.lower()}_id_idx IF NOT EXISTS FOR (n:{label}) ON (n.id)")
        except Exception:
            pass
    try:
        sess.run("CREATE INDEX pendingrefs_id_idx IF NOT EXISTS FOR (n:PendingRefs) ON (n.id)")
    except Exception:
        pass
    try:   # xlink targets that are the primary MultiSurface of a BoundarySurface
        sess.run("CREATE INDEX boundarysurface_multi_surface_id_idx IF NOT EXISTS "
                 "FOR (n:BoundarySurface) ON (n.multi_surface_id)")
    except Exception:
        pass


def _reindex_stale(sess, ids=None):
    """Remove the R-tree entry of features flagged rtree_stale (extent grew after they
    were indexed) so the following addNodes re-inserts them with the new bbox. Only 3.0
    ingests set the flag, so this is a no-op on 2.0-only graphs."""
    q = ("UNWIND $ids AS i MATCH (n:Entity {id: i}) WHERE n.rtree_stale = true "
         if ids is not None else
         "MATCH (n) WHERE (n:Building OR n:CityObject) AND n.rtree_stale = true ")
    stale = sess.run(q + "RETURN n.id AS id, coalesce(n.rtree_layer, 'features') AS layer",
                     ids=ids or []).data()
    for r in stale:
        sess.run("MATCH (n:Entity {id: $i}) WHERE ()-[:RTREE_REFERENCE]->(n) "
                 "CALL spatial.removeNode($layer, n) YIELD nodeId RETURN nodeId",
                 i=r["id"], layer=r["layer"]).consume()
        sess.run("MATCH (n:Entity {id: $i}) REMOVE n.rtree_stale", i=r["id"]).consume()
    return len(stale)


def _index_rtree3(sess, ids, layer):
    """Register this file's top-level features in the R-tree layer `layer` (own layer per
    source, GRAPH.md). spatial.addNodes is not idempotent: only unindexed features are
    added; features whose extent grew since they were indexed (rtree_stale, e.g. a Road
    continued in this tile) are removed and re-added."""
    if not ids or not _ensure_spatial_layer(sess, layer):
        return 0
    total = 0
    for i in range(0, len(ids), 5000):
        chunk = ids[i:i + 5000]
        try:
            _reindex_stale(sess, chunk)
            rec = sess.run(
                "UNWIND $ids AS i MATCH (n:Entity {id: i}) "
                "WHERE n.bbox_wkt IS NOT NULL AND NOT ()-[:RTREE_REFERENCE]->(n) "
                "WITH collect(n) AS nodes CALL spatial.addNodes($layer, nodes) YIELD count "
                "RETURN count", ids=chunk, layer=layer).single()
            total += rec["count"] if rec else 0
        except Exception as exc:
            print(f"  WARNING: spatial indexing failed ({exc})")
            break
    return total


def ingest_citygml3(gml_path: Path, driver, num_writers: int = 3, nearby_edges: bool = False,
                    dataset_id: str = None, dataset_name: str = None,
                    target_crs: str = None, source_crs: str = None,
                    skip_spatial_index: bool = False, defer_code_hubs: bool = False,
                    source_type: str = None, method: str = None, method_version: str = None,
                    rtree_layer: str = None, id_prefix: str = None, check_ids: bool = True,
                    district: str = None):
    """CityGML 3.0 → Neo4j (see the codec notes above). Same call contract and return
    shape as ingest(); ingest() dispatches here when the root namespace is 3.0."""
    source = gml_path.name
    layer = rtree_layer or "features"
    prov = {k: v for k, v in (("source_type", source_type), ("method", method),
                              ("method_version", method_version)) if v}
    dataset_id = dataset_id or _slug(gml_path.stem)
    ds_display_name = dataset_name or source
    ctx = _Ctx3(source, dataset_id, prov, layer, id_prefix)
    _tls.srs_dim3 = None

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

    crs, srs_dim, root_lc, root_uc = None, 3, None, None
    batch = _new_batch()
    t0 = time.perf_counter()

    with driver.session(database="neo4j") as sess:
        with _SHARED_NODE_LOCK:
            _create_indexes(sess)
            _create_indexes3(sess)
            sess.run("MERGE (n:Dataset {id: $id}) SET n.name = $name, n.source = $src",
                     id=dataset_id, name=ds_display_name, src=ds_display_name)
            claimed = _claim_dataset_file(sess, dataset_id, source)
            try:
                _record_id_prefix(sess, dataset_id, source, id_prefix)
                if check_ids:
                    _check_id_scope(sess, gml_path, dataset_id, GML3_ID, id_prefix or "")
            except IdScopeError:
                if claimed:
                    _release_dataset_file(sess, dataset_id, source)
                raise

        batch_queue: queue.Queue = queue.Queue(maxsize=num_writers * 2)
        flush_errors: list = []

        def _writer():
            with driver.session(database="neo4j") as wsess:
                while True:
                    item = batch_queue.get()
                    if item is None:
                        batch_queue.task_done()
                        break
                    try:
                        for attempt in range(5):
                            try:
                                _flush_batch(wsess, item, "3.0")
                                break
                            except TransientError:
                                if attempt == 4:
                                    raise
                                time.sleep(0.2 * (attempt + 1))
                    except Exception as exc:
                        flush_errors.append(exc)
                    finally:
                        batch_queue.task_done()

        writers = [threading.Thread(target=_writer, daemon=True) for _ in range(num_writers)]
        for w in writers:
            w.start()

        member_tag = _P3.city_object_member
        app_member_tag = _P3.appearance_member
        env_tag = f"{{{_GML3}}}Envelope"
        bounded_by_tag = f"{{{_GML3}}}boundedBy"
        app_order = 0
        root_gml_id = None
        tags3 = [member_tag, app_member_tag, env_tag]
        events = (_iter_prefixed(gml_path, tags3, id_prefix, GML3_ID) if id_prefix else
                  etree.iterparse(str(gml_path), events=("end",), tag=tags3, huge_tree=True))
        for _, el in events:
            if root_gml_id is None and el.tag != env_tag and el.getparent() is not None:
                root_gml_id = el.getparent().get(GML3_ID) or ""
            if el.tag == env_tag:
                parent = el.getparent()
                if parent is not None and parent.tag == bounded_by_tag and \
                        parent.getparent() is not None and parent.getparent().getparent() is None:
                    crs = el.get("srsName")
                    srs_dim = int(el.get("srsDimension", 3))
                    _tls.srs_dim3 = int(el.get("srsDimension")) if el.get("srsDimension") else None
                    root_lc = _t3(el, "gml:lowerCorner")
                    root_uc = _t3(el, "gml:upperCorner")
                continue
            child = _first_child3(el)
            if el.tag == app_member_tag:
                if child is not None and _ln3(child) == "Appearance":
                    ctx.local_ids = set()
                    aid = _collect_appearance3(child, f"app_{_slug(source)}_{app_order}",
                                               source, batch, ctx)
                    ctx.dataset_appearances.append({"id": aid, "order": app_order})
                    app_order += 1
            elif child is None:
                ctx.unsupported["cityObjectMember by reference"] += 1
            else:
                _collect_member3(child, source, batch, ctx)
            if (len(batch["buildings3"]) + len(batch["city_objects3"]) >= BATCH_SIZE
                    or len(batch["polygons"]) >= _V3_BATCH_POLYGONS):
                batch_queue.put(batch)
                batch = _new_batch()
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]

        if any(batch[k] for k in batch):
            batch_queue.put(batch)
        for _ in writers:
            batch_queue.put(None)
        for w in writers:
            w.join()
        if flush_errors:
            raise flush_errors[0]
        t_parse = time.perf_counter() - t0

        # ── implicit-geometry instances: bbox = matrix·(prototype extent) + ref point
        if ctx.implicit_jobs:
            missing = {p for _f, _m, _r, p in ctx.implicit_jobs if p not in ctx.proto_bbox}
            if missing:
                for rec in sess.run(
                        "UNWIND $ids AS i MATCH (p:GeometryPrototype {id: i}) "
                        "WHERE p.local_min_x IS NOT NULL RETURN p.id AS id, "
                        "[p.local_min_x, p.local_min_y, p.local_min_z, p.local_max_x, "
                        " p.local_max_y, p.local_max_z] AS bb", ids=list(missing)):
                    ctx.proto_bbox[rec["id"]] = rec["bb"]
            rows = []
            for fid, m, ref, pid in ctx.implicit_jobs:
                bb = ctx.proto_bbox.get(pid)
                if bb is None:
                    continue
                pts = []
                for x in (bb[0], bb[3]):
                    for y in (bb[1], bb[4]):
                        for z in (bb[2], bb[5]):
                            pts.append((m[0] * x + m[1] * y + m[2] * z + m[3] + ref[0],
                                        m[4] * x + m[5] * y + m[6] * z + m[7] + ref[1],
                                        m[8] * x + m[9] * y + m[10] * z + m[11] + ref[2]))
                wb = [min(p[0] for p in pts), min(p[1] for p in pts), min(p[2] for p in pts),
                      max(p[0] for p in pts), max(p[1] for p in pts), max(p[2] for p in pts)]
                tmp = {"bbox_union": []}
                _add_bbox3(tmp, ctx, fid, wb)
                rows.extend(tmp["bbox_union"])
            for j in range(0, len(rows), 2000):
                _apply_bbox_union(sess, rows[j:j + 2000])

        # ── CityModel-level appearances
        if ctx.dataset_appearances:
            sess.run("UNWIND $rows AS row MATCH (d:Dataset {id: $did}) "
                     "MATCH (a:Appearance {id: row.id}) MERGE (d)-[r:HAS_APPEARANCE]->(a) "
                     "SET r.order = row.order", rows=ctx.dataset_appearances, did=dataset_id)

        # ── deferred references (xlinks, relations, predecessor/successor, targets)
        unresolved = _resolve_refs3(sess, ctx.deferred)
        _store_pending3(sess, unresolved)
        resolved_pending, still_pending = resolve_pending_refs(sess, doc=source)

        # ── PART_OF (top-level features only; sub-features never)
        for i in range(0, len(ctx.top_ids), 1000):
            _run_retry(sess, "UNWIND $ids AS fid MATCH (n:Entity {id: fid}) "
                             "MATCH (d:Dataset {id: $did}) MERGE (n)-[:PART_OF]->(d)",
                       ids=ctx.top_ids[i:i + 1000], did=dataset_id)

        backfilled = _backfill_bbox3(sess, dataset_id)

        if ctx.building_ids and not defer_code_hubs:
            _derive_code_hubs(sess, source_filter=source)

        # ── Dataset metadata (coalesce / bbox union: safe for tiled datasets)
        cm_name, cm_desc, cm_schema_loc = _read_citymodel_header(gml_path)
        if not crs:
            crs = _peek_srsname(gml_path)
        stored_crs = reproj_target or crs
        lod = f"LOD{ctx.max_lod}" if ctx.max_lod >= 0 else "LOD0"
        _run_retry(sess,
                   "MATCH (n:Dataset {id: $id}) SET n.format = coalesce(n.format, $format), "
                   "n.citygml_version = coalesce(n.citygml_version, '3.0'), "
                   "n.gml_version = coalesce(n.gml_version, '3.2'), "
                   "n.crs = coalesce(n.crs, $crs), n.srs_dimension = coalesce(n.srs_dimension, $sd), "
                   "n.lod = coalesce(n.lod, $lod), n.source_crs = coalesce(n.source_crs, $scrs), "
                   "n.rtree_layer = coalesce(n.rtree_layer, $layer)",
                   id=dataset_id, format="CityGML 3.0", crs=stored_crs, sd=srs_dim, lod=lod,
                   scrs=(crs if reproj_target else None), layer=layer)
        for k, v in list(prov.items()) + [("gml_name", cm_name), ("gml_description", cm_desc),
                                          ("schema_location", cm_schema_loc),
                                          ("gml_id", root_gml_id)]:
            if v:
                _run_retry(sess, f"MATCH (n:Dataset {{id: $id}}) SET n.{k} = coalesce(n.{k}, $v)",
                           id=dataset_id, v=v)
        if root_lc and root_uc:
            lc, uc = _parse_corner(root_lc), _parse_corner(root_uc)
            _run_retry(sess, "MATCH (n:Dataset {id: $id}) SET "
                             "n.bbox_lower_corner = coalesce(n.bbox_lower_corner, $lt), "
                             "n.bbox_upper_corner = coalesce(n.bbox_upper_corner, $ut)",
                       id=dataset_id, lt=root_lc, ut=root_uc)
            box = [lc[0], lc[1], lc[2], uc[0], uc[1], uc[2]]
        else:
            box = ctx.ds_bbox if ctx.ds_bbox[0] != float("inf") else None
        if box:
            _run_retry(sess,
                       "MATCH (n:Dataset {id: $id}) SET "
                       "n.bbox_min_x = CASE WHEN n.bbox_min_x IS NULL OR $b[0] < n.bbox_min_x THEN $b[0] ELSE n.bbox_min_x END, "
                       "n.bbox_min_y = CASE WHEN n.bbox_min_y IS NULL OR $b[1] < n.bbox_min_y THEN $b[1] ELSE n.bbox_min_y END, "
                       "n.bbox_min_z = CASE WHEN n.bbox_min_z IS NULL OR $b[2] < n.bbox_min_z THEN $b[2] ELSE n.bbox_min_z END, "
                       "n.bbox_max_x = CASE WHEN n.bbox_max_x IS NULL OR $b[3] > n.bbox_max_x THEN $b[3] ELSE n.bbox_max_x END, "
                       "n.bbox_max_y = CASE WHEN n.bbox_max_y IS NULL OR $b[4] > n.bbox_max_y THEN $b[4] ELSE n.bbox_max_y END, "
                       "n.bbox_max_z = CASE WHEN n.bbox_max_z IS NULL OR $b[5] > n.bbox_max_z THEN $b[5] ELSE n.bbox_max_z END",
                       id=dataset_id, b=box)

        # ── District / City / LOCATED_IN: only when buildings were ingested (a
        #    street-space tile would otherwise create one District per tile file) and the
        #    file names its municipality (Gemeindeschluessel) or --district names it; a
        #    District named after a file says nothing
        if ctx.building_ids and not (ctx.gemeinde_key or district):
            print("  No Gemeindeschluessel in the file: no District / LOCATED_IN "
                  "(name one with --district).")
        if ctx.building_ids and (ctx.gemeinde_key or district):
            district_name, city_name = _resolve_municipality(
                ctx.gemeinde_key, dataset_name or gml_path.stem)
            if district:
                district_name = district
                city_name = city_name if ctx.gemeinde_key else district
            district_id, city_id = _slug(district_name), _slug(city_name)
            with _SHARED_NODE_LOCK:
                sess.run("MERGE (n:City {id: $id}) SET n.name = $name", id=city_id, name=city_name)
                sess.run("MERGE (n:District {id: $id}) SET n.name = $name",
                         id=district_id, name=district_name)
            _run_retry(sess, "MATCH (a:District {id: $did}), (b:City {id: $cid}) "
                             "MERGE (a)-[:PART_OF]->(b)", did=district_id, cid=city_id)
            _run_retry(sess, "MATCH (a:Dataset {id: $dsid}), (b:District {id: $did}) "
                             "MERGE (a)-[:COVERS]->(b)", dsid=dataset_id, did=district_id)
            for i in range(0, len(ctx.building_ids), 1000):
                _run_retry(sess, "UNWIND $ids AS bid MATCH (b:Building {id: bid}) "
                                 "MATCH (d:District {id: $did}) MERGE (b)-[:LOCATED_IN]->(d)",
                           ids=ctx.building_ids[i:i + 1000], did=district_id)

        proximity_edges = _create_proximity_edges_native(sess) if nearby_edges else 0
        spatial_indexed = 0
        if not skip_spatial_index:
            spatial_indexed = _index_rtree3(sess, ctx.top_ids + backfilled, layer)
            print(f"  Spatial R-tree entries added to layer '{layer}': {spatial_indexed}")

    top = {k[4:]: v for k, v in ctx.stats.items() if k.startswith("top:")}
    sub = {k[4:]: v for k, v in ctx.stats.items() if k.startswith("sub:")}
    surf = {k[8:]: v for k, v in ctx.stats.items() if k.startswith("surface:")}
    other = {k: v for k, v in ctx.stats.items() if ":" not in k}
    print(f"  CityGML 3.0: {sum(top.values())} top-level features in {t_parse:.1f}s "
          f"(deferred refs {len(ctx.deferred)}, unresolved→pending {len(unresolved)}, "
          f"pending resolved now {resolved_pending}, still pending {still_pending})")
    for name, d in (("top-level", top), ("sub-features", sub), ("surfaces", surf), ("other", other)):
        if d:
            print(f"    {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(d.items())))
    if ctx.unsupported:
        print("  NOT ingested (reported, not silently dropped): " +
              ", ".join(f"{k} ×{v}" for k, v in sorted(ctx.unsupported.items())))
    return {
        "source": source, "format": "CityGML 3.0", "crs": crs, "srs_dimension": srs_dim,
        "lod": f"LOD{ctx.max_lod}" if ctx.max_lod >= 0 else "LOD0",
        "features_total": sum(top.values()),
        "features_by_type": dict(sorted(top.items())),
        "sub_features_by_type": dict(sorted(sub.items())),
        "surfaces_by_type": dict(sorted(surf.items())),
        "geometry_and_links": dict(sorted(other.items())),
        "deferred_refs": len(ctx.deferred), "pending_refs": still_pending,
        "unsupported": dict(sorted(ctx.unsupported.items())),
        "rtree_layer": layer, "proximity_edges": proximity_edges,
        "spatial_indexed": spatial_indexed,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):   # a cp1252 console must not crash on non-ASCII output
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    import argparse

    parser = argparse.ArgumentParser(description="Ingest CityGML 2.0 or 3.0 (all modules) into "
                                     "Neo4j; the codec is chosen per file from the root namespace.")
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
    parser.add_argument("--source-type", default=None,
                        choices=["authoritative", "crowdsourced", "predicted", "reconstructed",
                                 "sensed", "derived"],
                        help="Provenance trust class (GRAPH.md provenance rules), written as "
                             "source_type on the Dataset and on every feature / sub-feature / "
                             "surface / prototype / appearance node of the ingested files "
                             "(e.g. --source-type reconstructed for the street-space tiles).")
    parser.add_argument("--method", default=None,
                        help="Method / model id of derived data, stored as `method` beside "
                             "source_type (e.g. --method streetspace-recon).")
    parser.add_argument("--method-version", default=None,
                        help="Version of --method, stored as `method_version`. --method and "
                             "--method-version are required for predicted, reconstructed and "
                             "derived data (GRAPH.md provenance rules).")
    parser.add_argument("--id-prefix", default=None,
                        help="Prefix every gml:id (and every reference to one) of these files "
                             "(letters, digits, '_', '-', '.', e.g. lgv_), "
                             "for a source that reuses another source's ids (e.g. a model of "
                             "the same buildings with the cadastral ids). Recorded on the "
                             "Dataset; export_citygml strips it again. Every file of a Dataset "
                             "uses the same prefix.")
    parser.add_argument("--no-id-check", action="store_true",
                        help="Skip the pre-flight check that refuses a file whose gml:ids "
                             "belong to nodes of another source (they would be merged and "
                             "overwritten). Only for files known to be independent.")
    parser.add_argument("--district", default=None,
                        help="District name for the buildings of these files when the files "
                             "carry no Gemeindeschluessel (or to override it). Without it, a "
                             "CityGML 3.0 file without one gets no District.")
    parser.add_argument("--rtree-layer", default=None,
                        help="Neo4j Spatial R-tree layer for the top-level features of these "
                             "files (default 'features'). Give every non-authoritative source its "
                             "own layer, e.g. --rtree-layer streetspace or citygml3; features of "
                             "another layer never leak into 'features'.")
    parser.add_argument("--resolve-pending", action="store_true",
                        help="Only retry the parked cross-file references (PendingRefs: "
                             "predecessor/successor/relatedTo/xlinks into tiles that were not "
                             "ingested yet) and exit. Runs automatically after every 3.0 file.")
    parser.add_argument("--backfill-bbox", action="store_true",
                        help="Only compute bbox_min/max_x/y/z on sub-features and boundary "
                             "surfaces that miss them (graphs ingested before the 3.0 codec "
                             "stored them) and exit; scoped to --dataset, else to every CityGML "
                             "3.0 Dataset. Idempotent and resumable.")
    parser.add_argument("--dataset", default=None,
                        help="Dataset id for --backfill-bbox (default: all CityGML 3.0 Datasets).")
    parser.add_argument("--uri", default=neo4j_uri(),
                        help="Neo4j bolt URI (default $NEO4J_URI or bolt://localhost:7687).")
    parser.add_argument("--user", default=neo4j_auth()[0],
                        help="Neo4j user (default $NEO4J_USER or neo4j).")
    parser.add_argument("--password", default=neo4j_auth()[1],
                        help="Neo4j password (default $NEO4J_PASSWORD or neo4jneo4j).")
    args = parser.parse_args()
    if args.source_type in ("predicted", "reconstructed", "derived") and not (
            args.method and args.method_version):
        parser.error(f"--source-type {args.source_type} needs --method and --method-version")
    if args.id_prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", args.id_prefix):
        # prefixed ids and the ids pykci derives from them must stay valid gml:ids (NCName)
        parser.error("--id-prefix must start with a letter or '_' and contain only letters, "
                     "digits, '_', '-' and '.' (no ':'), e.g. lgv_")
    _opts = dict(source_type=args.source_type, method=args.method,
                 method_version=args.method_version, rtree_layer=args.rtree_layer,
                 id_prefix=args.id_prefix, check_ids=not args.no_id_check,
                 district=args.district)

    if args.backfill_bbox and not args.paths:
        _drv = GraphDatabase.driver(args.uri, auth=(args.user, args.password),
                                    notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
        with _drv.session(database="neo4j") as _s:
            backfill_subfeature_bbox(_s, args.dataset)
        _drv.close()
        sys.exit(0)

    if args.resolve_pending and not args.paths:
        _drv = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
        with _drv.session(database="neo4j") as _s:
            _res, _left = resolve_pending_refs(_s)
        _drv.close()
        print(f"Pending references resolved: {_res}, still pending: {_left}")
        sys.exit(0)

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
        args.uri,
        auth=(args.user, args.password),
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
            res = ingest(f, driver, num_writers=writers_per,
                         nearby_edges=do_nearby_edges,
                         dataset_id=shared_id, dataset_name=args.dataset_name,
                         target_crs=args.target_crs, source_crs=args.source_crs,
                         id_registry=shared_registry, skip_spatial_index=True,
                         defer_code_hubs=True, **_opts)
            progress("ingest.file", done=len(results) + 1, total=len(candidates),
                     unit="files", file=f.name)
            return res

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
            progress("ingest.spatial_index", message="building spatial R-tree (final pass)")
            if _ensure_spatial_layer(sess, args.rtree_layer or "features"):
                # CityObject footprints are added to the R-tree AFTER OSM fusion,
                # not here — see the note at the single-file call site above and
                # ingest_osm.py's post-fusion CityObject-index step.
                _total_spatial = _index_features_spatial(sess, args.rtree_layer or "features")
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
                                    target_crs=args.target_crs, source_crs=args.source_crs,
                                    **_opts)] = f
            for fut in as_completed(futures):
                gml_f = futures[fut]
                try:
                    results[gml_f.name] = fut.result()
                except Exception as exc:
                    print(f"ERROR ingesting {gml_f}: {exc}")
                    results[gml_f.name] = {"error": str(exc)}
                progress("ingest.file", done=len(results), total=len(candidates),
                         unit="files", file=gml_f.name)

    # Final pass over parked cross-file references (3.0 tiles ingested in parallel or
    # out of order); a no-op when nothing is pending (always for 2.0-only runs).
    with driver.session(database="neo4j") as sess:
        if sess.run("MATCH (n:PendingRefs) RETURN count(n) AS c").single()["c"]:
            _res, _left = resolve_pending_refs(sess)
            print(f"Pending references resolved: {_res}, still pending: {_left}")
            results["_pending_refs"] = {"resolved": _res, "pending": _left}
    driver.close()
    print("Done.")
    print(json.dumps(results, indent=2))
    failed = sorted(k for k, v in results.items() if isinstance(v, dict) and "error" in v)
    progress("ingest.done", message="failed" if failed else "ok", failed=failed)
    # A non-zero exit lets callers (pykci_cli, the web client) detect a failed file
    # instead of parsing the log; manual runs still see the same printed summary.
    if failed:
        sys.exit(1)
