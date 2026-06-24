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

Output: output/3dtiles/tileset.json + output/3dtiles/tiles/city.glb
"""

import argparse
import json
import math
import re
import struct
import sys
from pathlib import Path

import mapbox_earcut as earcut_lib
import numpy as np
from neo4j import GraphDatabase
from pyproj import Transformer

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


def _geoid_m(srs_name: str, sample_x: float, sample_y: float, sample_z: float) -> float:
    """Estimate the geoid undulation (m) at a sample point in the dataset CRS.

    Attempts a compound-CRS → EPSG:4979 (WGS84 3D ellipsoidal) transformation
    via pyproj. The difference between the transformed ellipsoidal height and
    the input orthometric height gives the geoid undulation.

    Returns 0.0 and prints a note when:
      - no compound CRS mapping is found for the srsName pattern, or
      - PROJ datum-grid files are unavailable.
    """
    compound = None
    for pat, epsg in _ADV_COMPOUND.items():
        if pat.lower() in srs_name.lower():
            compound = epsg
            break
    if compound is None:
        print(f"  Note: no compound CRS mapping for '{srs_name}'; geoid offset = 0.0 m")
        return 0.0
    try:
        t3d = Transformer.from_crs(compound, "EPSG:4979", always_xy=True)
        _, _, h_ell = t3d.transform(sample_x, sample_y, sample_z)
        return h_ell - sample_z
    except Exception as exc:
        print(f"  Note: geoid transform unavailable ({exc}); geoid offset = 0.0 m")
        return 0.0


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

_NEO4J_URI  = "bolt://localhost:7687"
_NEO4J_AUTH = ("neo4j", "neo4jneo4j")


def _connect():
    driver = GraphDatabase.driver(
        _NEO4J_URI, auth=_NEO4J_AUTH,
        notifications_disabled_categories={"DEPRECATION"},
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
             mat_fids=None, n_features=0) -> bytes:
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
    for m in MATERIALS_GLTF:
        gltf_mats.append({
            "name": m["name"],
            "pbrMetallicRoughness": {
                "baseColorFactor": m["color"],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.85,
            },
            "doubleSided": True,
        })

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

def build_3dtiles(source: str, out_dir: Path):
    # ---- 1. Read CRS and bbox from the Dataset node in Neo4j ---------------
    driver = _connect()
    print("Connected to Neo4j.")
    with driver.session(database="neo4j") as sess:
        ds = fetch_dataset(sess, source)
        if not ds:
            print(f"ERROR: no Dataset with source='{source}' found in graph.", file=sys.stderr)
            driver.close()
            sys.exit(1)

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
        geoid_m = _geoid_m(srs_name, mid_x, mid_y, mid_z)
        print(f"  Geoid offset: {geoid_m:+.2f} m")

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
    mat_pos  = [[] for _ in range(_N_MATS)]
    mat_nor  = [[] for _ in range(_N_MATS)]
    mat_idx  = [[] for _ in range(_N_MATS)]
    mat_fids = [[] for _ in range(_N_MATS)]
    building_registry: dict = {}   # building_id str → feature index int
    skipped = 0

    for s in surface_list:
        mi  = FEATURE_MATERIAL.get(s["type"], 0)
        bid = s.get("building_id") or ""
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

    total_verts = sum(len(mat_pos[i]) for i in range(_N_MATS))
    total_tris  = sum(len(mat_idx[i]) // 3 for i in range(_N_MATS))
    print(f"  Triangulated: {total_tris:,} triangles, {total_verts:,} vertices"
          f"  (skipped degenerate polygons: {skipped})")
    for i in range(_N_MATS):
        if mat_pos[i]:
            print(f"    {MATERIALS_GLTF[i]['name']}: "
                  f"{len(mat_idx[i]) // 3:,} tri / {len(mat_pos[i]):,} vert")

    # ---- 5. Pack GLB -------------------------------------------------------
    n_features = len(building_registry)
    print(f"Packing GLB … ({n_features} building features)")
    glb_bytes = pack_glb(mat_pos, mat_nor, mat_idx, mat_fids, n_features)

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

    result = build_3dtiles(source, Path(args.out))
    if result is None or result[0] is None:
        print("No output generated (no surface polygons found).")
        sys.exit(1)
    print("Done.")
