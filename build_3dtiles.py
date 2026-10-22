"""
Neo4j graph → 3D Tiles 1.1 (GLB + tileset.json)

Reads geometry and CRS from the Neo4j graph (populated by ingest_citygml.py).
Supports all CityGML 2.0 modules ingested into the graph:
  Building, Transportation, Vegetation, WaterBody, LandUse, CityFurniture,
  Relief, Bridge, Tunnel, CityObjectGroup.

Pipeline:
  1. Read srsName and bbox from the Dataset node in Neo4j.
  2. Fetch each polygon's exterior ring (+ any interior rings = window/door
     cut-outs) with surface/feature type from the graph.
  3. Dataset CRS → WGS84 lon/lat (pyproj) + geoid offset → ECEF
  4. Project all ECEF to local East-North-Up frame at dataset centroid
  5. Earcut-triangulate each planar polygon, cutting interior rings as holes
     (overlapping/touching openings are resolved with shapely if available)
  6. Pack into a single GLB (one primitive per surface/module type)
  7. Write tileset.json with per-tile transform that places GLB on the globe

Usage (from project root):
  python build_3dtiles.py                          # first dataset in graph
  python build_3dtiles.py --source <filename.gml>  # specific dataset by source name
  python build_3dtiles.py --out <output_dir>        # custom output directory
  python build_3dtiles.py --tiled --workers 8       # whole city: streamable quadtree

Output: output/3dtiles/tileset.json + output/3dtiles/tiles/city.glb
        (--tiled: tileset.json + feature_schema.json + tiles/r*.glb, one GLB per
        quadtree tile, each with per-feature attributes as EXT_structural_metadata)
"""

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import mapbox_earcut as earcut_lib
import numpy as np
from lxml import etree
from neo4j import GraphDatabase
from pyproj import Transformer

from pykci_provenance import AUTHORITATIVE, DERIVED, LAYERS, property_provenance
from pykci_runtime import neo4j_auth, neo4j_uri, progress

# Optional: shapely lets us union overlapping window/door openings before
# cutting them as holes. earcut cannot bridge overlapping holes (it silently
# drops some), so without shapely such openings stay un-cut in the mesh.
try:
    from shapely.geometry import Polygon as _ShPolygon
    from shapely.ops import unary_union as _sh_unary_union
    _HAVE_SHAPELY = True
except Exception:
    _HAVE_SHAPELY = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WGS84_A  = 6378137.0
WGS84_F  = 1.0 / 298.257223563
WGS84_E2 = 2 * WGS84_F - WGS84_F ** 2

# ADV srsName pattern → compound EPSG (horizontal + DHHN92 vertical ≈ DHHN2016).
# Used to derive the geoid undulation (orthometric → ellipsoidal height offset)
# via a pyproj compound-CRS → EPSG:4979 (WGS84 3D) transformation.
# Requires PROJ datum-grid files; falls back to 0.0 m with a warning if absent.
_ADV_COMPOUND = {
    "ETRS89_UTM32": "EPSG:5555",   # ETRS89-UTM32N + DHHN92 height
    "ETRS89_UTM33": "EPSG:5556",   # ETRS89-UTM33N + DHHN92 height
    "DE_DHDN_GK2":  "EPSG:5675",   # Gauss-Krüger zone 2 + DHHN92
    "DE_DHDN_GK3":  "EPSG:5678",   # Gauss-Krüger zone 3 + DHHN92
    "DE_DHDN_GK4":  "EPSG:5679",   # Gauss-Krüger zone 4 + DHHN92
    "DE_DHDN_GK5":  "EPSG:5680",   # Gauss-Krüger zone 5 + DHHN92
}

NS = {
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
}

# Surface/feature type → material index
FEATURE_MATERIAL = {
    # Building boundary surfaces
    "WallSurface":              0,
    "ClosureSurface":           0,
    "RoofSurface":              1,
    "GroundSurface":            2,
    # Transportation
    "Road":                     3,
    "Railway":                  3,
    "Track":                    3,
    "Square":                   3,
    "TransportationComplex":    3,
    # Vegetation
    "SolitaryVegetationObject": 4,
    "PlantCover":               4,
    # WaterBody
    "WaterBody":                5,
    # LandUse
    "LandUse":                  6,
    # CityFurniture / CityObjectGroup
    "CityFurniture":            7,
    "CityObjectGroup":          7,
    # Relief
    "ReliefFeature":            8,
    # Bridge
    "Bridge":                   9,
    # Tunnel
    "Tunnel":                   10,
}

MATERIALS_GLTF = [
    {"name": "wall",           "color": [0.91, 0.86, 0.76, 1.0]},  # warm cream / European facade
    {"name": "roof",           "color": [0.72, 0.48, 0.18, 1.0]},  # amber ochre — matches UI accent
    {"name": "ground",         "color": [0.34, 0.26, 0.16, 1.0]},  # dark warm earth
    {"name": "transportation", "color": [0.50, 0.46, 0.38, 1.0]},  # warm asphalt
    {"name": "vegetation",     "color": [0.38, 0.54, 0.26, 1.0]},  # warm olive green
    {"name": "water",          "color": [0.46, 0.70, 0.86, 1.0]},  # calm Voyager blue
    {"name": "landuse",        "color": [0.76, 0.68, 0.38, 1.0]},  # warm wheat / field
    {"name": "furniture",      "color": [0.58, 0.50, 0.40, 1.0]},  # warm stone
    {"name": "terrain",        "color": [0.70, 0.58, 0.38, 1.0]},  # sandy soil
    {"name": "bridge",         "color": [0.64, 0.58, 0.48, 1.0]},  # warm limestone concrete
    {"name": "tunnel",         "color": [0.34, 0.30, 0.24, 1.0]},  # dark warm concrete
]

_N_MATS = len(MATERIALS_GLTF)

# ---------------------------------------------------------------------------
# Attribute-driven ("thematic") building colouring
# ---------------------------------------------------------------------------
# Colours a building by its ML-predicted roof material, keeping the buildings
# translucent so the aligned knowledge graph reads through them, and lifts the
# LoD3-reconstructed building to an opaque gold accent so the multi-source
# enrichment story is legible at a glance.  Roofs are drawn a touch more opaque
# than walls because the teaser is viewed obliquely from above.

# Categorical, harmonised for the CARTO Voyager (warm cream) basemap.
# 'soft': muted tones tuned for the translucent overlay teaser.
# 'vivid': basic main colours (blue / green / red / sand / graphite) — clearly
#          distinguishable classes for fully opaque renders; kept pastel-creamy
#          so they still sit softly on the basemap.
ROOFMAT_PALETTES = {
    "soft": {
        "metal":      (0.36, 0.56, 0.71),   # steel blue
        "concrete":   (0.60, 0.56, 0.50),   # warm grey
        "tar_paper":  (0.40, 0.42, 0.50),   # slate blue-grey
        "glass":      (0.34, 0.74, 0.74),   # teal
        "roof_tiles": (0.85, 0.50, 0.36),   # terracotta
    },
    "vivid": {
        "metal":      (0.22, 0.42, 0.78),   # true blue
        "concrete":   (0.83, 0.72, 0.52),   # sand
        "tar_paper":  (0.32, 0.34, 0.42),   # graphite
        "glass":      (0.24, 0.63, 0.42),   # green
        "roof_tiles": (0.82, 0.32, 0.24),   # red
    },
}
ROOFMAT_COLORS = ROOFMAT_PALETTES["soft"]   # default palette
ROOFMAT_ORDER = ["metal", "concrete", "tar_paper", "glass", "roof_tiles"]
LOD3_COLOR    = (0.91, 0.70, 0.29)      # gold accent — reconstructed building
DEFAULT_MAT   = (0.72, 0.68, 0.62)      # warm neutral — no prediction

# Per-role alpha + brightness (viewed obliquely: walls semi-transparent so the
# graph shows through, roofs a bit more present, LoD3 nearly opaque to "pop").
_ROLE_STYLE = {   # role: (alpha, lighten toward white)
    "wall":   (0.60, 0.15),
    "roof":   (0.80, 0.00),
    "ground": (0.35, 0.00),
}
_LOD3_ROLE_STYLE = {
    "wall":   (0.86, 0.10),
    "roof":   (0.94, 0.00),
    "ground": (0.60, 0.00),
}


def _mix_white(rgb, t):
    return tuple(c + (1.0 - c) * t for c in rgb)


def _surface_role(surface_type: str) -> str:
    if surface_type == "RoofSurface":
        return "roof"
    if surface_type == "GroundSurface":
        return "ground"
    return "wall"


# Building boundary-surface types; every other FEATURE_MATERIAL key belongs to
# a non-building city object (Road, WaterBody, Bridge, …) which keeps its
# standard per-type colour even in thematic mode.
_BLDG_SURFACE_TYPES = frozenset(
    {"WallSurface", "ClosureSurface", "RoofSurface", "GroundSurface"})


def build_thematic_materials(building_attrs: dict, palette: str = "soft",
                             opacity: float | None = None):
    """Return (materials_list, index_fn) for roof-material building colouring.

    building_attrs : {building_id -> {"roofmat": str|None, "is_lod3": bool}}
    palette        : key into ROOFMAT_PALETTES ('soft' | 'vivid')
    opacity        : if given, overrides every per-role alpha (1.0 = fully
                     opaque; materials then render on the opaque pass and a
                     viewer can still fade them at runtime via a style alpha)
    index_fn(building_id, surface_type) -> material index into materials_list

    Degrades gracefully: buildings without a prediction (or graphs where the
    attribute does not exist at all) use the neutral default coat, and
    non-building city objects fall back to the standard per-type palette.
    """
    colors = ROOFMAT_PALETTES[palette]
    materials: list = []
    key_to_idx: dict = {}

    def _ensure(key: str, rgb, role: str, lod3: bool):
        if key in key_to_idx:
            return key_to_idx[key]
        alpha, lighten = (_LOD3_ROLE_STYLE if lod3 else _ROLE_STYLE)[role]
        if opacity is not None:
            alpha = opacity
        col = _mix_white(rgb, lighten)
        materials.append({"name": key, "color": [col[0], col[1], col[2], alpha]})
        key_to_idx[key] = len(materials) - 1
        return key_to_idx[key]

    # Pre-register a stable palette so the legend order is deterministic.
    for rm in ROOFMAT_ORDER:
        for role in ("wall", "roof", "ground"):
            _ensure(f"{rm}_{role}", colors[rm], role, False)
    for role in ("wall", "roof", "ground"):
        _ensure(f"lod3_{role}", LOD3_COLOR, role, True)
        _ensure(f"default_{role}", DEFAULT_MAT, role, False)

    # Standard per-type palette appended for non-building city objects
    # (Road, WaterBody, Bridge, …) so mixed-module datasets stay readable.
    type_offset = len(materials)
    materials.extend(MATERIALS_GLTF)

    def index_fn(building_id: str, surface_type: str) -> int:
        if surface_type in FEATURE_MATERIAL and surface_type not in _BLDG_SURFACE_TYPES:
            return type_offset + FEATURE_MATERIAL[surface_type]
        role = _surface_role(surface_type)
        info = building_attrs.get(building_id or "", None)
        if info and info.get("is_lod3"):
            return key_to_idx[f"lod3_{role}"]
        rm = (info or {}).get("roofmat")
        if rm in colors:
            return key_to_idx[f"{rm}_{role}"]
        return key_to_idx[f"default_{role}"]

    return materials, index_fn


def fetch_building_attrs(sess, source: str) -> dict:
    """{building_id -> {roofmat, is_lod3}} for thematic colouring."""
    attrs: dict = {}
    for row in sess.run("""
        MATCH (b:Building)-[:PART_OF]->(d:Dataset {source: $src})
        RETURN b.id AS id,
               b.predictedroofmaterial AS roofmat,
               coalesce(b.lod3_enriched, false) AS is_lod3
    """, src=source):
        attrs[row["id"]] = {"roofmat": row["roofmat"], "is_lod3": bool(row["is_lod3"])}
    return attrs


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def lonlat_to_ecef(lon_deg: float, lat_deg: float, h: float):
    """WGS84 geographic (degrees) + ellipsoidal height (m) → ECEF (m)."""
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    N = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
    X = (N + h) * math.cos(lat) * math.cos(lon)
    Y = (N + h) * math.cos(lat) * math.sin(lon)
    Z = (N * (1.0 - WGS84_E2) + h) * math.sin(lat)
    return X, Y, Z


