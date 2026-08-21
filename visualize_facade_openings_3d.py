"""
visualize_facade_openings_3d.py

CesiumJS 3-D viewer for the Hamburg LOD2/LOD3 semantic city graph stored in
Neo4j.  Generates a self-contained HTML file that can be opened in any browser
(internet required for the CesiumJS CDN and CartoDB basemap tiles).

What is displayed
-----------------
* 17 LOD3-enriched buildings — rendered from their actual WallSurface and
  RoofSurface polygons (perPositionHeight), coloured uniform gray.
* Window and door openings — rendered as opaque 3-D slabs (front face +
  left/right sides + top face) protruding 18 cm from the wall surface.
  Windows: red  (#eb3333).  Doors: green (#33c833).
* Click any building to open the Building Inspector panel (facade photo with
  overlaid opening bounding boxes, metadata, roof type / material).

Coordinate handling
-------------------
* Source CRS: ETRS89 / UTM32 + DHHN2016 (EPSG:25832 / compound).
* All building heights are normalised to 0 m base (building sits on the flat
  CartoDB basemap) by subtracting each building's ground_z from vertex heights.
* Opening UV coords are projected onto the largest-area WallSurface polygon
  via its true 3-D local frame (Newell normal → h_axis / v_axis), so openings
  lie exactly in the wall plane regardless of facade orientation.

Key functions
-------------
fetch_enriched_buildings()       — 17 LOD3 buildings with metadata
fetch_building_surfaces()        — 286 wall/roof polygon geometries
find_best_wall_frame_per_building() — wall-plane frames for UV projection
fetch_openings_3d()              — 499 opening slabs (back + front corners)

Usage
-----
    python visualize_facade_openings_3d.py
    python visualize_facade_openings_3d.py --output /path/to/viewer.html
    python visualize_facade_openings_3d.py --no-osm-buildings
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import webbrowser
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from neo4j import GraphDatabase
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).parent))
from enrich_facade_openings import parse_mask, parse_gml

# ---------------------------------------------------------------------------
NEO4J_URL      = os.getenv("NEO4J_URL",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

DATA_DIR = Path(
    "/home/luarzou/Projects/aussenraume/cityReconstruction/"
    "imageToLOD3/LoD3-Reconstruction/data"
)
DEFAULT_OUTPUT = Path(__file__).parent / "output" / "html" / "facade_openings_3d.html"

# When the DHHN2016 pyproj grid is absent the transform returns z unchanged
# (offset ≈ 0).  For a Cesium viewer with no terrain provider the raw
# orthometric heights (≈ MSL, 0–50 m for Hamburg) sit correctly on the flat
# basemap.  Adding the true ~42 m undulation would make buildings float above
# the imagery.  So we use 0.0 as the fallback when the grid is unavailable.
_HAMBURG_GEOID_FALLBACK = 0.0

_ADV_COMPOUND = {
    "ETRS89_UTM32": "EPSG:5555",
    "ETRS89_UTM33": "EPSG:5556",
}


def _horiz_epsg(srs_name: str) -> str:
    m = re.search(r"EPSG[:\s]+(\d+)", srs_name)
    if m:
        return f"EPSG:{m.group(1)}"
    m = re.search(r"ETRS89_UTM(\d+)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{25800 + int(m.group(1))}"
    return srs_name


def _geoid_offset(srs_name: str, sx: float, sy: float, sz: float) -> float:
    compound = next(
        (epsg for pat, epsg in _ADV_COMPOUND.items()
         if pat.lower() in srs_name.lower()),
        None,
    )
    if compound is None:
        print(f"  Note: no compound CRS for '{srs_name}'; using fallback {_HAMBURG_GEOID_FALLBACK} m")
        return _HAMBURG_GEOID_FALLBACK
    try:
        t3d = Transformer.from_crs(compound, "EPSG:4979", always_xy=True)
        _, _, h_ell = t3d.transform(sx, sy, sz)
        offset = h_ell - sz
        if abs(offset) < 1.0:
            print(f"  Note: DHHN2016 grid absent (got {offset:.3f} m); "
                  f"using fallback {_HAMBURG_GEOID_FALLBACK} m")
            return _HAMBURG_GEOID_FALLBACK
        return offset
    except Exception as exc:
        print(f"  Note: geoid transform failed ({exc}); "
              f"using fallback {_HAMBURG_GEOID_FALLBACK} m")
        return _HAMBURG_GEOID_FALLBACK


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def fetch_dataset(driver):
    with driver.session() as s:
        # Prefer the dataset that actually holds the LOD3 buildings (the graph
        # may now also contain a separate street-trees dataset with a different
        # CRS / degenerate envelope).
        row = s.run(
            "MATCH (b:Building {has_lod3_data:true})-[:PART_OF]->(d:Dataset) "
            "RETURN d.crs AS crs, d.bbox_min_x AS x, "
            "d.bbox_min_y AS y, d.bbox_min_z AS z LIMIT 1"
        ).single()
        if row is None:
            row = s.run(
                "MATCH (d:Dataset) RETURN d.crs AS crs, d.bbox_min_x AS x, "
                "d.bbox_min_y AS y, d.bbox_min_z AS z LIMIT 1"
            ).single()
    crs = (row["crs"] if row else None) or "EPSG:25832"
    sx  = float(row["x"] or 0) if row else 0.0
    sy  = float(row["y"] or 0) if row else 0.0
    sz  = float(row["z"] or 0) if row else 0.0
    print(f"  CRS: {crs}")
    return crs, sx, sy, sz


def _pos_list_to_wgs84(pos_list: str, t32_wgs: Transformer,
                        geoid: float, z_offset: float = 0.0) -> list[list]:
    """Parse a GML posList (x y z ...) → [[lon, lat, h+geoid+z_offset], ...]"""
    vals = list(map(float, pos_list.split()))
    pts = []
    for i in range(0, len(vals) - 2, 3):
        lon, lat = t32_wgs.transform(vals[i], vals[i + 1])
        h = vals[i + 2] + geoid + z_offset
        pts.append([round(lon, 7), round(lat, 7), round(h, 2)])
    return pts


def fetch_enriched_buildings(driver, t32_wgs: Transformer,
                              geoid: float) -> list[dict]:
    """One dict per enriched building.  Uses aggregation to avoid duplicate rows."""
    with driver.session() as s:
        rows = s.run("""
            MATCH (b:Building) WHERE b.has_lod3_data = true
            OPTIONAL MATCH (b)-[:HAS_ROOF_MATERIAL]->(rm:RoofMaterial)
            OPTIONAL MATCH (b)-[:HAS_ROOF_TYPE]->(rt:RoofType)
            WITH b,
                 collect(DISTINCT rm.name)[0] AS material,
                 head(collect(DISTINCT rt.name)) AS roof_type
            OPTIONAL MATCH (b)-[:HAS_BOUNDARY]->
                           (gs:BoundarySurface {surface_type:'GroundSurface'})
                           -[:HAS_POLYGON]->(gp:GeometryPolygon)
                           -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
            WITH b, material, roof_type, collect(gr.pos_list)[0] AS pos_list
            RETURN b.id                                         AS id,
                   coalesce(material, b.predictedroofmaterial)  AS material,
                   toFloat(coalesce(b.measured_height, 8.0))   AS height,
                   toFloat(coalesce(b.ground_z, 0.0))          AS ground_z,
                   b.storeys_above_ground                      AS storeys,
                   toInteger(coalesce(b.lod3_windows, 0))      AS windows,
                   toInteger(coalesce(b.lod3_doors,   0))      AS doors,
                   b.lod3_source                               AS source,
                   toFloat(b.center_x)                        AS cx,
                   toFloat(b.center_y)                        AS cy,
                   toFloat(b.bbox_min_x)                      AS bbox_min_x,
                   toFloat(b.bbox_max_x)                      AS bbox_max_x,
                   roof_type, pos_list
            ORDER BY windows DESC
        """).data()

    buildings = []
    for r in rows:
        height   = float(r["height"])
        ground_z = float(r["ground_z"])
        cx_utm   = float(r["cx"] or 0)
        cy_utm   = float(r["cy"] or 0)
        lon_c, lat_c = t32_wgs.transform(cx_utm, cy_utm)

        footprint = []
        if r["pos_list"]:
            pts = _pos_list_to_wgs84(r["pos_list"], t32_wgs, geoid)
            footprint = [[p[0], p[1]] for p in pts]   # 2D only for footprint

        buildings.append({
            "id":        r["id"],
            "lon":       round(lon_c, 7),
            "lat":       round(lat_c, 7),
            "height":    round(height, 2),
            "ground_z":  0.0,                         # building base at basemap surface
            "ground_z_raw": round(ground_z + geoid, 2),  # raw elevation, used for z-offset
            "windows":   int(r["windows"]),
            "doors":     int(r["doors"]),
            "storeys":   r["storeys"],
            "roof_type": r["roof_type"] or "—",
            "roof_mats": [r["material"]] if r["material"] else [],
            "source":    r["source"] or "",
            "footprint": footprint,
            "bbox_min_x": float(r["bbox_min_x"] or 0),
            "bbox_max_x": float(r["bbox_max_x"] or 0),
            "cy_utm":     cy_utm,
        })
    return buildings


def fetch_building_surfaces(driver, t32_wgs: Transformer,
                             geoid: float,
                             ground_z_by_bid: dict) -> list[dict]:
    """
    Return all WallSurface and RoofSurface polygons for the 17 enriched
    buildings, with coordinates converted to WGS84 + ellipsoidal height.
    ground_z_by_bid offsets each building's vertices so the base sits at 0 m
    (on the flat basemap surface) rather than at its raw MSL elevation.
    Each entry: {bid, stype, pts: [[lon, lat, h], ...]}
    """
    with driver.session() as s:
        rows = s.run("""
            MATCH (b:Building) WHERE b.has_lod3_data = true
            MATCH (b)-[:HAS_BOUNDARY]->(bs:BoundarySurface)
            WHERE bs.surface_type IN ['WallSurface', 'RoofSurface']
            MATCH (bs)-[:HAS_POLYGON]->(gp:GeometryPolygon)
                  -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
            RETURN b.id AS bid, bs.surface_type AS stype, gr.pos_list AS pos_list
            ORDER BY bid, stype
        """).data()

    surfaces = []
    for r in rows:
        if not r["pos_list"]:
            continue
        gz = ground_z_by_bid.get(r["bid"], 0.0)
        pts = _pos_list_to_wgs84(r["pos_list"], t32_wgs, geoid, z_offset=-gz)
        if len(pts) >= 3:
            surfaces.append({
                "bid":   r["bid"],
                "stype": r["stype"],
                "pts":   pts,
            })
    return surfaces


def _wall_frame(pos_list: str) -> dict | None:
    """
    Compute the local coordinate frame of a wall surface polygon (UTM coords).

    Returns a dict:
      centroid – (x, y, z) centre of mass
      h_axis   – unit vector along the wall's horizontal direction
      v_axis   – unit vector along the wall's vertical direction (z-ward)
      u_min/max, v_min/max – extent of the polygon in the local frame (metres)
      area     – projected area in the local frame (m²), used to rank walls
    Returns None for degenerate or near-horizontal polygons.
    """
    vals = list(map(float, pos_list.split()))
    if len(vals) < 9:
        return None
    pts = np.array([[vals[i], vals[i + 1], vals[i + 2]]
                    for i in range(0, len(vals) - 2, 3)], dtype=float)
    centroid = pts.mean(axis=0)

    # Surface normal via Newell's method (robust for any polygon)
    n = len(pts)
    normal = np.zeros(3)
    for i in range(n):
        c, nxt = pts[i], pts[(i + 1) % n]
        normal[0] += (c[1] - nxt[1]) * (c[2] + nxt[2])
        normal[1] += (c[2] - nxt[2]) * (c[0] + nxt[0])
        normal[2] += (c[0] - nxt[0]) * (c[1] + nxt[1])
    nlen = np.linalg.norm(normal)
    if nlen < 1e-6:
        return None
    normal /= nlen

    # Skip polygons tilted more than ~45° from vertical (roofs, etc.)
    if abs(normal[2]) > 0.7:
        return None

    # Horizontal wall axis: perpendicular to the normal and to the z-up vector
    z_up = np.array([0.0, 0.0, 1.0])
    h_axis = np.cross(z_up, normal)
    hlen = np.linalg.norm(h_axis)
    if hlen < 1e-6:
        return None
    h_axis /= hlen

    # Vertical wall axis: in the wall plane, pointing upward
    v_axis = np.cross(normal, h_axis)
    vlen = np.linalg.norm(v_axis)
    if vlen < 1e-6:
        return None
    v_axis /= vlen
    if v_axis[2] < 0:
        v_axis = -v_axis

    # Local bounding box
    rel = pts - centroid
    us = rel @ h_axis
    vs = rel @ v_axis
    u_min, u_max = float(us.min()), float(us.max())
    v_min, v_max = float(vs.min()), float(vs.max())

    return {
        "centroid": centroid,
        "normal":   normal,
        "h_axis":   h_axis,
        "v_axis":   v_axis,
        "u_min": u_min, "u_max": u_max,
        "v_min": v_min, "v_max": v_max,
        "area":  (u_max - u_min) * (v_max - v_min),
    }


def find_best_wall_frame_per_building(driver) -> dict:
    """
    For each enriched building, return the local coordinate frame of the
    WallSurface with the largest projected area — best proxy for the main
    photographed façade.  Keyed by building ID.
    """
    with driver.session() as s:
        rows = s.run("""
            MATCH (b:Building) WHERE b.has_lod3_data = true
            MATCH (b)-[:HAS_BOUNDARY]->(ws:BoundarySurface {surface_type:'WallSurface'})
            MATCH (ws)-[:HAS_POLYGON]->(gp:GeometryPolygon)
                  -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
            RETURN b.id AS bid, gr.pos_list AS pos_list
        """).data()
    best: dict = {}
    for r in rows:
        if not r["pos_list"]:
            continue
        frame = _wall_frame(r["pos_list"])
        if frame is None:
            continue
        bid = r["bid"]
        if bid not in best or frame["area"] > best[bid]["area"]:
            best[bid] = frame
    return best


def fetch_openings_3d(driver, buildings: list[dict],
                      t32_wgs: Transformer, geoid: float,
                      wall_frames: dict) -> list[dict]:
    """
    Query Opening nodes from Neo4j and compute 4 WGS84 corner positions for
    each opening rectangle by projecting UV onto the wall's actual 3D plane.

    For each building the largest-area WallSurface provides a local coordinate
    frame (h_axis = horizontal along the wall, v_axis = vertical along the
    wall).  UV [0,1]² maps onto the wall's bounding rectangle in that frame,
    so the resulting corners lie exactly in the wall plane — regardless of the
    wall's orientation in the horizontal plane.

    Heights are 0-based (building base normalised to basemap surface).
    """
    with driver.session() as s:
        rows = s.run("""
            MATCH (b:Building) WHERE b.has_lod3_data = true
            MATCH (b)-[:HAS_FACADE_OPENING]->(o:Opening)
            RETURN b.id AS bid,
                   o.type          AS type,
                   toFloat(o.u0)   AS u0, toFloat(o.u1) AS u1,
                   toFloat(o.v0)   AS v0, toFloat(o.v1) AS v1
            ORDER BY bid, o.v0, o.u0
        """).data()

    bldg = {b["id"]: b for b in buildings}
    result = []
    for r in rows:
        b = bldg.get(r["bid"])
        if not b:
            continue

        _BACK_M  = 0.02   # m — tiny offset so back face clears the wall surface
        _DEPTH_M = 0.18   # m — slab thickness (front − back)

        frame = wall_frames.get(r["bid"])
        if frame is not None:
            c   = frame["centroid"]
            n   = frame["normal"]
            h   = frame["h_axis"]
            v   = frame["v_axis"]
            u_w = frame["u_max"] - frame["u_min"]
            v_h = frame["v_max"] - frame["v_min"]
            if u_w < 0.1 or v_h < 0.1:
                continue
            gz = b["ground_z_raw"]

            def corner(u_frac, v_frac, extrude):
                lu = frame["u_min"] + u_frac * u_w
                lv = frame["v_min"] + v_frac * v_h
                pt = c + lu * h + lv * v + extrude * n
                lon, lat = t32_wgs.transform(float(pt[0]), float(pt[1]))
                ht = float(pt[2]) - gz
                return [round(lon, 7), round(lat, 7), round(ht, 2)]

            u0, u1, v0, v1 = r["u0"], r["u1"], r["v0"], r["v1"]
            result.append({
                "bid":  r["bid"],
                "type": r["type"],
                # back: flush against wall  [BL, BR, TR, TL]
                "back": [
                    corner(u0, v0, _BACK_M),
                    corner(u1, v0, _BACK_M),
                    corner(u1, v1, _BACK_M),
                    corner(u0, v1, _BACK_M),
                ],
                # front: extruded outward  [BL, BR, TR, TL]
                "front": [
                    corner(u0, v0, _BACK_M + _DEPTH_M),
                    corner(u1, v0, _BACK_M + _DEPTH_M),
                    corner(u1, v1, _BACK_M + _DEPTH_M),
                    corner(u0, v1, _BACK_M + _DEPTH_M),
                ],
            })
        else:
            # Fallback: axis-aligned bbox projection (flat slab)
            w = b["bbox_max_x"] - b["bbox_min_x"]
            if w <= 0:
                continue
            x0_utm = b["bbox_min_x"] + r["u0"] * w
            x1_utm = b["bbox_min_x"] + r["u1"] * w
            cy_utm = b["cy_utm"]
            z0 = r["v0"] * b["height"]
            z1 = r["v1"] * b["height"]
            lon0, lat = t32_wgs.transform(x0_utm, cy_utm)
            lon1, _   = t32_wgs.transform(x1_utm, cy_utm)
            flat = [
                [round(lon0, 7), round(lat, 7), round(z0, 2)],
                [round(lon1, 7), round(lat, 7), round(z0, 2)],
                [round(lon1, 7), round(lat, 7), round(z1, 2)],
                [round(lon0, 7), round(lat, 7), round(z1, 2)],
            ]
            result.append({"bid": r["bid"], "type": r["type"],
                           "back": flat, "front": flat})
    return result


def fetch_context_buildings(driver, t32_wgs: Transformer, geoid: float,
                             center_lon: float, center_lat: float,
                             radius_km: float = 1.5) -> list[list]:
    """
    Return [lon_w, lat_s, lon_e, lat_n, height] for every non-enriched building
    within radius_km.  Used to render batched extruded rectangles in Cesium.
    """
    wgs_utm = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    cx, cy = wgs_utm.transform(center_lon, center_lat)
    r_m = radius_km * 1000
    with driver.session() as s:
        rows = s.run("""
            MATCH (b:Building)
            WHERE b.has_lod3_data IS NULL
              AND b.center_x >= $x0 AND b.center_x <= $x1
              AND b.center_y >= $y0 AND b.center_y <= $y1
            RETURN toFloat(b.bbox_min_x) AS x0,
                   toFloat(b.bbox_min_y) AS y0,
                   toFloat(b.bbox_max_x) AS x1,
                   toFloat(b.bbox_max_y) AS y1,
                   toFloat(coalesce(b.measured_height, 5.0)) AS mh
            LIMIT 30000
        """, x0=cx - r_m, x1=cx + r_m, y0=cy - r_m, y1=cy + r_m).data()
    out = []
    for r in rows:
        try:
            if None in (r["x0"], r["y0"], r["x1"], r["y1"]):
                continue
            lon_w, lat_s = t32_wgs.transform(float(r["x0"]), float(r["y0"]))
            lon_e, lat_n = t32_wgs.transform(float(r["x1"]), float(r["y1"]))
            mh = max(1.0, float(r["mh"] or 5.0))
            out.append([round(lon_w, 6), round(lat_s, 6),
                        round(lon_e, 6), round(lat_n, 6),
                        round(mh, 1)])
        except Exception:
            pass
    return out


def fetch_street_trees(driver, buildings: list[dict],
                        t32_wgs: Transformer,
                        radius_m: float = 80.0) -> list[dict]:
    """
    Street trees (veg:SolitaryVegetationObject) within `radius_m` of any
    enriched building.  Uses the spatial R-tree via WKT `spatial.intersects`
    (the map-coordinate form of spatial.bbox/withinDistance throws on this
    plugin build).  Returns one dict per distinct tree with a simple
    trunk+crown glyph model.
    """
    # one buffered WKT box per enriched building; union the tree hits in Python
    by_id: dict[str, dict] = {}
    with driver.session() as s:
        for b in buildings:
            x0 = b["bbox_min_x"] - radius_m
            x1 = b["bbox_max_x"] + radius_m
            # bbox_min/max_y aren't carried on the building dict; derive from cy
            cy = b["cy_utm"]
            y0 = cy - (radius_m + 60.0)
            y1 = cy + (radius_m + 60.0)
            wkt = (f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, "
                   f"{x0} {y1}, {x0} {y0}))")
            rows = s.run("""
                CALL spatial.intersects('features', $wkt) YIELD node
                WHERE node:SolitaryVegetationObject
                RETURN node.id            AS id,
                       toFloat(node.center_x) AS x,
                       toFloat(node.center_y) AS y,
                       node.gattung       AS gattung,
                       node.baumart       AS baumart,
                       node.stra_e        AS strasse,
                       node.pflanzjahr    AS pflanzjahr,
                       toFloat(node.hoehe_aus_als)     AS height,
                       toFloat(node.kronendurchmesser) AS crown_d
            """, wkt=wkt).data()
            b["tree_count"] = len(rows)
            for r in rows:
                if r["id"] in by_id or r["x"] is None or r["y"] is None:
                    continue
                lon, lat = t32_wgs.transform(r["x"], r["y"])
                height  = float(r["height"]) if r["height"] else 8.0
                height  = max(2.0, min(height, 45.0))
                crown_d = float(r["crown_d"]) if r["crown_d"] else 5.0
                crown_d = max(1.5, min(crown_d, 25.0))
                crown_r = crown_d / 2.0
                trunk_h = max(1.5, min(0.35 * height, height - 1.0))
                crown_h = height - trunk_h          # vertical crown extent
                by_id[r["id"]] = {
                    "id":      r["id"],
                    "lon":     round(lon, 7),
                    "lat":     round(lat, 7),
                    "trunk_h": round(trunk_h, 2),
                    "crown_r": round(crown_r, 2),
                    "crown_h": round(crown_h, 2),
                    "height":  round(height, 1),
                    "gattung": r["gattung"] or "—",
                    "baumart": r["baumart"] or "—",
                    "strasse": r["strasse"] or "",
                    "planted": r["pflanzjahr"] or "—",
                }
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def load_and_encode(img_path: Path, max_px: int = 800) -> tuple[str, int, int]:
    im = Image.open(img_path).convert("RGB")
    ow, oh = im.size
    scale = min(max_px / max(ow, oh), 1.0)
    if scale < 1.0:
        im = im.resize((max(1, int(ow * scale)), max(1, int(oh * scale))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=78, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), ow, oh


def make_facade_panel_data(gml_files: list[Path], max_px: int) -> dict:
    result: dict[str, dict] = {}
    for gml_path in gml_files:
        n = gml_path.stem
        jpg_path  = DATA_DIR / f"{n}.jpg"
        mask_path = DATA_DIR / f"{n}_mask.png"
        if not jpg_path.exists() or not mask_path.exists():
            continue
        try:
            gml_data  = parse_gml(str(gml_path))
            mask_data = parse_mask(str(mask_path))
            b64, ow, oh = load_and_encode(jpg_path, max_px)
        except Exception as exc:
            print(f"  [skip] {n}: {exc}")
            continue
        bid   = gml_data["building_id"]
        entry = result.setdefault(bid, {
            "b64": b64, "orig_w": ow, "orig_h": oh,
            "openings": [], "n_windows": 0, "n_doors": 0,
        })
        total_new = mask_data["n_windows"] + mask_data["n_doors"]
        if total_new > entry["n_windows"] + entry["n_doors"]:
            entry.update({
                "b64": b64, "orig_w": ow, "orig_h": oh,
                "openings":  mask_data["openings"],
                "n_windows": mask_data["n_windows"],
                "n_doors":   mask_data["n_doors"],
            })
        print(f"  [{n:>3}] {bid}  {mask_data['n_windows']}W {mask_data['n_doors']}D")
    return result


# ---------------------------------------------------------------------------
# Color scale: window count → CSS hex
# ---------------------------------------------------------------------------
_WIN_SCALE = [
    (0,  "#4fc3f7"),
    (10, "#66bb6a"),
    (25, "#ffa726"),
    (50, "#ef5350"),
]

def _win_color(n: int) -> str:
    stops = _WIN_SCALE
    if n <= stops[0][0]:  return stops[0][1]
    if n >= stops[-1][0]: return stops[-1][1]
    for i in range(len(stops) - 1):
        lo_v, lo_c = stops[i]; hi_v, hi_c = stops[i + 1]
        if lo_v <= n <= hi_v:
            t = (n - lo_v) / (hi_v - lo_v)
            def h2r(h): return tuple(int(h[k:k+2], 16) for k in (1, 3, 5))
            lr, lg, lb = h2r(lo_c); hr, hg, hb = h2r(hi_c)
            return "#{:02x}{:02x}{:02x}".format(
                int(lr + t*(hr-lr)), int(lg + t*(hg-lg)), int(lb + t*(hb-lb)))
    return stops[-1][1]


# ---------------------------------------------------------------------------
# HTML / JS
# ---------------------------------------------------------------------------

def build_html(buildings, surfaces, openings_3d, panel_data,
               context_bldgs, trees, osm_buildings: bool,
               initial_view: str = "building") -> str:

    for b in buildings:
        b["color"] = "#9e9e9e"   # uniform gray

    n_bldg  = len(buildings)
    total_w = sum(b["windows"] for b in buildings)
    total_d = sum(b["doors"]   for b in buildings)
    n_trees = len(trees)

    # Strip Python-only fields before embedding in JS
    _strip = {"cy_utm", "bbox_min_x", "bbox_max_x", "ground_z_raw"}
    bldg_js = []
    for b in buildings:
        bldg_js.append({k: v for k, v in b.items() if k not in _strip})

    panel_json    = json.dumps({
        bid: {"b64": d["b64"], "orig_w": d["orig_w"], "orig_h": d["orig_h"],
              "openings": d["openings"]}
        for bid, d in panel_data.items()
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Facade Openings 3D — Hamburg</title>
<script>window.CESIUM_BASE_URL='https://cdn.jsdelivr.net/npm/cesium@1.116.0/Build/Cesium/';</script>
<script src="https://cdn.jsdelivr.net/npm/cesium@1.116.0/Build/Cesium/Cesium.js"></script>
<link  href="https://cdn.jsdelivr.net/npm/cesium@1.116.0/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:100%;height:100%;overflow:hidden;
           font-family:system-ui,sans-serif;font-size:13px;background:#12121e}}
#cesiumContainer{{width:100%;height:100%}}
.cesium-credit-container,.cesium-widget-credits{{display:none!important}}
#topbar{{
  position:absolute;top:0;left:0;right:0;height:40px;z-index:200;
  background:rgba(18,28,50,.92);backdrop-filter:blur(6px);
  border-bottom:1px solid #1e3a5f;
  display:flex;align-items:center;gap:8px;padding:0 14px;
}}
#topbar h1{{font-size:13px;font-weight:600;color:#90caf9;margin-right:auto;white-space:nowrap}}
.chip{{background:#1e3a5f;border-radius:12px;padding:2px 10px;
       font-size:11px;color:#90caf9;white-space:nowrap}}
.btn{{background:#1565c0;border:none;border-radius:4px;color:#fff;
      padding:3px 9px;cursor:pointer;font-size:11px;white-space:nowrap}}
.btn:hover{{background:#1976d2}} .btn.off{{background:#37474f}}
#right-panel{{
  position:absolute;top:40px;right:0;bottom:0;width:320px;z-index:200;
  background:rgba(14,20,38,.93);backdrop-filter:blur(6px);
  border-left:1px solid #1e3a5f;
  display:flex;flex-direction:column;overflow:hidden;transition:width .2s;
}}
#right-panel.hidden{{width:0;border:none}}
.panel-hdr{{font-size:10px;font-weight:600;color:#546e7a;
            padding:7px 12px;border-bottom:1px solid #1e3a5f;
            text-transform:uppercase;letter-spacing:.5px;flex-shrink:0}}
#info-scroll{{overflow-y:auto;flex:1;padding:10px 12px}}
.prop{{margin-bottom:8px}}
.prop-key{{font-size:10px;color:#546e7a;text-transform:uppercase;letter-spacing:.4px}}
.prop-val{{font-size:12px;color:#e0e0e0;margin-top:1px}}
.empty{{color:#37474f;font-size:12px;padding:12px 0}}
.facade-wrap{{position:relative;width:100%;background:#000;margin:8px 0}}
.facade-wrap img{{width:100%;height:auto;display:block}}
.facade-wrap svg{{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}}
.legend-scale{{display:flex;align-items:center;gap:4px;flex-wrap:wrap;
               font-size:10px;color:#aaa;padding:5px 0}}
#legend{{
  position:absolute;bottom:16px;left:16px;z-index:200;
  background:rgba(14,20,38,.88);border:1px solid #1e3a5f;
  border-radius:6px;padding:8px 12px;font-size:11px;color:#90caf9
}}
#legend .title{{font-size:10px;font-weight:600;color:#546e7a;
                text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px}}
#legend .row{{display:flex;align-items:center;gap:5px;margin-top:4px}}
#legend .dot{{width:10px;height:10px;border-radius:2px;display:inline-block}}
</style>
</head>
<body>
<div id="cesiumContainer"></div>
<div id="topbar">
  <h1>Facade Openings 3D — Hamburg LoD3</h1>
  <span class="chip">🏘 {n_bldg} enriched</span>
  <span class="chip">🪟 {total_w} windows</span>
  <span class="chip">🚪 {total_d} doors</span>
  <span class="chip">🌳 {n_trees} trees</span>
  <select id="bldg-sel" title="Fly to enriched building"
    style="background:#1e3a5f;color:#cfe8ff;border:1px solid #2e5a8f;border-radius:4px;
           font-size:11px;padding:2px 4px;max-width:230px"></select>
  <button class="btn off" id="btn-osm">OSM Bldgs</button>
  <button class="btn"     id="btn-op">Openings ON</button>
  <button class="btn"     id="btn-tree">Trees ON</button>
  <button class="btn"     id="btn-bm">🌙 Dark</button>
  <button class="btn"     id="btn-panel">Panel ▶</button>
</div>
<div id="legend">
  <div class="title">Roof material</div>
  <div class="row"><span class="dot" style="background:#b5451b"></span><span style="font-size:10px;color:#aaa">Roof tiles</span></div>
  <div class="row"><span class="dot" style="background:#37474f"></span><span style="font-size:10px;color:#aaa">Tar paper</span></div>
  <div class="row"><span class="dot" style="background:#90a4ae"></span><span style="font-size:10px;color:#aaa">Concrete</span></div>
  <div class="row"><span class="dot" style="background:#78909c"></span><span style="font-size:10px;color:#aaa">Metal</span></div>
  <div class="row"><span class="dot" style="background:#4fc3f7"></span><span style="font-size:10px;color:#aaa">Glass</span></div>
  <div class="row" style="margin-top:6px">
    <span class="dot" style="background:rgba(235,50,50,1);border-radius:1px"></span>
    <span style="font-size:10px;color:#aaa">Window</span>
  </div>
  <div class="row">
    <span class="dot" style="background:rgba(50,200,50,1);border-radius:1px"></span>
    <span style="font-size:10px;color:#aaa">Door</span>
  </div>
  <div class="row" style="margin-top:6px">
    <span class="dot" style="background:#2e7d32;border-radius:50%"></span>
    <span style="font-size:10px;color:#aaa">Street tree</span>
  </div>
</div>
<div id="right-panel">
  <div class="panel-hdr">Building Inspector</div>
  <div id="info-scroll"><div class="empty">Click an enriched building</div></div>
</div>

<script>
const BUILDINGS   = {json.dumps(bldg_js, ensure_ascii=False)};
const SURFACES    = {json.dumps(surfaces,    ensure_ascii=False)};
const OPENINGS3D  = {json.dumps(openings_3d, ensure_ascii=False)};
const TREES       = {json.dumps(trees, ensure_ascii=False)};
const PANEL_DATA  = {panel_json};
const USE_OSM_DEFAULT = {json.dumps(osm_buildings)};

/* ── Cesium init (1.116.0 compatible) ─────────────────────────── */
const viewer = new Cesium.Viewer('cesiumContainer', {{
  baseLayerPicker: false, geocoder: false, homeButton: false,
  sceneModePicker: false, navigationHelpButton: false,
  animation: false, timeline: false, infoBox: false, selectionIndicator: false,
}});
viewer.scene.globe.enableLighting = false;
viewer.scene.fog.enabled = false;
viewer.imageryLayers.removeAll();

const BASEMAPS = {{
  light: 'https://basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png',
  dark:  'https://basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}.png',
}};
let currentBasemap = 'light';
function setBasemap(name) {{
  currentBasemap = name;
  viewer.imageryLayers.removeAll();
  viewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({{
    url: BASEMAPS[name],
    credit: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © CARTO',
    maximumLevel: 19,
  }}));
  const btn = document.getElementById('btn-bm');
  btn.textContent = name === 'light' ? '🌙 Dark' : '☀ Light';
}}
setBasemap('light');
document.getElementById('btn-bm').addEventListener('click', () =>
  setBasemap(currentBasemap === 'light' ? 'dark' : 'light'));

/* ── OSM 3D Buildings ────────────────────────────────────────────*/
let osmTileset = null, osmVisible = false;
async function toggleOsm(on) {{
  osmVisible = on;
  const btn = document.getElementById('btn-osm');
  btn.textContent = on ? 'OSM Bldgs ON' : 'OSM Bldgs';
  btn.classList.toggle('off', !on);
  if (on && !osmTileset) {{
    try {{
      osmTileset = await Cesium.createOsmBuildingsAsync();
      viewer.scene.primitives.add(osmTileset);
      osmTileset.style = new Cesium.Cesium3DTileStyle({{
        color: "color('rgb(45,55,80)', 0.5)",
      }});
    }} catch(e) {{ console.warn('OSM Buildings unavailable:', e); }}
  }}
  if (osmTileset) osmTileset.show = on;
}}
document.getElementById('btn-osm').addEventListener('click',
  () => toggleOsm(!osmVisible));
if (USE_OSM_DEFAULT) toggleOsm(true);


/* ── Roof material colours ───────────────────────────────────────*/
const ROOF_MAT_COLORS = {{
  'roof_tiles': '#b5451b',   // terracotta red
  'tar_paper':  '#37474f',   // dark slate
  'concrete':   '#90a4ae',   // cool grey
  'metal':      '#78909c',   // metallic blue-grey
  'glass':      '#4fc3f7',   // sky blue
}};
const WALL_COLOR = '#9e9e9e';
const ROOF_FALLBACK = '#b0bec5';

const roofMatByBid = {{}};
BUILDINGS.forEach(b => {{
  if (b.roof_mats && b.roof_mats.length > 0)
    roofMatByBid[b.id] = b.roof_mats[0];
}});

function surfaceColor(bid, stype, alpha) {{
  if (stype === 'WallSurface')
    return Cesium.Color.fromCssColorString(WALL_COLOR).withAlpha(alpha ?? 0.78);
  const hex = ROOF_MAT_COLORS[roofMatByBid[bid]] ?? ROOF_FALLBACK;
  return Cesium.Color.fromCssColorString(hex).withAlpha(alpha ?? 0.82);
}}

/* ── LOD2 surface geometry (wall + roof) ─────────────────────────*/
const surfaceEntities = [];
SURFACES.forEach(s => {{
  const flat = s.pts.flatMap(([lo, la, h]) => [lo, la, h]);
  const isWall = s.stype === 'WallSurface';
  const col = surfaceColor(s.bid, s.stype);
  const ent = viewer.entities.add({{
    polygon: {{
      hierarchy: new Cesium.PolygonHierarchy(
        Cesium.Cartesian3.fromDegreesArrayHeights(flat)
      ),
      perPositionHeight: true,
      material:    col,
      outline:     true,
      outlineColor: Cesium.Color.WHITE.withAlpha(isWall ? 0.4 : 0.2),
      outlineWidth: 1.5,
      arcType:     Cesium.ArcType.NONE,
    }},
    _bid: s.bid,
    _isSurface: true,
  }});
  surfaceEntities.push({{ ent, bid: s.bid, stype: s.stype }});
}});

/* ── Opening boxes (front + left + right + top faces) ── */
// back/front each: [BL, BR, TR, TL]  (v0=bottom, v1=top)
// front face  : wall  BL_f→BR_f  min=[BL_f.z, BR_f.z]  max=[TL_f.z, TR_f.z]
// left side   : wall  BL_b→BL_f  min=[BL_b.z, BL_f.z]  max=[TL_b.z, TL_f.z]
// right side  : wall  BR_b→BR_f  min=[BR_b.z, BR_f.z]  max=[TR_b.z, TR_f.z]
// top face    : polygon  TL_b, TR_b, TR_f, TL_f  (all same z → triangulates fine)
const openingEntities = [];
let openingsVisible = true;
OPENINGS3D.forEach(op => {{
  const isWin = op.type === 'Window';
  const rgba = isWin ? new Cesium.Color(0.92, 0.20, 0.20, 1.0)
                     : new Cesium.Color(0.20, 0.78, 0.20, 1.0);
  const mat  = new Cesium.ColorMaterialProperty(rgba);
  const b = op.back,  f = op.front;   // [BL, BR, TR, TL]

  // front face
  openingEntities.push(viewer.entities.add({{
    wall: {{
      positions:      Cesium.Cartesian3.fromDegreesArrayHeights([
                        f[0][0],f[0][1],f[0][2], f[1][0],f[1][1],f[1][2]]),
      minimumHeights: [f[0][2], f[1][2]],
      maximumHeights: [f[3][2], f[2][2]],
      material: mat, outline: false,
    }}, _bid: op.bid,
  }}));

  // left side  (BL_back → BL_front, full height)
  openingEntities.push(viewer.entities.add({{
    wall: {{
      positions:      Cesium.Cartesian3.fromDegreesArrayHeights([
                        b[0][0],b[0][1],b[0][2], f[0][0],f[0][1],f[0][2]]),
      minimumHeights: [b[0][2], f[0][2]],
      maximumHeights: [b[3][2], f[3][2]],
      material: mat, outline: false,
    }}, _bid: op.bid,
  }}));

  // right side  (BR_back → BR_front, full height)
  openingEntities.push(viewer.entities.add({{
    wall: {{
      positions:      Cesium.Cartesian3.fromDegreesArrayHeights([
                        b[1][0],b[1][1],b[1][2], f[1][0],f[1][1],f[1][2]]),
      minimumHeights: [b[1][2], f[1][2]],
      maximumHeights: [b[2][2], f[2][2]],
      material: mat, outline: false,
    }}, _bid: op.bid,
  }}));

  // top face  (all corners at v1 height → horizontal polygon triangulates fine)
  openingEntities.push(viewer.entities.add({{
    polygon: {{
      hierarchy: new Cesium.PolygonHierarchy(
        Cesium.Cartesian3.fromDegreesArrayHeights([
          b[3][0],b[3][1],b[3][2], b[2][0],b[2][1],b[2][2],
          f[2][0],f[2][1],f[2][2], f[3][0],f[3][1],f[3][2],
        ])),
      perPositionHeight: true,
      arcType:  Cesium.ArcType.NONE,
      material: mat,
    }}, _bid: op.bid,
  }}));
}});
const btnOp = document.getElementById('btn-op');
btnOp.classList.remove('off');
btnOp.textContent = 'Openings ON';
document.getElementById('btn-op').addEventListener('click', e => {{
  openingsVisible = !openingsVisible;
  openingEntities.forEach(e => {{ e.show = openingsVisible; }});
  e.target.classList.toggle('off', !openingsVisible);
  e.target.textContent = openingsVisible ? 'Openings ON' : 'Openings';
}});

/* ── Street trees (trunk cylinder + crown ellipsoid) ─────────────*/
const TRUNK_COLOR = Cesium.Color.fromCssColorString('#6d4c41');
const CROWN_COLOR = Cesium.Color.fromCssColorString('#2e7d32').withAlpha(0.9);
const treeEntities = [];
let treesVisible = true;
TREES.forEach(t => {{
  const crownVR = Math.max(0.5, t.crown_h / 2.0);   // vertical radius
  // trunk: centred at trunk_h/2
  treeEntities.push(viewer.entities.add({{
    position: Cesium.Cartesian3.fromDegrees(t.lon, t.lat, t.trunk_h / 2.0),
    cylinder: {{
      length: t.trunk_h,
      topRadius: 0.18, bottomRadius: 0.25,
      material: TRUNK_COLOR, outline: false,
    }},
    _treeData: t,
  }}));
  // crown: ellipsoid centred at trunk_h + crownVR
  treeEntities.push(viewer.entities.add({{
    position: Cesium.Cartesian3.fromDegrees(t.lon, t.lat, t.trunk_h + crownVR),
    ellipsoid: {{
      radii: new Cesium.Cartesian3(t.crown_r, t.crown_r, crownVR),
      material: CROWN_COLOR,
      outline: false,
      slicePartitions: 12, stackPartitions: 8,
    }},
    _treeData: t,
  }}));
}});
document.getElementById('btn-tree').addEventListener('click', e => {{
  treesVisible = !treesVisible;
  treeEntities.forEach(en => {{ en.show = treesVisible; }});
  e.target.classList.toggle('off', !treesVisible);
  e.target.textContent = treesVisible ? 'Trees ON' : 'Trees';
}});

function renderTreePanel(t) {{
  let h = '';
  h += `<div class="prop"><div class="prop-key">Street tree</div>
    <div class="prop-val" style="font-family:monospace;font-size:10px">${{t.id}}</div></div>`;
  h += `<div class="prop"><div class="prop-key">Genus / Species</div>
    <div class="prop-val">${{t.gattung}}<br><span style="color:#90a4ae">${{t.baumart}}</span></div></div>`;
  if (t.strasse) h += `<div class="prop"><div class="prop-key">Location</div>
    <div class="prop-val">${{t.strasse}}</div></div>`;
  h += `<div class="prop"><div class="prop-key">Height / Crown</div>
    <div class="prop-val">${{t.height}} m tall &nbsp;/&nbsp; ${{(t.crown_r*2).toFixed(1)}} m crown Ø</div></div>`;
  h += `<div class="prop"><div class="prop-key">Planted</div>
    <div class="prop-val">${{t.planted}}</div></div>`;
  document.getElementById('info-scroll').innerHTML = h;
}}

/* ── Entity-id lookup for click ─────────────────────────────────*/
const entityById = {{}};
BUILDINGS.forEach(b => {{
  // dummy entity just to carry the id for click detection on surfaces
  entityById[b.id] = {{ data: b }};
}});

/* ── Fly to a single enriched building (oblique close view) ─────*/
function flyToBuilding(bid, duration) {{
  const b = BUILDINGS.find(x => x.id === bid);
  if (!b) return;
  viewer.camera.flyTo({{
    destination: Cesium.Cartesian3.fromDegrees(
      b.lon, b.lat, (b.height || 15) + 140),
    orientation: {{
      heading: Cesium.Math.toRadians(-30),
      pitch:   Cesium.Math.toRadians(-32),
      roll: 0,
    }},
    duration: duration ?? 1.5,
  }});
}}

/* ── Building selector dropdown (sorted by nearby tree count) ────*/
const sel = document.getElementById('bldg-sel');
const byTrees = [...BUILDINGS].sort(
  (a,b) => (b.tree_count||0) - (a.tree_count||0));
byTrees.forEach(b => {{
  const o = document.createElement('option');
  o.value = b.id;
  o.textContent = `${{b.id}}  ·  🌳${{b.tree_count||0}} 🪟${{b.windows}} 🚪${{b.doors}}`;
  sel.appendChild(o);
}});
sel.addEventListener('change', () => flyToBuilding(sel.value));

/* ── Initial camera ─────────────────────────────────────────────*/
const FOCUS_BID = (byTrees[0] || BUILDINGS[0]).id;
sel.value = FOCUS_BID;
if ({json.dumps(initial_view)} === 'all') {{
  const lons = BUILDINGS.map(b => b.lon), lats = BUILDINGS.map(b => b.lat), pad = 0.0015;
  viewer.camera.flyTo({{
    destination: Cesium.Rectangle.fromDegrees(
      Math.min(...lons)-pad, Math.min(...lats)-pad,
      Math.max(...lons)+pad, Math.max(...lats)+pad),
    duration: 0.0,
  }});
}} else {{
  flyToBuilding(FOCUS_BID, 0.0);
}}

/* ── Highlight helpers ───────────────────────────────────────────*/
function setSurfaceAlpha(bid, on) {{
  surfaceEntities.forEach(s => {{
    if (s.bid !== bid) return;
    const isWall = s.stype === 'WallSurface';
    const alpha  = on ? 0.95 : (isWall ? 0.78 : 0.82);
    s.ent.polygon.material = surfaceColor(bid, s.stype, alpha);
  }});
}}

/* ── Panel rendering ─────────────────────────────────────────────*/
function renderPanel(bid) {{
  const rec = BUILDINGS.find(b => b.id === bid);
  if (!rec) return;
  const pd = PANEL_DATA[bid];
  let h = '';
  const matChips = (rec.roof_mats||[]).map(m =>
    `<span style="display:inline-block;border-radius:10px;padding:1px 7px;
     font-size:10px;background:#555;color:#eee;margin:1px">${{m}}</span>`
  ).join('');
  h += `<div class="prop"><div class="prop-key">Building ID</div>
    <div class="prop-val" style="font-family:monospace;font-size:10px">${{rec.id}}</div></div>`;
  h += `<div class="prop"><div class="prop-key">Height / Storeys</div>
    <div class="prop-val">${{rec.height}} m &nbsp;/&nbsp; ${{rec.storeys||'—'}} floors</div></div>`;
  h += `<div class="prop"><div class="prop-key">Openings</div>
    <div class="prop-val">🪟 ${{rec.windows}} windows &nbsp;🚪 ${{rec.doors}} doors</div></div>`;
  h += `<div class="prop"><div class="prop-key">Roof type</div>
    <div class="prop-val">${{rec.roof_type}}</div></div>`;
  if (matChips) h += `<div class="prop"><div class="prop-key">Roof material</div>
    <div class="prop-val">${{matChips}}</div></div>`;
  if (pd) {{
    const rects = (pd.openings||[]).map(o => {{
      const [x0,y0,x1,y1] = o.bbox_px;
      const fill   = o.type==='Window'?'rgba(255,60,60,.25)':'rgba(60,220,60,.25)';
      const stroke = o.type==='Window'?'#ff4444':'#44ee44';
      return `<rect x="${{x0}}" y="${{y0}}" width="${{x1-x0}}" height="${{y1-y0}}"
        fill="${{fill}}" stroke="${{stroke}}" stroke-width="6"/>`;
    }}).join('');
    h += `<div class="legend-scale">
      <span style="width:14px;height:10px;background:rgba(255,60,60,.3);
        border:2px solid #f44;border-radius:2px;display:inline-block"></span>
      <span>Window</span>
      <span style="width:14px;height:10px;background:rgba(60,220,60,.3);
        border:2px solid #4e4;border-radius:2px;display:inline-block;margin-left:6px"></span>
      <span>Door</span></div>`;
    h += `<div class="facade-wrap">
      <img src="data:image/jpeg;base64,${{pd.b64}}" alt="Facade">
      <svg viewBox="0 0 ${{pd.orig_w}} ${{pd.orig_h}}" xmlns="http://www.w3.org/2000/svg">
        ${{rects}}</svg></div>`;
    h += `<div class="prop"><div class="prop-key">Source</div>
      <div class="prop-val" style="font-size:10px;color:#546e7a">${{rec.source}} · mask-derived</div></div>`;
  }} else {{
    h += `<div class="empty">No facade image available</div>`;
  }}
  document.getElementById('info-scroll').innerHTML = h;
}}

/* ── Click handler ───────────────────────────────────────────────*/
let selectedBid = null;
const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
handler.setInputAction(click => {{
  const picked = viewer.scene.pick(click.position);
  let bid = null;
  if (picked && picked.id) {{
    const ent = picked.id;
    if (ent._treeData) {{
      if (selectedBid) {{ setSurfaceAlpha(selectedBid, false); selectedBid = null; }}
      renderTreePanel(ent._treeData);
      return;
    }}
    if (ent._bid) bid = ent._bid;
  }}
  if (!bid || !entityById[bid]) {{
    if (selectedBid) {{ setSurfaceAlpha(selectedBid, false); selectedBid=null; }}
    document.getElementById('info-scroll').innerHTML =
      '<div class="empty">Click an enriched building</div>';
    return;
  }}
  if (selectedBid && selectedBid !== bid) setSurfaceAlpha(selectedBid, false);
  selectedBid = bid;
  setSurfaceAlpha(bid, true);
  renderPanel(bid);
  const b = entityById[bid].data;
  viewer.camera.flyTo({{
    destination: Cesium.Cartesian3.fromDegrees(
      b.lon, b.lat, b.ground_z + b.height + 100),
    orientation: {{
      heading: Cesium.Math.toRadians(-30),
      pitch:   Cesium.Math.toRadians(-25),
      roll: 0,
    }},
    duration: 1.2,
  }});
}}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

handler.setInputAction(mv => {{
  const p = viewer.scene.pick(mv.endPosition);
  viewer.container.style.cursor =
    (p && p.id && (p.id._bid || p.id._treeData)) ? 'pointer' : '';
}}, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

/* ── Panel toggle ────────────────────────────────────────────────*/
document.getElementById('btn-panel').addEventListener('click', e => {{
  const panel = document.getElementById('right-panel');
  panel.classList.toggle('hidden');
  e.target.classList.toggle('off');
  e.target.textContent = panel.classList.contains('hidden') ? 'Panel ▶' : 'Panel ◀';
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output",           default=str(DEFAULT_OUTPUT))
    ap.add_argument("--max-px",           type=int, default=800)
    ap.add_argument("--context-radius",   type=float, default=1.5)
    ap.add_argument("--tree-radius",      type=float, default=80.0,
                    help="metres around each enriched building to include street trees")
    ap.add_argument("--no-osm-buildings", action="store_true")
    args = ap.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    driver = GraphDatabase.driver(
        NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications={"UNRECOGNIZED", "DEPRECATION"},
    )
    crs, sx, sy, sz = fetch_dataset(driver)
    horiz   = _horiz_epsg(crs)
    t32_wgs = Transformer.from_crs(horiz, "EPSG:4326", always_xy=True)
    geoid   = _geoid_offset(crs, sx, sy, sz)
    print(f"  Geoid offset applied: {geoid:+.3f} m")

    print("Fetching enriched building data…")
    buildings = fetch_enriched_buildings(driver, t32_wgs, geoid)
    ground_z_by_bid = {b["id"]: b["ground_z_raw"] for b in buildings}
    print(f"  {len(buildings)} buildings")

    print("Fetching LOD2 surface geometry…")
    surfaces = fetch_building_surfaces(driver, t32_wgs, geoid, ground_z_by_bid)
    print(f"  {len(surfaces)} surface polygons (wall + roof)")

    print("Computing façade wall frames…")
    wall_frames = find_best_wall_frame_per_building(driver)
    print(f"  {len(wall_frames)} buildings with wall geometry")

    print("Computing opening 3D positions…")
    openings_3d = fetch_openings_3d(driver, buildings, t32_wgs, geoid, wall_frames)
    print(f"  {len(openings_3d)} opening rectangles")

    print(f"Fetching street trees within {args.tree_radius:.0f} m of each building…")
    trees = fetch_street_trees(driver, buildings, t32_wgs, radius_m=args.tree_radius)
    print(f"  {len(trees)} street trees")

    mean_lon = sum(b["lon"] for b in buildings) / len(buildings)
    mean_lat = sum(b["lat"] for b in buildings) / len(buildings)
    driver.close()
    context_bldgs = []

    gml_files = sorted(DATA_DIR.glob("*.gml"), key=lambda p: int(p.stem))
    print(f"Encoding {len(gml_files)} facade images (max {args.max_px}px)…")
    panel_data = make_facade_panel_data(gml_files, args.max_px)

    html = build_html(
        buildings, surfaces, openings_3d, panel_data, context_bldgs, trees,
        osm_buildings=not args.no_osm_buildings,
    )
    out_path.write_text(html, encoding="utf-8")
    size_kb = out_path.stat().st_size // 1024
    print(f"\nSaved → {out_path}  ({size_kb} KB)")
    print("Requires internet for CesiumJS CDN.  Opening in browser…")
    webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