def ecef_to_lonlat(X: float, Y: float, Z: float):
    """ECEF (m) → WGS84 lon/lat (degrees)."""
    lon = math.degrees(math.atan2(Y, X))
    p   = math.sqrt(X * X + Y * Y)
    lat = math.degrees(math.atan2(Z, p * (1.0 - WGS84_E2)))  # initial guess
    for _ in range(5):
        sin_lat = math.sin(math.radians(lat))
        N = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat ** 2)
        lat = math.degrees(math.atan2(Z + WGS84_E2 * N * sin_lat, p))
    return lon, lat


def _horiz_epsg_from_srs(srs_name: str) -> str:
    """Map a CityGML srsName to a horizontal EPSG CRS identifier for pyproj.

    Handles the most common patterns found in CityGML files:
      urn:ogc:def:crs:EPSG::25832   → EPSG:25832
      EPSG:25832                    → EPSG:25832
      urn:adv:crs:ETRS89_UTM32*…   → EPSG:25832
      urn:adv:crs:ETRS89_UTM33*…   → EPSG:25833
      urn:adv:crs:DE_DHDN_GK3*…    → EPSG:31467
    Falls back to the raw srsName string (pyproj may still accept it).
    """
    # OGC/EPSG URN or plain prefix: EPSG:NNNNN
    m = re.search(r"EPSG[:\s]+(\d+)", srs_name)
    if m:
        return f"EPSG:{m.group(1)}"
    # ADV UTM: ETRS89_UTM32 → EPSG:25832, ETRS89_UTM33 → EPSG:25833
    m = re.search(r"ETRS89_UTM(\d+)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{25800 + int(m.group(1))}"
    # ADV Gauss-Krüger: GK2 → EPSG:31466, GK3 → 31467, GK4 → 31468, GK5 → 31469
    m = re.search(r"GK(\d)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{31464 + int(m.group(1))}"
    return srs_name


def _geoid_m(srs_name: str, sample_x: float, sample_y: float, sample_z: float,
             override: float | None = None) -> tuple[float, str]:
    """Geoid undulation (m) at a sample point in the dataset CRS, and where it comes from.

    `override` (--geoid-offset) wins. Otherwise a compound-CRS → EPSG:4979 (WGS84 3D
    ellipsoidal) transformation via pyproj: the difference between the transformed
    ellipsoidal height and the input orthometric height is the undulation.

    Returns (0.0, 'none') and prints a warning when no compound CRS mapping is found for
    the srsName, the transformation fails, or PROJ has no geoid grid for it: PROJ then
    falls back to a 'ballpark' transformation that leaves the height unchanged, which
    would silently give 0 m instead of the ~40 m of Germany.
    """
    if override is not None:
        return float(override), "--geoid-offset"
    compound = None
    for pat, epsg in _ADV_COMPOUND.items():
        if pat.lower() in srs_name.lower():
            compound = epsg
            break
    if compound is None:
        print(f"  Note: no compound CRS mapping for '{srs_name}'; geoid offset = 0.0 m "
              "(pass --geoid-offset to set it)")
        return 0.0, "none"
    try:
        t3d = Transformer.from_crs(compound, "EPSG:4979", always_xy=True)
        _, _, h_ell = t3d.transform(sample_x, sample_y, sample_z)
    except Exception as exc:
        print(f"  WARNING: geoid transform unavailable ({exc}); geoid offset = 0.0 m "
              "(pass --geoid-offset to set it)")
        return 0.0, "none"
    if "ballpark" in t3d.description.lower() or not np.isfinite(h_ell) or abs(h_ell - sample_z) < 1e-6:
        print(f"  WARNING: PROJ has no geoid grid for {compound} (ballpark transformation); geoid offset = "
              "0.0 m. Install the grid (projsync) or pass --geoid-offset")
        return 0.0, "none"
    return h_ell - sample_z, f"PROJ {compound} -> EPSG:4979 at the dataset centre"


def enu_basis(lon_deg: float, lat_deg: float):
    """East / North / Up unit vectors at a WGS84 geographic point."""
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    east  = np.array([-math.sin(lon),
                       math.cos(lon),
                       0.0])
    north = np.array([-math.sin(lat) * math.cos(lon),
                      -math.sin(lat) * math.sin(lon),
                       math.cos(lat)])
    up    = np.array([ math.cos(lat) * math.cos(lon),
                       math.cos(lat) * math.sin(lon),
                       math.sin(lat)])
    return east, north, up


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def newell_normal(verts: np.ndarray) -> np.ndarray:
    """Polygon normal via Newell's method; returns unit vector."""
    n = np.zeros(3)
    m = len(verts)
    for i in range(m):
        j = (i + 1) % m
        v1, v2 = verts[i], verts[j]
        n[0] += (v1[1] - v2[1]) * (v1[2] + v2[2])
        n[1] += (v1[2] - v2[2]) * (v1[0] + v2[0])
        n[2] += (v1[0] - v2[0]) * (v1[1] + v2[1])
    length = np.linalg.norm(n)
    return n / length if length > 1e-10 else np.array([0.0, 0.0, 1.0])


def _plane_basis(verts: np.ndarray, normal: np.ndarray):
    """Return (origin, u, v): an orthonormal in-plane 2-D basis for a polygon.

    Projecting in-plane points with (u, v) is an isometry (distances/areas
    preserved), so it is both more robust than dropping a coordinate axis and
    invertible — 2-D points map back to 3-D via origin + x*u + y*v.
    """
    origin = verts[0]
    u = None
    for i in range(1, len(verts)):
        e = verts[i] - origin
        n = np.linalg.norm(e)
        if n > 1e-9:
            u = e / n
            break
    if u is None:
        u = np.array([1.0, 0.0, 0.0])
    v = np.cross(normal, u)
    nv = np.linalg.norm(v)
    v = v / nv if nv > 1e-12 else np.array([0.0, 1.0, 0.0])
    return origin, u, v


def _earcut_2d(rings_2d):
    """Earcut a list of 2-D rings (exterior first, then holes). Returns flat
    int index array, or None on failure."""
    verts_2d  = np.vstack(rings_2d).astype(np.float64)
    ring_size = np.cumsum([len(r) for r in rings_2d]).astype(np.uint32)
    try:
        idx = earcut_lib.triangulate_float64(verts_2d, ring_size)
    except Exception:
        return None
    if len(idx) == 0 or len(idx) % 3 != 0:
        return None
    return idx


def _triangulate_with_holes_shapely(ext2d, holes2d, to3d):
    """Cut holes via shapely boolean difference, then triangulate.

    Computes exterior MINUS union(holes), which resolves every case earcut
    cannot handle on its own — overlapping holes, holes touching each other,
    and holes touching the exterior boundary (these become notches). Each
    resulting polygon (with its own interior rings) is earcut-triangulated and
    the pieces are concatenated. 2-D results are lifted back to 3-D via to3d.

    Returns (all_verts_3d, flat_indices) or None to signal fallback.
    """
    try:
        ext_poly = _ShPolygon(ext2d)
        if not ext_poly.is_valid:
            ext_poly = ext_poly.buffer(0)
        hole_polys = []
        for h in holes2d:
            p = _ShPolygon(h)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 1e-9:
                hole_polys.append(p)
        remaining = ext_poly.difference(_sh_unary_union(hole_polys)) if hole_polys else ext_poly
    except Exception:
        return None

    parts = list(remaining.geoms) if remaining.geom_type.startswith("Multi") else [remaining]
    all_v, all_idx, voff = [], [], 0
    for poly in parts:
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        rings = [np.asarray(poly.exterior.coords)[:-1]]
        for ring in poly.interiors:
            r = np.asarray(ring.coords)[:-1]
            if len(r) >= 3:
                rings.append(r)
        idx = _earcut_2d(rings)
        if idx is None:
            continue
        v2 = np.vstack(rings).astype(np.float64)
        all_v.append(to3d(v2))
        all_idx.append(idx.astype(np.int64) + voff)
        voff += len(v2)
    if not all_v:
        return None
    return np.vstack(all_v), np.concatenate(all_idx).astype(np.int32)


def triangulate_polygon_enu(verts_enu: np.ndarray, holes_enu=None):
    """
    Triangulate a planar polygon (with optional holes) in local ENU space.

    verts_enu : (N, 3) array exterior ring — may end with closing duplicate.
    holes_enu : optional list of (M, 3) interior-ring arrays (window/door
                cut-outs); each may end with a closing duplicate vertex.
                Overlapping holes are unioned (via shapely) so earcut can
                cut them all — it cannot bridge overlapping holes itself.
    Returns   : (all_verts, normal_enu, flat_indices) or None.  all_verts is
                the exterior ring followed by every hole ring; flat_indices
                reference into it.
    """
    # Drop duplicate closing vertex (CityGML closed rings)
    if len(verts_enu) > 1 and np.allclose(verts_enu[0], verts_enu[-1], atol=1e-4):
        verts_enu = verts_enu[:-1]
    if len(verts_enu) < 3:
        return None

    normal = newell_normal(verts_enu)
    origin, u, v = _plane_basis(verts_enu, normal)

    def to2d(P):
        d = P - origin
        return np.column_stack([d @ u, d @ v])

    def to3d(xy):
        return origin + np.outer(xy[:, 0], u) + np.outer(xy[:, 1], v)

    # Collect valid hole rings (closing duplicate dropped).
    holes2d = []
    if holes_enu:
        for h in holes_enu:
            if len(h) > 1 and np.allclose(h[0], h[-1], atol=1e-4):
                h = h[:-1]
            if len(h) >= 3:
                holes2d.append(to2d(h))

    # Preferred path: shapely boolean difference robustly resolves overlapping /
    # touching / edge-touching openings that earcut alone cannot bridge.
    if holes2d and _HAVE_SHAPELY:
        res = _triangulate_with_holes_shapely(to2d(verts_enu), holes2d, to3d)
        if res is not None:
            all_verts, indices = res
            return all_verts, normal, indices

    # Fallback (no shapely, or difference failed): feed rings straight to earcut.
    rings_3d = [verts_enu]
    rings_2d = [to2d(verts_enu)]
    for ring2d in holes2d:
        rings_2d.append(ring2d)
        rings_3d.append(to3d(np.asarray(ring2d, dtype=np.float64)))

    all_verts = np.vstack(rings_3d)
    indices = _earcut_2d(rings_2d)
    if indices is None:
        return None

    return all_verts, normal, indices.astype(np.int32)


# ---------------------------------------------------------------------------
# Neo4j surface fetcher
# ---------------------------------------------------------------------------

_NEO4J_URI  = neo4j_uri()
_NEO4J_AUTH = neo4j_auth()


def _connect():
    driver = GraphDatabase.driver(
        _NEO4J_URI, auth=_NEO4J_AUTH,
        # UNRECOGNIZED: the tiled queries probe optional properties (is_implicit,
        # lod3_enriched, …) that a given city may not have
        notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"},
    )
    driver.verify_connectivity()
    return driver


def fetch_dataset(sess, source: str) -> dict:
    """Return Dataset node properties for the given source filename."""
    row = sess.run(
        "MATCH (d:Dataset {source: $src}) RETURN d LIMIT 1", src=source
    ).single()
    if row is None:
        return {}
    return dict(row["d"])


def list_sources(sess) -> list[str]:
    """Return all Dataset source names in the graph."""
    return [r["src"] for r in sess.run("MATCH (d:Dataset) RETURN d.source AS src")]


def fetch_surfaces(sess, source: str):
    """Yield {type, pos_str} for every polygon exterior ring in the dataset.

    Building boundary surfaces use the BoundarySurface.surface_type
    (WallSurface, RoofSurface, GroundSurface, ClosureSurface) so each gets
    its own material colour.  All other city objects use CityObject.feature_type
    (Road, WaterBody, Bridge, …) as the material type.
    """
    # 1. Building boundary surfaces (+ interior rings = window/door holes)
    for row in sess.run("""
        MATCH (b:Building)-[:PART_OF]->(d:Dataset {source: $src})
        MATCH (b)-[:HAS_BOUNDARY]->(bs:BoundarySurface)-[:HAS_POLYGON]->(p:GeometryPolygon)
        MATCH (p)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
        OPTIONAL MATCH (p)-[:HAS_INTERIOR_RING]->(ri:GeometryRing)
        WITH b, bs, p, r, ri ORDER BY ri.ring_index
        WITH b.id AS building_id, bs.surface_type AS type, p, r.pos_list AS pos_str,
             [x IN collect(ri.pos_list) WHERE x IS NOT NULL AND x <> ''] AS holes
        RETURN building_id, type, pos_str, holes
    """, src=source):
        yield {"building_id": row["building_id"], "type": row["type"] or "WallSurface",
               "pos_str": row["pos_str"], "holes": row["holes"]}

    # 2. Building solids (LOD1 or ungrouped)
    for row in sess.run("""
        MATCH (b:Building)-[:PART_OF]->(d:Dataset {source: $src})
        MATCH (b)-[:HAS_LOD_SOLID]->(s:GeometrySolid)-[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
        MATCH (p)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
        RETURN b.id AS building_id, r.pos_list AS pos_str
    """, src=source):
        yield {"building_id": row["building_id"], "type": "WallSurface", "pos_str": row["pos_str"]}

    # 3. CityObject MultiSurface geometry (includes implicit representations)
    for row in sess.run("""
        MATCH (n:CityObject)-[:PART_OF]->(d:Dataset {source: $src})
        MATCH (n)-[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)-[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
        MATCH (p)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
        RETURN n.id AS building_id, n.feature_type AS type, r.pos_list AS pos_str,
               coalesce(ms.is_implicit, false) AS is_implicit,
               coalesce(ms.transformation_matrix, '') AS transformation_matrix,
               coalesce(ms.reference_point, '') AS reference_point
    """, src=source):
        yield {
            "building_id":           row["building_id"],
            "type":                  row["type"] or "WallSurface",
            "pos_str":               row["pos_str"],
            "is_implicit":           row["is_implicit"],
            "transformation_matrix": row["transformation_matrix"],
            "reference_point":       row["reference_point"],
        }

    # 4. CityObject Solid geometry
    for row in sess.run("""
        MATCH (n:CityObject)-[:PART_OF]->(d:Dataset {source: $src})
        MATCH (n)-[:HAS_LOD_SOLID]->(s:GeometrySolid)-[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
        MATCH (p)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
        RETURN n.id AS building_id, n.feature_type AS type, r.pos_list AS pos_str
    """, src=source):
        yield {"building_id": row["building_id"], "type": row["type"] or "WallSurface", "pos_str": row["pos_str"]}

    # 5. CityObject boundary surfaces (WaterBody, Bridge, Tunnel)
    #    Use the parent feature_type for material colour, not the surface sub-type
    for row in sess.run("""
        MATCH (n:CityObject)-[:PART_OF]->(d:Dataset {source: $src})
        MATCH (n)-[:HAS_BOUNDARY]->(bs:BoundarySurface)-[:HAS_POLYGON]->(p:GeometryPolygon)
        MATCH (p)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
        OPTIONAL MATCH (p)-[:HAS_INTERIOR_RING]->(ri:GeometryRing)
        WITH n, p, r, ri ORDER BY ri.ring_index
        WITH n.id AS building_id, n.feature_type AS type, p, r.pos_list AS pos_str,
             [x IN collect(ri.pos_list) WHERE x IS NOT NULL AND x <> ''] AS holes
        RETURN building_id, type, pos_str, holes
    """, src=source):
        yield {"building_id": row["building_id"], "type": row["type"] or "WallSurface",
               "pos_str": row["pos_str"], "holes": row["holes"]}


def pos_str_to_pts(pos_str: str) -> np.ndarray:
    """Parse a gml:posList string into an (N, 3) float array."""
    vals = list(map(float, pos_str.split()))
    n = len(vals) // 3
    return np.array(vals[: n * 3]).reshape(n, 3)


def _apply_implicit_transform(pts: np.ndarray, matrix_str: str, ref_str: str) -> np.ndarray:
    """Apply CityGML ImplicitGeometry transform: P_world = M * [P_local; 1] + P_ref.

    matrix_str : 16 space-separated floats in row-major order (4×4 matrix)
    ref_str    : 3 space-separated floats (world-space reference point in dataset CRS)
    """
    vals = list(map(float, matrix_str.split()))
    if len(vals) != 16:
        return pts
    M        = np.array(vals, dtype=np.float64).reshape(4, 4)
    ref_vals = list(map(float, ref_str.split()))
    ref      = np.array(ref_vals[:3], dtype=np.float64) if len(ref_vals) >= 3 else np.zeros(3)
    n        = len(pts)
    pts_h    = np.column_stack([pts, np.ones(n)])
    return (pts_h @ M.T)[:, :3] + ref


# ---------------------------------------------------------------------------
# GLB writer
# ---------------------------------------------------------------------------

def pack_glb(mat_positions, mat_normals, mat_indices,
             mat_fids=None, n_features=0, materials=None) -> bytes:
    """
    Pack N sets of (positions, normals, indices) into a binary GLB 2.0 file.
    mat_positions[i] : list of [x, y, z] float  (glTF Y-up local frame)
    mat_normals[i]   : list of [nx, ny, nz] float
    mat_indices[i]   : list of int (triangle vertex indices, absolute)
    mat_fids[i]      : list of int (per-vertex building feature index, optional)
    n_features       : total number of distinct features (buildings)
    """
    buf       = bytearray()
    bv_list   = []
    acc_list  = []
    primitives = []

    def _align4(ba: bytearray, pad: int = 0):
        while len(ba) % 4:
            ba.append(pad)

    def _add_bv(data: bytes, target: int) -> int:
        _align4(buf)
        offset = len(buf)
        buf.extend(data)
        idx = len(bv_list)
        bv_list.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(data),
            "target": target,
        })
        return idx

    def _add_vec3_acc(arr_f32: np.ndarray, bv: int, need_minmax: bool) -> int:
        acc = {
            "bufferView": bv,
            "byteOffset": 0,
            "componentType": 5126,   # FLOAT
            "count": len(arr_f32),
            "type": "VEC3",
        }
        if need_minmax:
            acc["min"] = arr_f32.min(axis=0).tolist()
            acc["max"] = arr_f32.max(axis=0).tolist()
        idx = len(acc_list)
        acc_list.append(acc)
        return idx

    def _add_scalar_acc(arr_u32: np.ndarray, bv: int) -> int:
        idx = len(acc_list)
        acc_list.append({
            "bufferView": bv,
            "byteOffset": 0,
            "componentType": 5125,   # UNSIGNED_INT
            "count": int(len(arr_u32)),
            "type": "SCALAR",
        })
        return idx

    for mi in range(len(mat_positions)):
        pos_list = mat_positions[mi]
        nor_list = mat_normals[mi]
        idx_list = mat_indices[mi]
        if not pos_list or not idx_list:
            continue

        pos_arr = np.array(pos_list, dtype=np.float32)
        nor_arr = np.array(nor_list, dtype=np.float32)
        idx_arr = np.array(idx_list, dtype=np.uint32)

        bv_pos = _add_bv(pos_arr.tobytes(), 34962)   # ARRAY_BUFFER
        bv_nor = _add_bv(nor_arr.tobytes(), 34962)
        bv_idx = _add_bv(idx_arr.tobytes(), 34963)   # ELEMENT_ARRAY_BUFFER

        acc_pos = _add_vec3_acc(pos_arr, bv_pos, need_minmax=True)
        acc_nor = _add_vec3_acc(nor_arr, bv_nor, need_minmax=False)
        acc_idx = _add_scalar_acc(idx_arr, bv_idx)

        prim: dict = {
            "attributes": {"POSITION": acc_pos, "NORMAL": acc_nor},
            "indices": acc_idx,
            "material": mi,
            "mode": 4,   # TRIANGLES
        }

        # Per-vertex building feature IDs for EXT_mesh_features picking
        fid_list = (mat_fids[mi] if mat_fids and mi < len(mat_fids) else None)
        if fid_list and n_features > 0:
            fid_arr = np.array(fid_list, dtype=np.uint32)
            bv_fid  = _add_bv(fid_arr.tobytes(), 34962)
            acc_fid = len(acc_list)
            acc_list.append({
                "bufferView":    bv_fid,
                "byteOffset":    0,
                "componentType": 5125,   # UNSIGNED_INT
                "count":         int(len(fid_arr)),
                "type":          "SCALAR",
                "normalized":    False,
            })
            prim["attributes"]["_FEATURE_ID_0"] = acc_fid
            prim["extensions"] = {
                "EXT_mesh_features": {
                    "featureIds": [{
                        "featureCount": n_features,
                        "attribute":    0,
                    }]
                }
            }

        primitives.append(prim)

    gltf_mats = []
    for m in (materials if materials is not None else MATERIALS_GLTF):
        col = m["color"]
        alpha = col[3] if len(col) > 3 else 1.0
        gmat = {
            "name": m["name"],
            "pbrMetallicRoughness": {
                "baseColorFactor": [col[0], col[1], col[2], alpha],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.85,
            },
            "doubleSided": True,
        }
        if alpha < 1.0:
            gmat["alphaMode"] = "BLEND"
        gltf_mats.append(gmat)

    gltf: dict = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "city"}],
        "meshes": [{"name": "city", "primitives": primitives}],
        "materials": gltf_mats,
        "accessors": acc_list,
        "bufferViews": bv_list,
        "buffers": [{"byteLength": len(buf)}],
    }
    if mat_fids and n_features > 0:
        gltf["extensionsUsed"] = ["EXT_mesh_features"]

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    while len(json_bytes) % 4:
        json_bytes += b" "

    bin_bytes = bytes(buf)
    while len(bin_bytes) % 4:
        bin_bytes += b"\x00"

    has_bin = bool(bin_bytes)
    total   = 12 + 8 + len(json_bytes) + (8 + len(bin_bytes) if has_bin else 0)

    glb = bytearray()
    glb.extend(struct.pack("<III", 0x46546C67, 2, total))        # header
    glb.extend(struct.pack("<II",  len(json_bytes), 0x4E4F534A)) # JSON chunk
    glb.extend(json_bytes)
    if has_bin:
        glb.extend(struct.pack("<II", len(bin_bytes), 0x004E4942)) # BIN chunk
        glb.extend(bin_bytes)
    return bytes(glb)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_3dtiles(source: str, out_dir: Path, color_by: str = "type",
                  palette: str = "soft", opacity: float | None = None,
                  geoid_offset: float | None = None):
    # ---- 1. Read CRS and bbox from the Dataset node in Neo4j ---------------
    driver = _connect()
    print("Connected to Neo4j.")
    with driver.session(database="neo4j") as sess:
        ds = fetch_dataset(sess, source)
        if not ds:
            print(f"ERROR: no Dataset with source='{source}' found in graph.", file=sys.stderr)
            driver.close()
            sys.exit(1)

        # ---- Choose material palette + per-surface material selector -------
        if color_by == "roofmat":
            b_attrs = fetch_building_attrs(sess, source)
            materials, _mat_index = build_thematic_materials(b_attrs, palette, opacity)
            n_pred  = sum(1 for v in b_attrs.values() if v.get("roofmat") in ROOFMAT_COLORS)
            n_lod3  = sum(1 for v in b_attrs.values() if v.get("is_lod3"))
            unknown = sorted({v["roofmat"] for v in b_attrs.values()
                              if v.get("roofmat") and v["roofmat"] not in ROOFMAT_COLORS})
            print(f"  Colouring by roof material: {n_pred}/{len(b_attrs)} buildings "
                  f"predicted, {n_lod3} LoD3 highlighted "
                  f"(palette={palette}, opacity={'baked per-role' if opacity is None else opacity})")
            if not n_pred:
                print("  Note: no 'predictedroofmaterial' values in this dataset — "
                      "all buildings use the neutral default coat.")
            if unknown:
                print(f"  Note: unmapped roof-material values (default coat): {unknown}")
        else:
            materials  = MATERIALS_GLTF
            _mat_index = lambda bid, stype: FEATURE_MATERIAL.get(stype, 0)
        n_mats = len(materials)

        srs_name   = ds.get("crs", "")
        lc_str     = ds.get("bbox_lower_corner", "")
        uc_str     = ds.get("bbox_upper_corner", "")
        horiz_epsg = _horiz_epsg_from_srs(srs_name)
        print(f"  Dataset     : {source}")
        print(f"  srsName     : {srs_name}")
        print(f"  Horiz. CRS  : {horiz_epsg}")

        try:
            t_to_wgs84 = Transformer.from_crs(horiz_epsg, "EPSG:4326", always_xy=True)
        except Exception as exc:
            print(f"ERROR: cannot build CRS transformer for '{horiz_epsg}': {exc}", file=sys.stderr)
            driver.close()
            sys.exit(1)

        if lc_str and uc_str:
            lc_v = list(map(float, lc_str.split()))
            uc_v = list(map(float, uc_str.split()))
            mid_x = (lc_v[0] + uc_v[0]) / 2
            mid_y = (lc_v[1] + uc_v[1]) / 2
            mid_z = (lc_v[2] + uc_v[2]) / 2 if len(lc_v) > 2 else 0.0
        else:
            mid_x = mid_y = mid_z = 0.0
        geoid_m, geoid_source = _geoid_m(srs_name, mid_x, mid_y, mid_z, geoid_offset)
        print(f"  Geoid offset: {geoid_m:+.2f} m ({geoid_source})")

        # ---- 2. Fetch surfaces from graph and project to ECEF ---------------
        print("Fetching geometry from graph and projecting to ECEF …")
        surface_list = []

        def _project(pts: np.ndarray):
            """CRS-project an (N,3) array → (ecef, lons, lats, h_ellip)."""
            lo, la = t_to_wgs84.transform(pts[:, 0], pts[:, 1])
            h      = pts[:, 2] + geoid_m
            ec = np.array([
                lonlat_to_ecef(float(lo[i]), float(la[i]), float(h[i]))
                for i in range(len(pts))
            ])
            return ec, lo, la, h

        for surf in fetch_surfaces(sess, source):
            verts = pos_str_to_pts(surf["pos_str"])
            if len(verts) < 3:
                continue
            is_impl = surf.get("is_implicit") and surf.get("transformation_matrix")
            if is_impl:
                verts = _apply_implicit_transform(
                    verts,
                    surf["transformation_matrix"],
                    surf.get("reference_point", ""),
                )
                if len(verts) < 3:
                    continue
            ecef, lons, lats, hs_ellip = _project(verts)

            # Interior rings = window/door cut-outs (holes in the wall plane)
            holes_ecef = []
            for hole_str in surf.get("holes") or []:
                hv = pos_str_to_pts(hole_str)
                if is_impl:
                    hv = _apply_implicit_transform(
                        hv, surf["transformation_matrix"],
                        surf.get("reference_point", ""),
                    )
                if len(hv) < 3:
                    continue
                holes_ecef.append(_project(hv)[0])

            surface_list.append({
                "type":        surf["type"],
                "building_id": surf.get("building_id") or "",
                "ecef":        ecef,
                "holes_ecef":  holes_ecef,
                "lons":        lons,
                "lats":        lats,
                "h":           hs_ellip,
            })

    driver.close()

    total_poly = len(surface_list)
    type_poly: dict = {}
    for _s in surface_list:
        _t = _s["type"]
        type_poly[_t] = type_poly.get(_t, 0) + 1
    print(f"  {total_poly} surface polygons")
    for _t, _n in sorted(type_poly.items()):
        print(f"    {_t}: {_n}")

    if not surface_list:
        print("WARNING: no surface polygons found in graph; nothing to tile.")
        return None, None

    # ---- 3. Tile centroid -----------------------------------------------
    all_ecef    = np.vstack([s["ecef"] for s in surface_list])
    center_ecef = all_ecef.mean(axis=0)
    center_lon, center_lat = ecef_to_lonlat(*center_ecef)
    east_v, north_v, up_v  = enu_basis(center_lon, center_lat)
    print(f"  Tile centre: lon={center_lon:.5f}° lat={center_lat:.5f}°")

    # ---- 4. Triangulate in local ENU, accumulate per-material geometry --
    print("Triangulating polygons …")
    mat_pos  = [[] for _ in range(n_mats)]
    mat_nor  = [[] for _ in range(n_mats)]
    mat_idx  = [[] for _ in range(n_mats)]
    mat_fids = [[] for _ in range(n_mats)]
    building_registry: dict = {}   # building_id str → feature index int
    skipped = 0

    for s in surface_list:
        bid = s.get("building_id") or ""
        mi  = _mat_index(bid, s["type"])
        if bid not in building_registry:
            building_registry[bid] = len(building_registry)
        bidx = building_registry[bid]

        delta = s["ecef"] - center_ecef
        enu   = np.column_stack([
            delta @ east_v,
            delta @ north_v,
            delta @ up_v,
        ])
        holes_enu = []
        for he in s.get("holes_ecef") or []:
            dh = he - center_ecef
            holes_enu.append(np.column_stack([
                dh @ east_v, dh @ north_v, dh @ up_v,
            ]))
        result = triangulate_polygon_enu(enu, holes_enu)
        if result is None:
            skipped += 1
            continue
        verts_enu, normal_enu, tri_idx = result
        n_verts = len(verts_enu)
        voffset = len(mat_pos[mi])
        for v in verts_enu:
            mat_pos[mi].append([float(v[0]), float(v[2]), float(-v[1])])
        n_gltf = [float(normal_enu[0]), float(normal_enu[2]), float(-normal_enu[1])]
        mat_nor[mi].extend([n_gltf] * n_verts)
        mat_fids[mi].extend([bidx] * n_verts)
        for idx in tri_idx:
            mat_idx[mi].append(int(idx) + voffset)

    total_verts = sum(len(mat_pos[i]) for i in range(n_mats))
    total_tris  = sum(len(mat_idx[i]) // 3 for i in range(n_mats))
    print(f"  Triangulated: {total_tris:,} triangles, {total_verts:,} vertices"
          f"  (skipped degenerate polygons: {skipped})")
    for i in range(n_mats):
        if mat_pos[i]:
            print(f"    {materials[i]['name']}: "
                  f"{len(mat_idx[i]) // 3:,} tri / {len(mat_pos[i]):,} vert")

    # ---- 5. Pack GLB -------------------------------------------------------
    n_features = len(building_registry)
    print(f"Packing GLB … ({n_features} building features)")
    glb_bytes = pack_glb(mat_pos, mat_nor, mat_idx, mat_fids, n_features,
                         materials=materials)

    # ---- 6. Bounding region (radians + ellipsoidal metres) -----------------
    all_lons = np.concatenate([s["lons"] for s in surface_list])
    all_lats = np.concatenate([s["lats"] for s in surface_list])
    all_h    = np.concatenate([s["h"]    for s in surface_list])
    bbox_region = [
        math.radians(float(all_lons.min())),
        math.radians(float(all_lats.min())),
        math.radians(float(all_lons.max())),
        math.radians(float(all_lats.max())),
        float(all_h.min()),
        float(all_h.max()),
    ]

    # ---- 7. Tile transform: local Z-up ENU → ECEF (column-major 4×4) ------
    e, n, u    = east_v, north_v, up_v
    cx, cy, cz = center_ecef
    transform = [
        float(e[0]), float(e[1]), float(e[2]), 0.0,
        float(n[0]), float(n[1]), float(n[2]), 0.0,
        float(u[0]), float(u[1]), float(u[2]), 0.0,
        float(cx),   float(cy),   float(cz),   1.0,
    ]

    # ---- 8. Write output ---------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir = out_dir / "tiles"
    tiles_dir.mkdir(exist_ok=True)

    glb_path = tiles_dir / "city.glb"
    glb_path.write_bytes(glb_bytes)

    tileset = {
        "asset": {"version": "1.1", "tilesetVersion": "1.0"},
        "extras": {"pykci": {"geoid_offset_m": round(geoid_m, 3), "geoid_source": geoid_source}},
        "geometricError": 500,
        "root": {
            "transform":      transform,
            "boundingVolume": {"region": bbox_region},
            "geometricError": 0,
            "refine":         "ADD",
            "content":        {"uri": "tiles/city.glb"},
        },
    }
    ts_path = out_dir / "tileset.json"
    ts_path.write_text(json.dumps(tileset, indent=2), encoding="utf-8")

    print(f"  tileset.json : {ts_path}")
    print(f"  city.glb     : {glb_path}  ({len(glb_bytes):,} bytes)")
    return center_lon, center_lat


# ---------------------------------------------------------------------------
# Tiled output: quadtree of small GLBs + per-feature semantic metadata
# ---------------------------------------------------------------------------
# The single-GLB path above suits a district (HafenCity) or a teaser render, but
# for a whole city it produces one file a browser cannot stream (Hamburg:
# 1.5 GB). --tiled writes a 3D Tiles 1.1 quadtree instead:
#
#   * leaf tiles hold the full geometry of at most --max-features top-level
#     features (a Building together with all its BuildingParts, or a CityObject);
#   * coarser tiles hold LoD1 blocks (footprint extruded to the building's
#     height) of the --overview-features most prominent buildings below them, so
#     the whole city stays visible from afar (REPLACE refinement);
#   * every tile carries EXT_mesh_features feature ids and an
#     EXT_structural_metadata property table with each feature's semantic
#     attributes, so a viewer can show or style a clicked building without a
#     database round-trip. Property names keep their namespace (osm_*,
#     predicted_*, …) and each schema property records its provenance layer
#     (pykci_provenance), mirrored in feature_schema.json next to tileset.json.
#
# All tiles share ONE local ENU frame at the dataset centre (a single root
# transform): per-tile transforms would be inherited by child tiles. Positions
# stay within a few tens of km of the origin, i.e. millimetre float32 precision.

_F64_NODATA = -1.7976931348623157e308   # "absent" marker for FLOAT64 properties
_META_EXCLUDE = frozenset({
    "bbox", "bbox_wkt", "bbox_lower_corner", "bbox_upper_corner", "location",
    "has_bbox_envelope", "gtype", "composite_surface_id", "multi_surface_id",
})
_LOD0_ROLES = ("lod0FootPrint", "lod0RoofEdge")

# Openings of a reconstructed LoD3 facade (bldg:Window / bldg:Door) get their own
# materials in the tiled output, after the per-type or thematic palette: glass
# reads darker and glossier than the wall it sits in.
_OPENING_TYPES = ("Window", "Door")
_OPENING_MATERIALS = [
    {"name": "window", "color": [0.30, 0.39, 0.47, 1.0], "roughness": 0.35},
    {"name": "door",   "color": [0.42, 0.30, 0.21, 1.0]},
]

# (id, type, provenance, description): added by the tiler next to the native
# node properties. District / function / roof-type names are decoded from the
# authoritative hubs; the address is parsed from the authoritative bldg:address.
_DERIVED_PROPS = [
    ("feature_type", "STRING", AUTHORITATIVE,
     "CityGML feature type (Building, Road, WaterBody, …)"),
    ("district", "STRING", AUTHORITATIVE, "District the feature is LOCATED_IN"),
    ("function_name", "STRING", AUTHORITATIVE, "Decoded building function (BuildingFunction)"),
    ("roof_type_name", "STRING", AUTHORITATIVE, "Decoded roof type (RoofType)"),
    ("address_street", "STRING", AUTHORITATIVE, "Street of the first bldg:address (xAL)"),
    ("address_house_number", "STRING", AUTHORITATIVE, "House number of the first bldg:address"),
    ("address_postcode", "STRING", AUTHORITATIVE, "Postcode of the first bldg:address"),
    ("address_locality", "STRING", AUTHORITATIVE, "Locality of the first bldg:address"),
    ("address_count", "FLOAT64", AUTHORITATIVE, "Number of bldg:address entries"),
    ("geometric_height", "FLOAT64", DERIVED,
     "bbox_max_z − bbox_min_z of the feature geometry (CRS units)"),
    ("footprint_area", "FLOAT64", DERIVED, "Area of the ground footprint (CRS units²)"),
    ("geometry_lod", "FLOAT64", DERIVED,
     "LoD of the geometry drawn in the tile (3: the reconstructed LoD3 facade, see lod3_*)"),
    ("longitude", "FLOAT64", DERIVED, "WGS84 longitude of the feature centre"),
    ("latitude", "FLOAT64", DERIVED, "WGS84 latitude of the feature centre"),
]
# Attributes kept on the LoD1 blocks of overview tiles (identification only).
_OVERVIEW_PROPS = frozenset({
    "id", "name", "gml_name", "feature_type", "district", "function_name",
    "measured_height", "geometric_height", "storeys_above_ground",
    "address_street", "address_house_number", "address_postcode", "address_locality",
    "osm_name", "osm_height", "predictedroofmaterial", "longitude", "latitude",
})
_XAL_FIELDS = {
    "ThoroughfareName": "address_street",
    "ThoroughfareNumber": "address_house_number",
    "PostalCodeNumber": "address_postcode",
    "LocalityName": "address_locality",
}


def _lonlat_to_ecef_np(lon_deg, lat_deg, h):
    """Vectorised lonlat_to_ecef for (N,) arrays → (N, 3)."""
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    sin_lat = np.sin(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat ** 2)
    return np.column_stack([
        (n + h) * np.cos(lat) * np.cos(lon),
        (n + h) * np.cos(lat) * np.sin(lon),
        (n * (1.0 - WGS84_E2) + h) * sin_lat,
    ])


def _lod_num(lod) -> int:
    m = re.search(r"\d", str(lod or ""))
    return int(m.group(0)) if m else 0


def _meta_id(key: str, used: set) -> str:
    """3D Metadata property ids must match ^[a-zA-Z_][a-zA-Z0-9_]*$. Keys that
    are mostly non-ASCII (Tokyo's Japanese PLATEAU keys) get a stable hashed id;
    the original key is kept as the property's display name."""
    pid = re.sub(r"[^A-Za-z0-9_]", "_", key)
    if not pid or pid[0].isdigit():
        pid = "_" + pid
    if pid != key and pid.count("_") > len(pid) / 2:
        pid = "p_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    base, i = pid, 2
    while pid in used:
        pid, i = f"{base}_{i}", i + 1
    used.add(pid)
    return pid


def discover_feature_schema(sess, source: str) -> list[dict]:
    """Schema of the per-feature property table: every scalar property found on
    the dataset's Buildings/CityObjects (numbers → FLOAT64, everything else →
    STRING) plus the derived properties. Geometry anchors and verbatim XML
    subtrees are left out."""
    rows = sess.run("""
        MATCH (n)-[:PART_OF]->(:Dataset {source: $src})
        WHERE n:Building OR n:CityObject
        UNWIND keys(n) AS k
        WITH k, valueType(n[k]) AS t
        RETURN k, collect(DISTINCT t) AS types, count(*) AS n
        ORDER BY k
    """, src=source).data()
    used: set = set()
    props = []
    for r in rows:
        key = r["k"]
        if (key in _META_EXCLUDE or key.startswith(("_", "embedding"))
                or key.endswith("_xml")):
            continue
        base_types = {t.split(" NOT NULL")[0] for t in r["types"]}
        if base_types & {"POINT", "MAP"}:
            continue
        mtype = "FLOAT64" if base_types <= {"INTEGER", "FLOAT"} else "STRING"
        pid = _meta_id(key, used)
        props.append({"id": pid, "name": key, "type": mtype, "derived": False,
                      "provenance": property_provenance(key), "count": r["n"]})
    native = {p["name"] for p in props}
    for pid, mtype, prov, desc in _DERIVED_PROPS:
        if pid in native:          # a native property of that name wins
            continue
        props.append({"id": _meta_id(pid, used), "name": pid, "type": mtype,
                      "derived": True, "provenance": prov, "description": desc})
    return props


def _schema_json(props: list[dict]) -> dict:
    """EXT_structural_metadata schema (embedded in every tile)."""
    out = {}
    for p in props:
        d = {"name": p["name"], "extras": {"provenance": p["provenance"]}}
        if p.get("description"):
            d["description"] = p["description"]
        if p["type"] == "FLOAT64":
            d.update({"type": "SCALAR", "componentType": "FLOAT64", "noData": _F64_NODATA})
        else:
            d.update({"type": "STRING", "noData": ""})
        if p["name"] == "id":
            d["semantic"] = "ID"
        elif p["name"] == "name":
            d["semantic"] = "NAME"
        out[p["id"]] = d
    return {"id": "pykci_features", "classes": {"feature": {
        "name": "City feature",
        "description": "Top-level CityGML feature (Building incl. BuildingParts, or CityObject)",
        "properties": out}}}


def _parse_xal(address_xml) -> dict:
    """Street / number / postcode / locality from the first bldg:address
    (address_xml holds the verbatim xAL subtree(s), a JSON list for
    multi-address buildings)."""
    if not address_xml:
        return {}
    try:
        items = json.loads(address_xml)
        items = items if isinstance(items, list) else [items]
    except (ValueError, TypeError):
        items = [address_xml]
    out = {"address_count": float(len(items))}
    if not items or not isinstance(items[0], str):
        return out
    try:
        root = etree.fromstring(items[0].encode("utf-8"),
                                parser=etree.XMLParser(recover=True))
    except Exception:
        return out
    if root is None:
        return out
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = el.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        field = _XAL_FIELDS.get(local)
        if field and field not in out and el.text and el.text.strip():
            out[field] = el.text.strip()
    return out


def _coerce(value, mtype):
    if value is None:
        return None
    if mtype == "FLOAT64":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


# ---- per-process context (workers re-create it after spawn) ----------------

_CTX: dict = {}


def _tile_ctx_init(ctx: dict):
    """Pool initializer: one driver + CRS transformer per worker process."""
    _CTX.clear()
    _CTX.update(ctx)
    _CTX["to_wgs84"] = Transformer.from_crs(ctx["horiz_epsg"], "EPSG:4326", always_xy=True)
    _CTX["center_ecef"] = np.array(ctx["center_ecef"])
    _CTX["basis"] = np.array(ctx["basis"])          # rows: east, north, up
    _CTX["driver"] = None


def _ctx_driver():
    if _CTX.get("driver") is None:
        _CTX["driver"] = _connect()
    return _CTX["driver"]


def _to_enu(pts: np.ndarray) -> np.ndarray:
    """(N,3) dataset-CRS points → (N,3) ENU metres in the shared tileset frame."""
    lon, lat = _CTX["to_wgs84"].transform(pts[:, 0], pts[:, 1])
    ecef = _lonlat_to_ecef_np(np.asarray(lon), np.asarray(lat), pts[:, 2] + _CTX["geoid_m"])
    return (ecef - _CTX["center_ecef"]) @ _CTX["basis"].T


# ---- graph reads ------------------------------------------------------------

_GEOM_HOLES = ("OPTIONAL MATCH (p)-[:HAS_INTERIOR_RING]->(ri:GeometryRing) "
               "WITH * ORDER BY ri.ring_index ")
_Q_BOUNDARY = """
    UNWIND $ids AS fid
    MATCH (f:Entity {id: fid})-[:HAS_BUILDING_PART*0..]->(g)
    MATCH (g)-[:HAS_BOUNDARY]->(bs:BoundarySurface)-[:HAS_POLYGON]->(p:GeometryPolygon)
          -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
    WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
    """ + _GEOM_HOLES + """
    WITH fid, g.id AS owner, bs.lod AS lod, bs.surface_type AS stype, p.id AS pid,
         r.pos_list AS ext, [x IN collect(ri.pos_list) WHERE x IS NOT NULL AND x <> ''] AS holes
    RETURN fid, owner, 'boundary' AS kind, lod, stype, '' AS role, pid, ext, holes,
           false AS impl, '' AS tm, '' AS ref
"""
_Q_SOLID = """
    UNWIND $ids AS fid
    MATCH (f:Entity {id: fid})-[:HAS_BUILDING_PART*0..]->(g)
    MATCH (g)-[:HAS_LOD_SOLID]->(s:GeometrySolid)-[:HAS_SURFACE_MEMBER*1..2]->(p:GeometryPolygon)
          -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
    WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
    """ + _GEOM_HOLES + """
    WITH fid, g.id AS owner, s.lod AS lod, p.id AS pid,
         r.pos_list AS ext, [x IN collect(ri.pos_list) WHERE x IS NOT NULL AND x <> ''] AS holes
    RETURN fid, owner, 'solid' AS kind, lod, null AS stype, '' AS role, pid, ext, holes,
           false AS impl, '' AS tm, '' AS ref
"""
_Q_MULTISURFACE = """
    UNWIND $ids AS fid
    MATCH (f:Entity {id: fid})-[:HAS_BUILDING_PART*0..]->(g)
    MATCH (g)-[hs:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)-[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
          -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
    WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
    """ + _GEOM_HOLES + """
    WITH fid, g.id AS owner, coalesce(ms.lod, hs.lod) AS lod, coalesce(hs.role, '') AS role,
         p.id AS pid, r.pos_list AS ext,
         [x IN collect(ri.pos_list) WHERE x IS NOT NULL AND x <> ''] AS holes,
         coalesce(ms.is_implicit, false) AS impl,
         coalesce(ms.transformation_matrix, '') AS tm, coalesce(ms.reference_point, '') AS ref
    RETURN fid, owner, 'ms' AS kind, lod, null AS stype, role, pid, ext, holes, impl, tm, ref
"""
# Reconstructed LoD3 facades (enrich_lod3_facades.py): an additive overlay reached
# via HAS_LOD3_FACADE. Its wall polygons carry the window/door holes, and the
# bldg:opening polygons fill them. Where present it is drawn instead of the
# authoritative LoD2 shell (see _select_representation; --no-lod3 keeps LoD2).
_Q_LOD3 = """
    UNWIND $ids AS fid
    MATCH (f:Entity {id: fid})-[:HAS_BUILDING_PART*0..]->(g)
    MATCH (g)-[:HAS_LOD3_FACADE]->(bs:BoundarySurface)
    CALL (bs) {
        MATCH (bs)-[:HAS_POLYGON]->(p:GeometryPolygon)
        RETURN bs.surface_type AS stype, p
      UNION ALL
        MATCH (bs)-[:HAS_OPENING]->(o:Opening)-[:HAS_LOD_SURFACE]->(:GeometryMultiSurface)
              -[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
        RETURN o.opening_type AS stype, p
    }
    MATCH (p)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
    WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
    """ + _GEOM_HOLES + """
    WITH fid, g.id AS owner, stype, p.id AS pid,
         r.pos_list AS ext, [x IN collect(ri.pos_list) WHERE x IS NOT NULL AND x <> ''] AS holes
    RETURN fid, owner, 'lod3' AS kind, '3' AS lod, stype, '' AS role, pid, ext, holes,
           false AS impl, '' AS tm, '' AS ref
"""
_Q_ATTRS = """
    UNWIND $ids AS fid
    MATCH (n:Entity {id: fid})
    OPTIONAL MATCH (n)-[:LOCATED_IN]->(d:District)
    OPTIONAL MATCH (n)-[:HAS_FUNCTION]->(bf:BuildingFunction)
    OPTIONAL MATCH (n)-[:HAS_ROOF_TYPE]->(rt:RoofType)
    WITH fid, n, collect(DISTINCT d.name) AS districts,
         collect(DISTINCT bf.name) AS functions, collect(DISTINCT rt.name) AS roof_types
    RETURN fid, properties(n) AS props,
           CASE WHEN n:Building THEN 'Building' ELSE coalesce(n.feature_type, 'CityObject') END AS ft,
           districts, functions, roof_types
"""


def fetch_feature_index(sess, source: str) -> list[dict]:
    """Every top-level feature of the dataset with a planar position + bbox.
    Features lacking centre and bbox (e.g. a CityObject before OSM fusion
    back-filled its bbox) are positioned at the first vertex of their geometry."""
    feats = sess.run("""
        MATCH (n)-[:PART_OF]->(:Dataset {source: $src})
        WHERE n:Building OR n:CityObject
        RETURN n.id AS id, n:Building AS is_bldg,
               n.center_x AS cx, n.center_y AS cy,
               n.bbox_min_x AS x0, n.bbox_min_y AS y0, n.bbox_min_z AS z0,
               n.bbox_max_x AS x1, n.bbox_max_y AS y1, n.bbox_max_z AS z1
    """, src=source).data()
    missing = []
    for f in feats:
        if f["cx"] is None or f["cy"] is None:
            if None not in (f["x0"], f["y0"], f["x1"], f["y1"]):
                f["cx"], f["cy"] = (f["x0"] + f["x1"]) / 2, (f["y0"] + f["y1"]) / 2
            else:
                missing.append(f["id"])
    if missing:
        found = {}
        for i in range(0, len(missing), 2000):
            for r in sess.run("""
                UNWIND $ids AS fid
                MATCH (n:Entity {id: fid})
                CALL (n) {
                    OPTIONAL MATCH (n)-[:HAS_BUILDING_PART*0..]->()
                          -[:HAS_BOUNDARY|HAS_LOD_SURFACE|HAS_LOD_SOLID]->(c)
                          -[:HAS_POLYGON|HAS_SURFACE_MEMBER*1..2]->(:GeometryPolygon)
                          -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
                    RETURN r.pos_list AS pl, c.reference_point AS ref,
                           coalesce(c.is_implicit, false) AS impl
                    LIMIT 1
                }
                RETURN fid, pl, ref, impl
            """, ids=missing[i:i + 2000]):
                src_str = r["ref"] if r["impl"] and r["ref"] else r["pl"]
                if src_str:
                    v = [float(t) for t in src_str.split()[:3]]
                    if len(v) >= 2:
                        found[r["fid"]] = v
        for f in feats:
            if f["id"] in found:
                v = found[f["id"]]
                f["cx"], f["cy"] = v[0], v[1]
                f["z0"] = f["z0"] if f["z0"] is not None else (v[2] if len(v) > 2 else 0.0)
    kept = [f for f in feats if f["cx"] is not None and f["cy"] is not None]
    if len(kept) < len(feats):
        print(f"  Note: {len(feats) - len(kept)} feature(s) without any geometry skipped.")
    return kept


def _select_representation(polys: list[dict]) -> list[dict]:
    """Pick ONE geometry representation per owner (a Building, BuildingPart or
    CityObject) so the same surface is not drawn twice: a reconstructed LoD3
    facade, else boundary surfaces, else the highest-LoD solid, else the
    highest-LoD multi-surface (lod0 footprint / roof edge only when nothing else
    exists). Polygons shared by xlink are de-duplicated."""
    by_kind: dict = {}
    for p in polys:
        by_kind.setdefault(p["kind"], []).append(p)
    if "lod3" in by_kind:
        cand = by_kind["lod3"]
    elif "boundary" in by_kind:
        cand = by_kind["boundary"]
    elif "solid" in by_kind:
        cand = by_kind["solid"]
    else:
        cand = by_kind.get("ms", [])
        upper = [p for p in cand if p["role"] not in _LOD0_ROLES]
        cand = upper or cand
    if not cand:
        return []
    top = max(_lod_num(p["lod"]) for p in cand)
    seen, out = set(), []
    for p in cand:
        if _lod_num(p["lod"]) != top:
            continue
        key = (p["pid"], p["tm"], p["ref"])
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# ---- per-feature geometry → triangles ----------------------------------------

def _material_type(ft: str, poly: dict, normal_up: float) -> str:
    """Surface type used for the material: the boundary surface type for
    buildings (solid / multi-surface faces classified by their normal), the
    module feature type for every other CityObject."""
    if ft != "Building":
        return ft
    if poly["stype"]:
        return poly["stype"]
    if normal_up > 0.7:
        return "RoofSurface"
    if normal_up < -0.7:
        return "GroundSurface"
    return "WallSurface"


def _feature_mesh(ft: str, polys: list[dict]):
    """Triangulate one feature. Returns (parts, info): parts is a list of
    (surface_type, verts_enu (N,3), normal_enu (3,), tri_idx (M,)); info holds
    the planar footprint material for overview tiles and the area attribute."""
    parts = []
    ground_rings, all_xy = [], []
    zmin, zmax = math.inf, -math.inf
    for p in polys:
        ext = pos_str_to_pts(p["ext"])
        if len(ext) < 3:
            continue
        holes = [pos_str_to_pts(h) for h in (p["holes"] or [])]
        if p["impl"] and p["tm"]:
            ext = _apply_implicit_transform(ext, p["tm"], p["ref"])
            holes = [_apply_implicit_transform(h, p["tm"], p["ref"]) for h in holes]
        zmin, zmax = min(zmin, float(ext[:, 2].min())), max(zmax, float(ext[:, 2].max()))
        all_xy.append(ext[:, :2])
        if p["stype"] == "GroundSurface":
            ground_rings.append(ext[:, :2])
        res = triangulate_polygon_enu(_to_enu(ext), [_to_enu(h) for h in holes if len(h) >= 3])
        if res is None:
            continue
        verts, normal, tri = res
        parts.append((_material_type(ft, p, float(normal[2])), verts, normal, tri))

    info = {"zmin": zmin, "zmax": zmax, "footprint": None, "area": None, "xy": None}
    if all_xy:
        xy = np.vstack(all_xy)
        info["xy"] = (*xy.min(axis=0).tolist(), *xy.max(axis=0).tolist())
    if not all_xy or ft != "Building":
        return parts, info
    foot = None
    if _HAVE_SHAPELY:
        try:
            from shapely.geometry import MultiPoint
            if ground_rings:
                foot = _sh_unary_union([_ShPolygon(r).buffer(0) for r in ground_rings
                                        if len(r) >= 3])
            if foot is None or foot.is_empty:
                foot = MultiPoint(np.vstack(all_xy)).convex_hull
            info["area"] = float(foot.area)
            simple = foot.simplify(0.5, preserve_topology=True)
            if simple.geom_type == "MultiPolygon":
                simple = max(simple.geoms, key=lambda g: g.area)
            if simple.geom_type == "Polygon" and not simple.is_empty:
                info["footprint"] = np.asarray(simple.exterior.coords)[:-1, :2]
        except Exception:
            foot = None
    if info["footprint"] is None:                      # bbox fallback
        x0, y0, x1, y1 = info["xy"]
        info["footprint"] = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        info["area"] = info["area"] if info["area"] is not None else float((x1 - x0) * (y1 - y0))
    return parts, info


def _attribute_row(rec: dict, schema: list[dict]) -> dict:
    props = rec["props"]
    derived = {
        "feature_type": rec["ft"],
        "district": "; ".join(sorted(x for x in rec["districts"] if x)) or None,
        "function_name": "; ".join(sorted(x for x in rec["functions"] if x)) or None,
        "roof_type_name": "; ".join(sorted(x for x in rec["roof_types"] if x)) or None,
        **_parse_xal(props.get("address_xml")),
    }
    if props.get("bbox_max_z") is not None and props.get("bbox_min_z") is not None:
        derived["geometric_height"] = props["bbox_max_z"] - props["bbox_min_z"]
    cx, cy = props.get("center_x"), props.get("center_y")
    if cx is None and props.get("bbox_min_x") is not None:
        cx = (props["bbox_min_x"] + props["bbox_max_x"]) / 2
        cy = (props["bbox_min_y"] + props["bbox_max_y"]) / 2
    if cx is not None and cy is not None:
        lon, lat = _CTX["to_wgs84"].transform(cx, cy)
        derived["longitude"], derived["latitude"] = float(lon), float(lat)
    row = {}
    for p in schema:
        v = derived.get(p["name"]) if p["derived"] else props.get(p["name"])
        if v is None:
            # a native key of a derived name (CityObjects store feature_type,
            # Buildings do not) still gets the derived value where it is absent
            v = derived.get(p["name"])
        row[p["id"]] = _coerce(v, p["type"])
    return row


# ---- GLB packing with feature ids + property table -----------------------------

class _GlbWriter:
    def __init__(self):
        self.buf = bytearray()
        self.views: list = []
        self.accessors: list = []

    def view(self, data: bytes, target=None, align: int = 4) -> int:
        while len(self.buf) % align:
            self.buf.append(0)
        v = {"buffer": 0, "byteOffset": len(self.buf), "byteLength": len(data)}
        if target is not None:
            v["target"] = target
        self.buf.extend(data)
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, arr: np.ndarray, component_type: int, type_: str, target, minmax=False) -> int:
        acc = {"bufferView": self.view(arr.tobytes(), target), "byteOffset": 0,
               "componentType": component_type, "count": int(len(arr)), "type": type_}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def glb(self, gltf: dict) -> bytes:
        gltf["bufferViews"] = self.views
        gltf["accessors"] = self.accessors
        while len(self.buf) % 4:
            self.buf.append(0)
        gltf["buffers"] = [{"byteLength": len(self.buf)}]
        js = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        js += b" " * (-len(js) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.buf)
        return (struct.pack("<III", 0x46546C67, 2, total)
                + struct.pack("<II", len(js), 0x4E4F534A) + js
                + struct.pack("<II", len(self.buf), 0x004E4942) + bytes(self.buf))


def _property_table(w: _GlbWriter, schema: list[dict], rows: list[dict]) -> dict:
    """Encode rows as one EXT_structural_metadata property table. Properties
    that are absent on every feature of this tile are omitted (a viewer then
    reports them as undefined, which is what 'not covered' means)."""
    props = {}
    for p in schema:
        vals = [r.get(p["id"]) for r in rows]
        if all(v is None for v in vals):
            continue
        if p["type"] == "FLOAT64":
            arr = np.array([_F64_NODATA if v is None else v for v in vals], dtype="<f8")
            props[p["id"]] = {"values": w.view(arr.tobytes(), align=8)}
        else:
            enc = [("" if v is None else v).encode("utf-8") for v in vals]
            offsets = np.zeros(len(enc) + 1, dtype="<u4")
            offsets[1:] = np.cumsum([len(e) for e in enc], dtype=np.uint64)
            data = b"".join(enc) or b" "      # a bufferView must not be empty
            props[p["id"]] = {"values": w.view(data, align=8),
                              "stringOffsets": w.view(offsets.tobytes(), align=8),
                              "stringOffsetType": "UINT32"}
    return {"name": "features", "class": "feature", "count": len(rows), "properties": props}


def _pack_tile(mesh_parts, materials, mat_index, rows, schema) -> bytes:
    """mesh_parts: list of (feature_index, feature_id, surface_type, verts, normal, tri).
    schema: feature properties to embed (None → feature ids without metadata).
    Each tile embeds a schema listing only the properties present in its table;
    feature_schema.json holds the full list."""
    n_mats = len(materials)
    buckets = [[] for _ in range(n_mats)]
    for fidx, fid, stype, verts, normal, tri in mesh_parts:
        buckets[mat_index(fid, stype)].append((fidx, verts, normal, tri))

    w = _GlbWriter()
    prims = []
    for mi, items in enumerate(buckets):
        if not items:
            continue
        pos, nor, ids, idx, off = [], [], [], [], 0
        for fidx, verts, normal, tri in items:
            n = len(verts)
            pos.append(verts)
            nor.append(np.repeat(np.asarray(normal, dtype=np.float64)[None, :], n, axis=0))
            ids.append(np.full(n, fidx, dtype=np.float32))
            idx.append(np.asarray(tri, dtype=np.int64) + off)
            off += n
        p = np.vstack(pos)
        nn = np.vstack(nor)
        # ENU (x=east, y=north, z=up) → glTF y-up: (east, up, −north)
        p_gl = np.column_stack([p[:, 0], p[:, 2], -p[:, 1]]).astype(np.float32)
        n_gl = np.column_stack([nn[:, 0], nn[:, 2], -nn[:, 1]]).astype(np.float32)
        prim = {
            "attributes": {
                "POSITION": w.accessor(p_gl, 5126, "VEC3", 34962, minmax=True),
                "NORMAL": w.accessor(n_gl, 5126, "VEC3", 34962),
                "_FEATURE_ID_0": w.accessor(np.concatenate(ids), 5126, "SCALAR", 34962),
            },
            "indices": w.accessor(np.concatenate(idx).astype(np.uint32), 5125, "SCALAR", 34963),
            "material": mi,
            "mode": 4,
        }
        fid_set = {"featureCount": len(rows), "attribute": 0}
        if schema is not None:
            fid_set["propertyTable"] = 0
        prim["extensions"] = {"EXT_mesh_features": {"featureIds": [fid_set]}}
        prims.append(prim)

    gltf = {
        "asset": {"version": "2.0", "generator": "pykci build_3dtiles.py --tiled"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": prims}],
        "materials": [_gltf_material(m) for m in materials],
        "extensionsUsed": ["EXT_mesh_features"],
    }
    if schema is not None:
        table = _property_table(w, schema, rows)
        present = [p for p in schema if p["id"] in table["properties"]]
        gltf["extensionsUsed"].append("EXT_structural_metadata")
        gltf["extensions"] = {"EXT_structural_metadata": {
            "schema": _schema_json(present), "propertyTables": [table]}}
    return w.glb(gltf)


def _gltf_material(m: dict) -> dict:
    col = m["color"]
    alpha = col[3] if len(col) > 3 else 1.0
    mat = {"name": m["name"],
           "pbrMetallicRoughness": {"baseColorFactor": [col[0], col[1], col[2], alpha],
                                    "metallicFactor": 0.0,
                                    "roughnessFactor": m.get("roughness", 0.85)},
           "doubleSided": True}
    if alpha < 1.0:
        mat["alphaMode"] = "BLEND"
    return mat


def _materials_for(ctx: dict, attrs_by_id: dict):
    """(materials, index_fn), with the same palettes as the single-GLB path plus
    the LoD3 opening materials (windows, doors) appended."""
    if ctx["color_by"] == "roofmat":
        b_attrs = {fid: {"roofmat": a["props"].get("predictedroofmaterial"),
                         "is_lod3": bool(a["props"].get("lod3_enriched"))}
                   for fid, a in attrs_by_id.items() if a["ft"] == "Building"}
        materials, index_fn = build_thematic_materials(b_attrs, ctx["palette"], ctx["opacity"])
    else:
        materials, index_fn = MATERIALS_GLTF, (lambda fid, stype: FEATURE_MATERIAL.get(stype, 0))
    base = len(materials)

    def with_openings(fid: str, stype: str) -> int:
        if stype in _OPENING_TYPES:
            return base + _OPENING_TYPES.index(stype)
        return index_fn(fid, stype)

    return [*materials, *_OPENING_MATERIALS], with_openings


# ---- leaf worker ------------------------------------------------------------------

def _build_leaf_tile(job: dict) -> dict:
    """Fetch, triangulate and write one leaf tile. Returns tile statistics and
    the overview candidates (the most prominent buildings, as footprints)."""
    ids, out_path = job["ids"], Path(job["out"])
    ctx = _CTX
    with _ctx_driver().session(database="neo4j") as sess:
        attrs = {r["fid"]: r for r in sess.run(_Q_ATTRS, ids=ids).data()}
        geom: dict = {}
        queries = (_Q_BOUNDARY, _Q_SOLID, _Q_MULTISURFACE) + ((_Q_LOD3,) if ctx["lod3"] else ())
        for q in queries:
            for r in sess.run(q, ids=ids):
                geom.setdefault(r["fid"], {}).setdefault(r["owner"], []).append(dict(r))

    materials, mat_index = _materials_for(ctx, attrs)
    schema = ctx["schema"]
    area_id = next((p["id"] for p in schema if p["name"] == "footprint_area"), None)
    lod_id = next((p["id"] for p in schema if p["name"] == "geometry_lod"), None)
    overview_ids = {p["id"] for p in schema if p["name"] in _OVERVIEW_PROPS}
    mesh_parts, rows, cands = [], [], []
    zmin, zmax, n_tri, n_lod3 = math.inf, -math.inf, 0, 0
    bx0 = by0 = math.inf
    bx1 = by1 = -math.inf
    for fid in ids:
        owners = geom.get(fid)
        rec = attrs.get(fid)
        if not owners or rec is None:
            continue
        polys = [p for owner_polys in owners.values()
                 for p in _select_representation(owner_polys)]
        parts, info = _feature_mesh(rec["ft"], polys)
        if not parts:
            continue
        fidx = len(rows)
        for stype, verts, normal, tri in parts:
            mesh_parts.append((fidx, fid, stype, verts, normal, tri))
            n_tri += len(tri) // 3
        row = _attribute_row(rec, schema) if ctx["attributes"] else {}
        if area_id and info["area"] is not None:
            row[area_id] = info["area"]
        geometry_lod = max(_lod_num(p["lod"]) for p in polys)
        n_lod3 += any(p["kind"] == "lod3" for p in polys)
        if lod_id and geometry_lod:
            row[lod_id] = float(geometry_lod)
        rows.append(row)
        zmin, zmax = min(zmin, info["zmin"]), max(zmax, info["zmax"])
        if info["xy"]:
            bx0, by0 = min(bx0, info["xy"][0]), min(by0, info["xy"][1])
            bx1, by1 = max(bx1, info["xy"][2]), max(by1, info["xy"][3])
        if rec["ft"] == "Building" and info["footprint"] is not None:
            height = max(info["zmax"] - info["zmin"], 1.0)
            cands.append({"id": fid, "prominence": (info["area"] or 0.0) * height,
                          "footprint": info["footprint"].tolist(),
                          "z0": info["zmin"], "z1": info["zmax"],
                          # overview tiles carry the identifying subset only; the
                          # full record is one click away in the leaf tile / graph
                          "row": {k: v for k, v in row.items() if k in overview_ids},
                          "roofmat": rec["props"].get("predictedroofmaterial"),
                          "is_lod3": bool(rec["props"].get("lod3_enriched"))})
    if not rows:
        return {"path": job["path"], "features": 0}
    glb = _pack_tile(mesh_parts, materials, mat_index, rows,
                     schema if ctx["attributes"] else None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(glb)
    cands.sort(key=lambda c: c["prominence"], reverse=True)
    # A parent merges up to four children, so each leaf offers a quarter of the
    # overview budget: bounded memory, and the overview stays spread across space.
    keep = max(1, ctx["overview_features"] // 4) if ctx["overview_features"] else 0
    return {"path": job["path"], "features": len(rows), "triangles": n_tri, "lod3": n_lod3,
            "bytes": len(glb), "zmin": zmin, "zmax": zmax,
            "bounds": (bx0, by0, bx1, by1), "candidates": cands[:keep]}


def _build_overview_tile(cands: list[dict], out_path: Path) -> dict:
    """LoD1 blocks (footprint extruded from z0 to z1) for a coarse tile."""
    ctx = _CTX
    b_attrs = {c["id"]: {"roofmat": c["roofmat"], "is_lod3": c["is_lod3"]} for c in cands}
    if ctx["color_by"] == "roofmat":
        materials, mat_index = build_thematic_materials(b_attrs, ctx["palette"], ctx["opacity"])
    else:
        materials, mat_index = MATERIALS_GLTF, (lambda fid, stype: FEATURE_MATERIAL.get(stype, 0))
    mesh_parts, rows = [], []
    for c in cands:
        fp = np.asarray(c["footprint"], dtype=np.float64)
        if len(fp) < 3:
            continue
        fidx = len(rows)
        roof = _to_enu(np.column_stack([fp, np.full(len(fp), c["z1"])]))
        base = _to_enu(np.column_stack([fp, np.full(len(fp), c["z0"])]))
        res = triangulate_polygon_enu(roof)
        if res is not None:
            verts, normal, tri = res
            if normal[2] < 0:                       # keep roofs facing up
                normal, tri = -normal, tri.reshape(-1, 3)[:, ::-1].ravel()
            mesh_parts.append((fidx, c["id"], "RoofSurface", verts, normal, tri))
        n = len(fp)
        for i in range(n):
            j = (i + 1) % n
            quad = np.array([base[i], base[j], roof[j], roof[i]])
            nrm = np.cross(quad[1] - quad[0], quad[3] - quad[0])
            ln = np.linalg.norm(nrm)
            if ln < 1e-9:
                continue
            mesh_parts.append((fidx, c["id"], "WallSurface", quad, nrm / ln,
                               np.array([0, 1, 2, 0, 2, 3], dtype=np.int32)))
        rows.append(c["row"])
    if not rows:
        return {"features": 0}
    glb = _pack_tile(mesh_parts, materials, mat_index, rows,
                     ctx["schema"] if ctx["attributes"] else None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(glb)
    return {"features": len(rows), "bytes": len(glb)}


# ---- quadtree + tileset assembly --------------------------------------------------

def _quadtree(feats: list[dict], max_features: int, max_depth: int = 16) -> dict:
    """Split features (by centre) into a quadtree; leaves hold <= max_features."""
    def bounds_of(items):
        xs0 = [f["x0"] if f["x0"] is not None else f["cx"] for f in items]
        ys0 = [f["y0"] if f["y0"] is not None else f["cy"] for f in items]
        xs1 = [f["x1"] if f["x1"] is not None else f["cx"] for f in items]
        ys1 = [f["y1"] if f["y1"] is not None else f["cy"] for f in items]
        return min(xs0), min(ys0), max(xs1), max(ys1)

    def build(items, rect, path, depth):
        node = {"path": path, "bounds": bounds_of(items)}
        x0, y0, x1, y1 = rect
        if len(items) <= max_features or depth >= max_depth or (x1 - x0) < 1.0:
            node["ids"] = [f["id"] for f in items]
            node["z"] = _z_range(items)
            return node
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        quads = [[], [], [], []]
        for f in items:
            quads[(f["cx"] >= mx) + 2 * (f["cy"] >= my)].append(f)
        rects = [(x0, y0, mx, my), (mx, y0, x1, my), (x0, my, mx, y1), (mx, my, x1, y1)]
        node["children"] = [build(q, rects[k], f"{path}{k}", depth + 1)
                            for k, q in enumerate(quads) if q]
        node["z"] = _z_range(items)
        return node

    xs = [f["cx"] for f in feats]
    ys = [f["cy"] for f in feats]
    side = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    return build(feats, (min(xs), min(ys), min(xs) + side, min(ys) + side), "r", 0)


def _z_range(items):
    z0 = [f["z0"] for f in items if f["z0"] is not None]
    z1 = [f["z1"] for f in items if f["z1"] is not None]
    lo = min(z0) if z0 else 0.0
    return lo, (max(z1) if z1 else lo)


def _region(bounds, z, to_wgs84, geoid_m) -> list:
    x0, y0, x1, y1 = bounds
    xs = [x0, x1, x1, x0, (x0 + x1) / 2, (x0 + x1) / 2, x0, x1]
    ys = [y0, y0, y1, y1, y0, y1, (y0 + y1) / 2, (y0 + y1) / 2]
    lon, lat = to_wgs84.transform(xs, ys)
    eps = 1e-7
    return [math.radians(min(lon)) - eps, math.radians(min(lat)) - eps,
            math.radians(max(lon)) + eps, math.radians(max(lat)) + eps,
            float(z[0] + geoid_m) - 1.0, float(z[1] + geoid_m) + 1.0]


def build_3dtiles_tiled(source: str, out_dir: Path, color_by: str = "type",
                        palette: str = "soft", opacity: float | None = None,
                        max_features: int = 1500, overview_features: int = 1500,
                        attributes: bool = True, workers: int = 1, lod3: bool = True,
                        geoid_offset: float | None = None):
    t_start = time.perf_counter()
    driver = _connect()
    print("Connected to Neo4j.")
    with driver.session(database="neo4j") as sess:
        ds = fetch_dataset(sess, source)
        if not ds:
            driver.close()
            raise SystemExit(f"ERROR: no Dataset with source='{source}' found in graph.")
        srs_name = ds.get("crs", "")
        horiz_epsg = _horiz_epsg_from_srs(srs_name)
        print(f"  Dataset     : {source}\n  srsName     : {srs_name}\n  Horiz. CRS  : {horiz_epsg}")
        progress("tiles.index", message="reading feature index")
        feats = fetch_feature_index(sess, source)
        if attributes:
            progress("tiles.schema", message="discovering feature attributes")
            schema = discover_feature_schema(sess, source)
        else:
            schema = []
    driver.close()
    if not feats:
        print("WARNING: no top-level features with geometry; nothing to tile.")
        return None
    print(f"  {len(feats):,} top-level features"
          + (f", {len(schema)} attribute(s) per feature" if attributes else ""))

    to_wgs84 = Transformer.from_crs(horiz_epsg, "EPSG:4326", always_xy=True)
    xs = np.array([f["cx"] for f in feats])
    ys = np.array([f["cy"] for f in feats])
    z0s = np.array([f["z0"] for f in feats if f["z0"] is not None] or [0.0])
    mid_x, mid_y, mid_z = float((xs.min() + xs.max()) / 2), float((ys.min() + ys.max()) / 2), \
        float(np.median(z0s))
    geoid_m, geoid_source = _geoid_m(srs_name, mid_x, mid_y, mid_z, geoid_offset)
    print(f"  Geoid offset: {geoid_m:+.2f} m ({geoid_source})")
    c_lon, c_lat = to_wgs84.transform(mid_x, mid_y)
    center_ecef = np.array(lonlat_to_ecef(c_lon, c_lat, mid_z + geoid_m))
    east_v, north_v, up_v = enu_basis(c_lon, c_lat)

    ctx = {
        "horiz_epsg": horiz_epsg, "geoid_m": geoid_m,
        "center_ecef": center_ecef.tolist(),
        "basis": [east_v.tolist(), north_v.tolist(), up_v.tolist()],
        "color_by": color_by, "palette": palette, "opacity": opacity,
        "attributes": attributes, "overview_features": overview_features,
        "schema": schema, "lod3": lod3,
    }
    _tile_ctx_init(ctx)

    tree = _quadtree(feats, max_features)
    leaves, stack = [], [tree]
    while stack:
        nd = stack.pop()
        if "ids" in nd:
            leaves.append(nd)
        else:
            stack.extend(nd["children"])
    print(f"  Quadtree: {len(leaves)} leaf tile(s), <= {max_features} features each")

    tiles_dir = out_dir / "tiles"
    jobs = [{"ids": lf["ids"], "path": lf["path"], "out": str(tiles_dir / f"{lf['path']}.glb")}
            for lf in leaves]
    results: dict = {}
    progress("tiles.leaf", done=0, total=len(jobs), unit="tiles")
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers, initializer=_tile_ctx_init,
                                 initargs=(ctx,)) as pool:
            futs = [pool.submit(_build_leaf_tile, j) for j in jobs]
            for fut in as_completed(futs):
                r = fut.result()
                results[r["path"]] = r
                progress("tiles.leaf", done=len(results), total=len(jobs), unit="tiles")
    else:
        for j in jobs:
            r = _build_leaf_tile(j)
            results[r["path"]] = r
            progress("tiles.leaf", done=len(results), total=len(jobs), unit="tiles")
            if len(results) % 25 == 0:
                print(f"  leaf tiles {len(results)}/{len(jobs)}", flush=True)
    if _CTX.get("driver") is not None:
        _CTX["driver"].close()
        _CTX["driver"] = None

    # ---- bottom-up: overview content + tileset JSON -----------------------------
    n_internal = sum(1 for _ in _iter_internal(tree))
    done_internal = [0]

    def assemble(nd):
        """Returns (tile_json | None, candidates, z_range, xy_bounds). Bounding
        volumes come from the triangulated geometry, not the stored bboxes, so
        BuildingParts or features without a bbox can never fall outside them."""
        if "ids" in nd:
            r = results.get(nd["path"], {})
            if not r.get("features"):
                return None, [], None, None
            z, b = (r["zmin"], r["zmax"]), r["bounds"]
            tile = {"boundingVolume": {"region": _region(b, z, to_wgs84, geoid_m)},
                    "geometricError": 0.0,
                    "content": {"uri": f"tiles/{nd['path']}.glb"}}
            return tile, r["candidates"], z, b
        children, cands, zs, bs = [], [], [], []
        for ch in nd["children"]:
            t, c, z, b = assemble(ch)
            if t is not None:
                children.append(t)
                cands.extend(c)
                zs.append(z)
                bs.append(b)
        if not children:
            return None, [], None, None
        z = (min(a for a, _ in zs), max(b for _, b in zs))
        b = (min(v[0] for v in bs), min(v[1] for v in bs),
             max(v[2] for v in bs), max(v[3] for v in bs))
        diag = math.hypot(b[2] - b[0], b[3] - b[1])
        tile = {"boundingVolume": {"region": _region(b, z, to_wgs84, geoid_m)},
                "geometricError": round(max(diag / 20.0, 5.0), 2),
                "children": children}
        cands.sort(key=lambda c: c["prominence"], reverse=True)
        cands = cands[:overview_features]
        if overview_features > 0 and cands:
            ov = _build_overview_tile(cands, tiles_dir / f"{nd['path']}.glb")
            if ov.get("features"):
                tile["content"] = {"uri": f"tiles/{nd['path']}.glb"}
        done_internal[0] += 1
        progress("tiles.overview", done=done_internal[0], total=n_internal, unit="tiles")
        return tile, cands, z, b

    root, _, _, _ = assemble(tree)
    if root is None:
        print("WARNING: no geometry could be triangulated; nothing written.")
        return None

    root["transform"] = [
        float(east_v[0]), float(east_v[1]), float(east_v[2]), 0.0,
        float(north_v[0]), float(north_v[1]), float(north_v[2]), 0.0,
        float(up_v[0]), float(up_v[1]), float(up_v[2]), 0.0,
        float(center_ecef[0]), float(center_ecef[1]), float(center_ecef[2]), 1.0,
    ]
    root["refine"] = "REPLACE"
    if "children" not in root:          # whole dataset fits in one leaf
        root["geometricError"] = 0.0
    n_feat = sum(r.get("features", 0) for r in results.values())
    n_lod3 = sum(r.get("lod3", 0) for r in results.values())
    tileset = {
        "asset": {"version": "1.1", "generator": "pykci build_3dtiles.py --tiled"},
        "geometricError": round(max(root["geometricError"] * 2, 50.0), 2),
        "root": root,
        "extras": {"pykci": {
            "dataset": source, "crs": srs_name, "horizontal_crs": horiz_epsg,
            "source_crs": ds.get("source_crs"),
            "geoid_offset_m": round(geoid_m, 3), "geoid_source": geoid_source,
            "center": {"longitude": c_lon, "latitude": c_lat,
                       "height": round(mid_z + geoid_m, 3)},
            "ground_height_median": round(mid_z + geoid_m, 3),
            "features": n_feat, "lod3_features": n_lod3, "leaf_tiles": len(leaves),
            "color_by": color_by, "attributes": bool(attributes),
            "feature_schema": "feature_schema.json" if attributes else None,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tileset.json").write_text(json.dumps(tileset, indent=1), encoding="utf-8")
    if attributes:
        (out_dir / "feature_schema.json").write_text(json.dumps({
            "class": "feature",
            "provenance_layers": LAYERS,
            "properties": schema,
        }, indent=1, ensure_ascii=False), encoding="utf-8")

    total_bytes = sum(r.get("bytes", 0) for r in results.values())
    n_tri = sum(r.get("triangles", 0) for r in results.values())
    elapsed = time.perf_counter() - t_start
    print(f"  {n_feat:,} features, {n_tri:,} triangles in {len(leaves)} leaf tiles "
          f"({total_bytes / 1e6:,.1f} MB) + {n_internal} overview tiles  [{elapsed:.0f} s]")
    if n_lod3:
        print(f"  {n_lod3:,} building(s) drawn with their reconstructed LoD3 facade")
    print(f"  tileset.json : {out_dir / 'tileset.json'}")
    progress("tiles.done", done=len(leaves), total=len(leaves), unit="tiles",
             features=n_feat, triangles=n_tri, bytes=total_bytes, out=str(out_dir))
    return c_lon, c_lat


def _iter_internal(nd):
    if "children" in nd:
        yield nd
        for ch in nd["children"]:
            yield from _iter_internal(ch)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", "--source", default=None, dest="dataset",
                    help="Value of Dataset.source in the graph to export "
                         "(e.g. hamburg_lod2.gml). All geometry comes from Neo4j — "
                         "the original GML file is NOT read. Omit when there is "
                         "exactly one dataset in the graph.")
    ap.add_argument("--out", default="output/3dtiles",
                    help="Output directory (default: output/3dtiles)")
    ap.add_argument("--color-by", choices=["type", "roofmat"], default="type",
                    dest="color_by",
                    help="Building colouring: 'type' (per-surface-type, default) or "
                         "'roofmat' (ML-predicted roof material as translucent base "
                         "coat + gold accent for LoD3-reconstructed buildings)")
    ap.add_argument("--palette", choices=sorted(ROOFMAT_PALETTES), default="soft",
                    help="Roof-material palette (roofmat mode): 'soft' muted tones "
                         "for translucent overlays (default), 'vivid' basic main "
                         "colours for opaque renders")
    ap.add_argument("--opacity", type=float, default=None,
                    help="Override the baked per-role alphas (roofmat mode), e.g. "
                         "1.0 for fully opaque buildings; a viewer can still fade "
                         "opaque tiles at runtime")
    ap.add_argument("--tiled", action="store_true",
                    help="Write a streamable quadtree tileset (many small GLBs, LoD1 "
                         "overview tiles, per-feature semantic attributes) instead of "
                         "one GLB; use for whole cities")
    ap.add_argument("--max-features", type=int, default=1500,
                    help="--tiled: max top-level features per leaf tile (default 1500)")
    ap.add_argument("--overview-features", type=int, default=1500,
                    help="--tiled: buildings shown as LoD1 blocks in each coarser tile "
                         "(default 1500; 0 = no overview tiles)")
    ap.add_argument("--attributes", choices=["all", "none"], default="all",
                    help="--tiled: embed every scalar feature attribute as "
                         "EXT_structural_metadata (default 'all')")
    ap.add_argument("--workers", type=int, default=1,
                    help="--tiled: parallel worker processes for the leaf tiles "
                         f"(default 1; this machine has {os.cpu_count()} CPUs)")
    ap.add_argument("--geoid-offset", type=float, default=None,
                    help="geoid undulation in m (orthometric + offset = ellipsoidal height) instead of "
                         "deriving it with PROJ, e.g. where PROJ has no geoid grid; recorded in "
                         "extras.pykci.geoid_offset_m, which other tilesets and terrain align to")
    ap.add_argument("--no-lod3", action="store_false", dest="lod3",
                    help="--tiled: draw the authoritative LoD2 shell even where a "
                         "reconstructed LoD3 facade (HAS_LOD3_FACADE) exists")
    args = ap.parse_args()

    # Resolve dataset from graph if not given
    source = args.dataset
    if source is None:
        driver = _connect()
        with driver.session(database="neo4j") as sess:
            sources = list_sources(sess)
        driver.close()
        if not sources:
            print("ERROR: no Dataset nodes found in graph. Run ingest_citygml.py first.",
                  file=sys.stderr)
            sys.exit(1)
        if len(sources) > 1:
            print("Multiple datasets in graph; specify one with --dataset:", file=sys.stderr)
            for s in sources:
                print(f"  {s}", file=sys.stderr)
            sys.exit(1)
        source = sources[0]

    if args.tiled:
        result = build_3dtiles_tiled(source, Path(args.out), color_by=args.color_by,
                                     palette=args.palette, opacity=args.opacity,
                                     max_features=args.max_features,
                                     overview_features=args.overview_features,
                                     attributes=args.attributes == "all",
                                     workers=args.workers, lod3=args.lod3,
                                     geoid_offset=args.geoid_offset)
    else:
        result = build_3dtiles(source, Path(args.out), color_by=args.color_by,
                               palette=args.palette, opacity=args.opacity,
                               geoid_offset=args.geoid_offset)
    if result is None or result[0] is None:
        print("No output generated (no surface polygons found).")
        sys.exit(1)
    print("Done.")
