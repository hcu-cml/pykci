#!/usr/bin/env python3
"""
build_graph_pointcloud.py

Exports Neo4j graph nodes + edges as a 3D Tiles 1.0 point-cloud (pnts)
quadtree tileset for scalable browser visualization.

Output → output/graph_tiles/
  tileset.json    3D Tiles quadtree manifest
  nodes.json      node properties for the property panel
  edges.json      graph edges (shown when camera is close)
  viewer.html     CesiumJS viewer
  serve.py        CORS-aware HTTP server (port 8081)
  tiles/          pnts tile files

Usage:
  py build_graph_pointcloud.py
  py build_graph_pointcloud.py --output output/graph_tiles
  py build_graph_pointcloud.py --building-tileset-url http://localhost:8080/tileset.json
  py build_graph_pointcloud.py --max-per-tile 500 --geo-error-scale 20
"""

import argparse
import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np
from pyproj import Transformer

from pykci import graphview
from pykci.crs import geoid_undulation, horiz_epsg_from_srs
from pykci.graphview import (DEFAULT_COLOR, SKIP_PROPS, fetch_all_edges, fetch_all_nodes,
                             fetch_dataset, node_group, node_label, xy_jitter)
from pykci.runtime import connect, console

# the shared tables plus the enrichment layers (provenance-coloured: OSM = green, LoD3 = gold)
LABEL_COLORS = {**graphview.LABEL_COLORS,
                "OsmBuilding": "#43A047", "OsmPOI": "#9CCC65",
                "Lod3Facade": "#FFB300", "Opening": "#FFE082"}
_PIXEL_SIZE = {**graphview.PIXEL_SIZE,
               "OsmBuilding": 12, "OsmPOI": 9, "Lod3Facade": 6, "Opening": 6}

# labels that name a node's group: the enrichment layers first, then the shared order with
# the composite surfaces
_GROUP_LABELS = ("OsmBuilding", "OsmPOI", "Lod3Facade", "Opening",
                 "Dataset", "City", "District", "Building",
                 "BuildingFunction", "RoofType",
                 "GeometrySolid", "GeometryMultiSurface", "GeometryCompositeSurface",
                 "GeometryPolygon", "GeometryRing", "GeometryLineString",
                 "TerrainIntersection")

# Priority: lower = higher importance = placed in parent tile (visible at coarser zoom)
_PRIORITY_RULES = [
    (frozenset({"Dataset", "City", "District"}), 0),
    (frozenset({"Building", "CityObject"}), 1),
    (frozenset({"BuildingFunction", "RoofType"}), 2),
    (frozenset({"BoundarySurface"}), 3),
    (frozenset({"GeometrySolid", "GeometryMultiSurface",
                "GeometryCompositeSurface", "TerrainIntersection"}), 4),
    (frozenset({"GeometryPolygon", "GeometryLineString"}), 5),
    (frozenset({"GeometryRing"}), 6),
]


def _node_priority(labels: list) -> int:
    lset = set(labels)
    for label_set, pri in _PRIORITY_RULES:
        if lset & label_set:
            return pri
    return 4


def _hex_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ── Projection ────────────────────────────────────────────────────────────────

def project_nodes(raw_nodes: list, crs_string: str, geoid_m: float):
    horiz = horiz_epsg_from_srs(crs_string)
    tf    = Transformer.from_crs(horiz, "EPSG:4326", always_xy=True)
    nodes_out, eid_map = [], {}

    for r in raw_nodes:
        eid   = r["eid"]
        labs  = r["labels"]
        props = {k: v for k, v in r["props"].items() if k not in SKIP_PROPS}
        cx, cy = r["cx"], r["cy"]
        if r.get("jitter"):
            dx, dy = xy_jitter(eid)
            cx += dx; cy += dy
        try:
            lon, lat = tf.transform(cx, cy)
        except Exception:
            continue

        alt   = r["cz"] + geoid_m + r.get("alt_extra", 0.0)
        group = node_group(labs, props, order=_GROUP_LABELS, generic=("CityObject", "Entity"))
        color = LABEL_COLORS.get(group, DEFAULT_COLOR)
        r_, g_, b_ = _hex_rgb(color)
        idx = len(nodes_out)
        eid_map[eid] = idx
        nodes_out.append({
            "nodeIndex": idx,
            "lon":       round(lon, 8),
            "lat":       round(lat, 8),
            "alt":       round(alt, 3),
            "r": r_, "g": g_, "b": b_,
            "pixelSize": _PIXEL_SIZE.get(group, 7),
            "priority":  _node_priority(labs),
            "group":     group,
            "color":     color,
            "label":     node_label(labs, props),
            "labels":    labs,
            "props":     props,
        })

    return nodes_out, eid_map


def build_edges(raw_edges: list, eid_map: dict) -> list:
    out = []
    for r in raw_edges:
        fi = eid_map.get(r["from_eid"])
        ti = eid_map.get(r["to_eid"])
        if fi is not None and ti is not None:
            out.append({"fi": fi, "ti": ti, "type": r["rel_type"]})
    return out


# ── Quadtree ──────────────────────────────────────────────────────────────────

class QuadTile:
    __slots__ = ("west", "south", "east", "north", "min_alt", "max_alt",
                 "depth", "nodes", "children")

    def __init__(self, west, south, east, north, min_alt, max_alt, depth=0):
        self.west = west; self.south = south
        self.east = east; self.north = north
        self.min_alt = min_alt; self.max_alt = max_alt
        self.depth = depth
        self.nodes: list    = []
        self.children: list = []  # 4 QuadTile (SW SE NW NE) or []

    def diagonal_m(self) -> float:
        mid_lat = (self.south + self.north) / 2
        dlat = abs(self.north - self.south) * 111_320
        dlon = abs(self.east  - self.west ) * 111_320 * math.cos(math.radians(mid_lat))
        return math.hypot(dlat, dlon)

    def geometric_error(self, scale: float) -> float:
        return round(self.diagonal_m() / scale, 2)

    def refine(self, max_per_tile: int, max_depth: int):
        if len(self.nodes) <= max_per_tile or self.depth >= max_depth:
            return
        # lowest priority number = most important = stays in parent tile
        self.nodes.sort(key=lambda n: n["priority"])
        overflow      = self.nodes[max_per_tile:]
        self.nodes    = self.nodes[:max_per_tile]
        if not overflow:
            return
        ml = (self.west  + self.east)  / 2
        mb = (self.south + self.north) / 2
        d  = self.depth + 1
        a0, a1 = self.min_alt, self.max_alt
        # 0=SW 1=SE 2=NW 3=NE
        self.children = [
            QuadTile(self.west, self.south, ml,        mb,         a0, a1, d),
            QuadTile(ml,        self.south, self.east, mb,         a0, a1, d),
            QuadTile(self.west, mb,         ml,        self.north, a0, a1, d),
            QuadTile(ml,        mb,         self.east, self.north, a0, a1, d),
        ]
        for node in overflow:
            col = 1 if node["lon"] >= ml else 0
            row = 1 if node["lat"] >= mb else 0
            self.children[row * 2 + col].nodes.append(node)
        for child in self.children:
            child.refine(max_per_tile, max_depth)


def build_quadtree(nodes: list, max_per_tile: int = 500, max_depth: int = 8) -> QuadTile:
    if not nodes:
        return QuadTile(0, 0, 0.001, 0.001, 0, 0)
    pad  = 0.0001
    lons = [n["lon"] for n in nodes]
    lats = [n["lat"] for n in nodes]
    alts = [n["alt"] for n in nodes]
    root = QuadTile(min(lons) - pad, min(lats) - pad,
                    max(lons) + pad, max(lats) + pad,
                    min(alts), max(alts))
    root.nodes = list(nodes)
    root.refine(max_per_tile, max_depth)
    return root


# ── pnts binary writer ────────────────────────────────────────────────────────

# Singleton EPSG:4979 → EPSG:4978 (geographic → ECEF) transformer
_ecef_tf: Transformer | None = None


def _geo_to_ecef(lon: float, lat: float, alt: float) -> tuple:
    return _ecef_tf.transform(lon, lat, alt)


def _pad8(b: bytes, pad_byte: bytes = b"\x00") -> bytes:
    r = len(b) % 8
    return b if r == 0 else b + pad_byte * (8 - r)


def write_pnts(filepath: Path, tile_nodes: list) -> None:
    """
    Feature table binary layout (per 3D Tiles 1.0 spec):
      POSITION : N × float32[3]  offset 0      (12 bytes/point)
      BATCH_ID : N × uint16      offset N*12    (2-byte aligned; N*12 is always even)
      RGB      : N × uint8[3]    offset N*14    (no alignment requirement)
      Total raw: N*17 bytes → padded to multiple of 8

    Batch table binary:
      r,g,b,pixelSize : N × uint8 each  offsets 0, N, 2N, 3N
      nodeIndex       : N × uint32       offset 4N  (4N always 4-aligned)
      Total raw: 8N bytes (always multiple of 8)
    """
    n = len(tile_nodes)
    if n == 0:
        return

    ecef = [_geo_to_ecef(nd["lon"], nd["lat"], nd["alt"]) for nd in tile_nodes]
    cx   = sum(e[0] for e in ecef) / n
    cy   = sum(e[1] for e in ecef) / n
    cz   = sum(e[2] for e in ecef) / n

    pos  = np.array([(e[0] - cx, e[1] - cy, e[2] - cz) for e in ecef], dtype=np.float32)
    bids = np.arange(n, dtype=np.uint16)
    rgb  = np.array([(nd["r"], nd["g"], nd["b"]) for nd in tile_nodes], dtype=np.uint8)

    ft_bin = _pad8(pos.tobytes() + bids.tobytes() + rgb.tobytes())

    r_arr   = np.array([nd["r"]         for nd in tile_nodes], dtype=np.uint8)
    g_arr   = np.array([nd["g"]         for nd in tile_nodes], dtype=np.uint8)
    b_arr   = np.array([nd["b"]         for nd in tile_nodes], dtype=np.uint8)
    ps_arr  = np.array([nd["pixelSize"] for nd in tile_nodes], dtype=np.uint8)
    idx_arr = np.array([nd["nodeIndex"] for nd in tile_nodes], dtype=np.uint32)
    bt_bin  = _pad8(r_arr.tobytes() + g_arr.tobytes() + b_arr.tobytes() +
                    ps_arr.tobytes() + idx_arr.tobytes())

    N12, N14 = n * 12, n * 14
    ft_json = _pad8(json.dumps({
        "POINTS_LENGTH": n,
        "BATCH_LENGTH":  n,
        "RTC_CENTER":    [float(cx), float(cy), float(cz)],
        "POSITION":      {"byteOffset": 0},
        "BATCH_ID":      {"byteOffset": N12},
        "RGB":           {"byteOffset": N14},
    }, separators=(",", ":")).encode(), b" ")

    bt_json = _pad8(json.dumps({
        "r":         {"byteOffset": 0,     "componentType": "UNSIGNED_BYTE", "type": "SCALAR"},
        "g":         {"byteOffset": n,     "componentType": "UNSIGNED_BYTE", "type": "SCALAR"},
        "b":         {"byteOffset": n * 2, "componentType": "UNSIGNED_BYTE", "type": "SCALAR"},
        "pixelSize": {"byteOffset": n * 3, "componentType": "UNSIGNED_BYTE", "type": "SCALAR"},
        "nodeIndex": {"byteOffset": n * 4, "componentType": "UNSIGNED_INT",  "type": "SCALAR"},
    }, separators=(",", ":")).encode(), b" ")

    byte_length = 28 + len(ft_json) + len(ft_bin) + len(bt_json) + len(bt_bin)
    with open(filepath, "wb") as f:
        f.write(b"pnts")
        f.write(struct.pack("<IIIIII", 1, byte_length,
                            len(ft_json), len(ft_bin), len(bt_json), len(bt_bin)))
        f.write(ft_json); f.write(ft_bin)
        f.write(bt_json); f.write(bt_bin)


# ── Tileset JSON + tile writing ───────────────────────────────────────────────

def _tile_json(tile: QuadTile, tile_id: str, geo_scale: float) -> dict | None:
    has_nodes = bool(tile.nodes)
    active_ch = [c for c in tile.children if c.nodes or c.children]
    if not has_nodes and not active_ch:
        return None

    d = {
        "boundingVolume": {"region": [
            math.radians(tile.west),  math.radians(tile.south),
            math.radians(tile.east),  math.radians(tile.north),
            tile.min_alt, tile.max_alt,
        ]},
        "geometricError": tile.geometric_error(geo_scale),
        "refine": "ADD",
    }
    if has_nodes:
        d["content"] = {"uri": f"tiles/{tile_id}.pnts"}

    children_json = []
    for i, child in enumerate(tile.children):
        cj = _tile_json(child, f"{tile_id}_{i}", geo_scale)
        if cj:
            children_json.append(cj)
    if children_json:
        d["children"] = children_json

    return d


def write_tileset(root: QuadTile, output_dir: Path, geo_scale: float) -> dict:
    tiles_dir = output_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    n_tiles = 0

    def _write(tile: QuadTile, tile_id: str):
        nonlocal n_tiles
        if tile.nodes:
            write_pnts(tiles_dir / f"{tile_id}.pnts", tile.nodes)
            n_tiles += 1
        for i, child in enumerate(tile.children):
            _write(child, f"{tile_id}_{i}")

    _write(root, "root")
    print(f"  Written {n_tiles} .pnts tiles → {tiles_dir}/")

    return {
        "asset": {"version": "1.0"},
        "geometricError": root.geometric_error(geo_scale) * 2,
        "root": _tile_json(root, "root", geo_scale),
    }


# ── Viewer HTML ───────────────────────────────────────────────────────────────

def build_viewer_html(building_tileset_url: str, label_colors: dict,
                      n_nodes: int, n_edges: int, title: str) -> str:
    lc_json  = json.dumps({**label_colors, "default": DEFAULT_COLOR})
    btu_json = json.dumps(building_tileset_url)
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script>window.CESIUM_BASE_URL='https://cdn.jsdelivr.net/npm/cesium@1.116.0/Build/Cesium/';</script>
<script src="https://cdn.jsdelivr.net/npm/cesium@1.116.0/Build/Cesium/Cesium.js"></script>
<link href="https://cdn.jsdelivr.net/npm/cesium@1.116.0/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:100%;height:100%;overflow:hidden;
           font-family:'Segoe UI',system-ui,sans-serif;font-size:13px}}
#cesiumContainer{{width:100%;height:100%}}

#topbar{{
  position:absolute;top:0;left:0;right:0;z-index:100;
  background:rgba(18,28,50,.88);backdrop-filter:blur(6px);
  display:flex;align-items:center;gap:8px;padding:6px 12px;
  border-bottom:1px solid rgba(15,52,96,.7);
}}
#topbar h1{{font-size:13px;font-weight:600;color:#90caf9;margin-right:auto;white-space:nowrap}}
.ctrl-lbl{{font-size:11px;color:#90caf9;white-space:nowrap}}
#opacity-slider{{width:80px;cursor:pointer;accent-color:#1565c0}}
#opacity-val{{font-size:11px;color:#90caf9;min-width:28px}}
.btn{{background:#1565c0;border:none;border-radius:4px;color:#fff;
      padding:3px 9px;cursor:pointer;font-size:11px;white-space:nowrap}}
.btn:hover{{background:#1976d2}}
.btn.off{{background:#37474f}}
.stat{{font-size:10px;color:#546e7a;white-space:nowrap}}
#loading{{font-size:11px;color:#80cbc4;white-space:nowrap}}

#left-panel{{
  position:absolute;top:40px;left:0;bottom:0;width:185px;z-index:100;
  background:rgba(18,28,50,.88);backdrop-filter:blur(6px);
  border-right:1px solid rgba(15,52,96,.6);
  display:flex;flex-direction:column;overflow:hidden;
}}
.panel-title{{
  font-size:10px;font-weight:600;color:#546e7a;padding:7px 10px;
  text-transform:uppercase;letter-spacing:.5px;
  border-bottom:1px solid rgba(15,52,96,.6);flex-shrink:0;
}}
#legend{{overflow-y:auto;padding:6px;flex:1}}
.leg-item{{
  display:flex;align-items:center;gap:6px;padding:3px 5px;
  border-radius:4px;cursor:pointer;user-select:none;color:#e0e0e0;
}}
.leg-item:hover{{background:rgba(15,52,96,.6)}}
.leg-item.off{{opacity:.3}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}

#right-panel{{
  position:absolute;top:40px;right:0;bottom:0;width:260px;z-index:100;
  background:rgba(18,28,50,.88);backdrop-filter:blur(6px);
  border-left:1px solid rgba(15,52,96,.6);
  display:flex;flex-direction:column;overflow:hidden;
}}
#info-body{{overflow-y:auto;padding:10px;flex:1}}
.prop{{margin-bottom:8px}}
.prop-key{{font-size:10px;color:#546e7a;text-transform:uppercase;letter-spacing:.4px}}
.prop-val{{font-size:12px;color:#e0e0e0;word-break:break-all;margin-top:1px}}
.chip{{display:inline-block;border-radius:3px;padding:1px 6px;font-size:10px;margin:1px;color:#fff}}
.empty{{color:#37474f;font-size:12px;padding:10px}}

.cesium-credit-container{{display:none!important}}

#edge-note{{
  position:absolute;bottom:10px;left:50%;transform:translateX(-50%);
  background:rgba(18,28,50,.82);color:#80cbc4;
  padding:4px 12px;border-radius:4px;font-size:11px;
  z-index:100;pointer-events:none;display:none;
}}
</style>
</head>
<body>
<div id="cesiumContainer"></div>

<div id="topbar">
  <h1>Graph Tiles — {title}</h1>
  <span class="ctrl-lbl">Buildings</span>
  <input id="opacity-slider" type="range" min="0" max="100" value="50" step="5">
  <span id="opacity-val">50%</span>
  <button class="btn" id="btn-edges">Edges ON</button>
  <button class="btn" id="btn-mode">Dark Mode</button>
  <span class="stat">{n_nodes} nodes &middot; {n_edges} edges &middot; {ts}</span>
  <span id="loading">Loading nodes…</span>
</div>

<div id="left-panel">
  <div class="panel-title">Node types</div>
  <div id="legend"><div class="empty">Loading…</div></div>
</div>

<div id="right-panel">
  <div class="panel-title">Properties</div>
  <div id="info-body"><div class="empty">Click a node</div></div>
</div>

<div id="edge-note">Zoom in to see edges (&lt; 2 km altitude)</div>

<script>
const LABEL_COLORS   = {lc_json};
const BUILDING_URL   = {btu_json};
const EDGE_ALT_LIMIT = 2000;   // metres — edges shown below this altitude

let GRAPH_NODES = null;
let GRAPH_EDGES = null;
let hiddenGroups = new Set();

// ── Viewer ────────────────────────────────────────────────────────────────────
const _CARTO_CREDIT = '© OpenStreetMap contributors © CARTO';
const viewer = new Cesium.Viewer('cesiumContainer', {{
  imageryProvider:      false,
  terrainProvider:      new Cesium.EllipsoidTerrainProvider(),
  baseLayerPicker:      false,
  geocoder:             false,
  homeButton:           false,
  sceneModePicker:      false,
  navigationHelpButton: false,
  animation:            false,
  timeline:             false,
  infoBox:              false,
  selectionIndicator:   false,
}});
viewer.scene.globe.enableLighting = false;
viewer.scene.fog.enabled = false;
viewer.imageryLayers.removeAll();

const layerDark = viewer.imageryLayers.addImageryProvider(
  new Cesium.UrlTemplateImageryProvider({{
    url: 'https://basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}.png',
    credit: _CARTO_CREDIT, maximumLevel: 19,
  }})
);
layerDark.show = false;

const layerLight = viewer.imageryLayers.addImageryProvider(
  new Cesium.UrlTemplateImageryProvider({{
    url: 'https://basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
    credit: _CARTO_CREDIT, maximumLevel: 19,
  }})
);

let isDark = false;
function applyMode(dark) {{
  isDark = dark;
  layerLight.show = !dark;
  layerDark.show  =  dark;
  viewer.scene.globe.baseColor = dark
    ? new Cesium.Color(0.06, 0.06, 0.08, 1)
    : new Cesium.Color(0.88, 0.76, 0.42, 1);
  document.body.style.background = dark ? '#111' : '#d8eecf';
  document.getElementById('btn-mode').textContent = dark ? 'Light Mode' : 'Dark Mode';
}}
applyMode(false);

// ── Building 3D Tiles (optional) ──────────────────────────────────────────────
let buildingTileset = null;
let tileOpacity = 0.5;

function applyBuildingOpacity(v) {{
  tileOpacity = v;
  if (!buildingTileset) return;
  buildingTileset.style = new Cesium.Cesium3DTileStyle({{
    color: `color('white', ${{v}})`,
  }});
}}

(async () => {{
  if (!BUILDING_URL) return;
  try {{
    buildingTileset = await Cesium.Cesium3DTileset.fromUrl(BUILDING_URL);
    viewer.scene.primitives.add(buildingTileset);
    applyBuildingOpacity(tileOpacity);
    await viewer.zoomTo(buildingTileset);
  }} catch (e) {{
    console.warn('Building tiles not loaded:', e.message);
  }}
}})();

// ── Graph Node 3D Tiles ───────────────────────────────────────────────────────
let graphTileset = null;
let selectedFeature = null;
let hoveredFeature  = null;

const SELECT_COLOR = Cesium.Color.fromCssColorString('#B3E5FC').withAlpha(1.0);
const HOVER_COLOR  = Cesium.Color.fromCssColorString('#FFF9C4').withAlpha(1.0);
const RESET_COLOR  = Cesium.Color.WHITE;

(async () => {{
  try {{
    graphTileset = await Cesium.Cesium3DTileset.fromUrl('tileset.json', {{
      maximumScreenSpaceError: 16,
    }});
    viewer.scene.primitives.add(graphTileset);

    graphTileset.style = new Cesium.Cesium3DTileStyle({{
      color:     'rgb(${{r}}, ${{g}}, ${{b}})',
      pointSize: '${{pixelSize}}',
    }});

    if (!buildingTileset) {{
      await viewer.zoomTo(graphTileset);
    }}
  }} catch (e) {{
    console.error('Graph tiles failed:', e);
  }}
}})();

// ── Edges ─────────────────────────────────────────────────────────────────────
const lineColl = viewer.scene.primitives.add(new Cesium.PolylineCollection());
let showEdges  = true;
const edgeMat  = Cesium.Material.fromType('Color', {{
  color: Cesium.Color.fromCssColorString('#546E7A').withAlpha(0.5),
}});
const edgeNote = document.getElementById('edge-note');

function refreshEdges() {{
  lineColl.removeAll();
  if (!showEdges || !GRAPH_NODES || !GRAPH_EDGES) return;

  const alt = viewer.camera.positionCartographic.height;
  if (alt > EDGE_ALT_LIMIT) {{
    edgeNote.style.display = 'block';
    return;
  }}
  edgeNote.style.display = 'none';

  // Compute view rectangle for frustum culling
  const rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
  const toD  = Cesium.Math.toDegrees;
  const rW   = rect ? toD(rect.west)  - 0.001 : -180;
  const rS   = rect ? toD(rect.south) - 0.001 : -90;
  const rE   = rect ? toD(rect.east)  + 0.001 : 180;
  const rN   = rect ? toD(rect.north) + 0.001 : 90;

  for (const e of GRAPH_EDGES) {{
    const n1 = GRAPH_NODES[e.fi], n2 = GRAPH_NODES[e.ti];
    if (!n1 || !n2) continue;
    // Both endpoints out of view → skip
    const in1 = n1.lon >= rW && n1.lon <= rE && n1.lat >= rS && n1.lat <= rN;
    const in2 = n2.lon >= rW && n2.lon <= rE && n2.lat >= rS && n2.lat <= rN;
    if (!in1 && !in2) continue;
    lineColl.add({{
      positions: Cesium.Cartesian3.fromDegreesArrayHeights([
        n1.lon, n1.lat, n1.alt,
        n2.lon, n2.lat, n2.alt,
      ]),
      width:    1.5,
      material: edgeMat,
    }});
  }}
}}

viewer.camera.moveEnd.addEventListener(refreshEdges);

// ── Legend builder ────────────────────────────────────────────────────────────
function buildLegend(nodes) {{
  const groups = [...new Set(nodes.map(n => n.group))].sort();
  const leg = document.getElementById('legend');
  leg.innerHTML = '';
  for (const g of groups) {{
    const color = LABEL_COLORS[g] || LABEL_COLORS.default;
    const item  = document.createElement('div');
    item.className = 'leg-item';
    item.dataset.group = g;
    item.innerHTML = `<span class="leg-dot" style="background:${{color}}"></span><span>${{g}}</span>`;
    item.addEventListener('click', () => {{
      hiddenGroups.has(g) ? hiddenGroups.delete(g) : hiddenGroups.add(g);
      item.classList.toggle('off');
      // Re-apply tileset style to show/hide groups via colour alpha
      if (graphTileset) {{
        graphTileset.style = new Cesium.Cesium3DTileStyle({{
          show:      hiddenGroups.size
            ? 'false'   // placeholder — group filtering via style not directly supported
            : 'true',
          color:     'rgb(${{r}}, ${{g}}, ${{b}})',
          pointSize: '${{pixelSize}}',
        }});
      }}
    }});
    leg.appendChild(item);
  }}
}}

// ── Property panel ────────────────────────────────────────────────────────────
function renderPanel(node) {{
  if (!node) return;
  let h = '<div class="prop"><span class="prop-key">Labels</span><div class="prop-val">';
  for (const l of (node.labels || [])) {{
    const c = LABEL_COLORS[l] || '#616161';
    h += `<span class="chip" style="background:${{c}}">${{l}}</span>`;
  }}
  h += '</div></div>';
  for (const [k, v] of Object.entries(node.props || {{}}).sort()) {{
    h += `<div class="prop"><span class="prop-key">${{k}}</span>`
       + `<div class="prop-val">${{String(v)}}</div></div>`;
  }}
  document.getElementById('info-body').innerHTML = h;
}}

// ── Data loading ──────────────────────────────────────────────────────────────
Promise.all([
  fetch('nodes.json').then(r => r.json()),
  fetch('edges.json').then(r => r.json()),
]).then(([nodes, edges]) => {{
  GRAPH_NODES = nodes;
  GRAPH_EDGES = edges;
  document.getElementById('loading').textContent =
    `${{nodes.length}} nodes · ${{edges.length}} edges loaded`;
  setTimeout(() => {{ document.getElementById('loading').style.display = 'none'; }}, 3000);
  buildLegend(nodes);
  refreshEdges();
}}).catch(err => {{
  document.getElementById('loading').textContent = 'nodes.json load failed — serve via HTTP';
  console.error(err);
}});

// ── Picking ───────────────────────────────────────────────────────────────────
const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);

handler.setInputAction(click => {{
  const picks = viewer.scene.drillPick(click.position);

  // 1. Graph node pick (point cloud feature with nodeIndex)
  const ptHit = picks.find(p =>
    p instanceof Cesium.Cesium3DTileFeature &&
    p.tileset === graphTileset
  );
  if (ptHit) {{
    if (selectedFeature && selectedFeature !== ptHit) selectedFeature.color = RESET_COLOR;
    selectedFeature = ptHit;
    ptHit.color = SELECT_COLOR;
    if (GRAPH_NODES) {{
      const idx = ptHit.getProperty('nodeIndex');
      if (idx != null) renderPanel(GRAPH_NODES[idx]);
    }}
    return;
  }}

  // 2. Building tile feature
  const bldHit = picks.find(p =>
    p instanceof Cesium.Cesium3DTileFeature &&
    p.tileset === buildingTileset
  );
  if (bldHit) {{
    if (selectedFeature) {{ selectedFeature.color = RESET_COLOR; selectedFeature = null; }}
    bldHit.color = SELECT_COLOR;
    selectedFeature = bldHit;
    return;
  }}

  // 3. Empty click
  if (selectedFeature) {{ selectedFeature.color = RESET_COLOR; selectedFeature = null; }}
  document.getElementById('info-body').innerHTML = '<div class="empty">Click a node</div>';
}}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

handler.setInputAction(mv => {{
  const p = viewer.scene.pick(mv.endPosition);
  const f = (p instanceof Cesium.Cesium3DTileFeature && p.tileset === graphTileset) ? p : null;
  if (f !== hoveredFeature) {{
    if (hoveredFeature && hoveredFeature !== selectedFeature) hoveredFeature.color = RESET_COLOR;
    hoveredFeature = f;
    if (f && f !== selectedFeature) f.color = HOVER_COLOR;
    viewer.container.style.cursor = f ? 'pointer' : '';
  }}
}}, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

// ── Controls ──────────────────────────────────────────────────────────────────
const opSlider = document.getElementById('opacity-slider');
const opVal    = document.getElementById('opacity-val');
opSlider.addEventListener('input', () => {{
  opVal.textContent = opSlider.value + '%';
  applyBuildingOpacity(opSlider.value / 100);
}});

document.getElementById('btn-edges').addEventListener('click', e => {{
  showEdges = !showEdges;
  e.target.textContent = showEdges ? 'Edges ON' : 'Edges OFF';
  e.target.classList.toggle('off', !showEdges);
  refreshEdges();
}});

document.getElementById('btn-mode').addEventListener('click', () => applyMode(!isDark));
</script>
</body>
</html>
"""


# ── serve.py content ──────────────────────────────────────────────────────────

_SERVE_PY = '''\
"""python3 serve.py  →  http://localhost:8081/viewer.html"""
import http.server, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        super().end_headers()
    def do_OPTIONS(self):
        self.send_response(200); self.end_headers()
    def log_message(self, fmt, *args):
        pass

Handler.extensions_map.update({
    ".pnts": "application/octet-stream",
    ".json": "application/json",
})

PORT = 8081
print(f"Graph tiles at http://localhost:{PORT}/")
print(f"  viewer → http://localhost:{PORT}/viewer.html")
print(f"  tiles  → http://localhost:{PORT}/tileset.json")
http.server.HTTPServer(("", PORT), Handler).serve_forever()
'''


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Build a 3D Tiles point-cloud tileset from Neo4j graph nodes."
    )
    ap.add_argument("--output", default="output/graph_tiles",
                    help="Output directory (default: output/graph_tiles)")
    ap.add_argument("--building-tileset-url", default="http://localhost:8080/tileset.json",
                    help="Optional building 3D Tiles URL shown alongside graph tiles")
    ap.add_argument("--max-per-tile", type=int, default=500,
                    help="Max nodes per quadtree tile before splitting (default: 500)")
    ap.add_argument("--max-depth", type=int, default=8,
                    help="Max quadtree depth (default: 8)")
    ap.add_argument("--geo-error-scale", type=float, default=20.0,
                    help="geometricError = tile_diagonal_m / scale (default: 20)")
    ap.add_argument("--osm-enrichment", action="store_true",
                    help="Add matched OSM buildings + attached POIs as an explicit "
                         "enrichment layer (ENRICHED_BY / HAS_POI edges) and suppress "
                         "the raw OSM geometry chain so the graph stays building-centric")
    args = ap.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    global _ecef_tf
    _ecef_tf = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)

    # ── Query Neo4j ───────────────────────────────────────────────────────────
    driver = connect()
    try:
        crs, sx, sy, sz = fetch_dataset(driver)
        raw_nodes        = fetch_all_nodes(
            driver,
            exclude_osm_geometry=args.osm_enrichment,
            include_osm_enrichment=args.osm_enrichment,
        )
        raw_edges        = fetch_all_edges(driver)
    finally:
        driver.close()

    if not raw_nodes:
        print("No nodes found — is the graph populated?", file=sys.stderr)
        sys.exit(1)

    geoid_m, _ = geoid_undulation(crs, sx, sy, sz)
    print(f"  Geoid offset: {geoid_m:+.3f} m")

    # ── Project + build quadtree ──────────────────────────────────────────────
    print("Projecting to WGS84…")
    nodes, eid_map = project_nodes(raw_nodes, crs, geoid_m)
    edges          = build_edges(raw_edges, eid_map)
    print(f"  {len(nodes)} nodes, {len(edges)} edges after projection")

    print("Building quadtree…")
    t0   = time.perf_counter()
    root = build_quadtree(nodes, args.max_per_tile, args.max_depth)
    print(f"  Done ({time.perf_counter()-t0:.2f}s)")

    # ── Write tiles ───────────────────────────────────────────────────────────
    print("Writing pnts tiles…")
    t0       = time.perf_counter()
    tileset  = write_tileset(root, out_dir, args.geo_error_scale)
    print(f"  Done ({time.perf_counter()-t0:.2f}s)")

    # ── Write JSON assets ─────────────────────────────────────────────────────
    print("Writing companion files…")

    (out_dir / "tileset.json").write_text(
        json.dumps(tileset, ensure_ascii=False), encoding="utf-8"
    )

    # nodes.json — strip large/redundant fields, keep display info + props
    nodes_lite = [
        {
            "lon":    n["lon"], "lat": n["lat"], "alt": n["alt"],
            "group":  n["group"],   "color": n["color"],
            "label":  n["label"],   "labels": n["labels"],
            "props":  n["props"],
        }
        for n in nodes
    ]
    (out_dir / "nodes.json").write_text(
        json.dumps(nodes_lite, ensure_ascii=False, default=str), encoding="utf-8"
    )

    (out_dir / "edges.json").write_text(
        json.dumps(edges, ensure_ascii=False), encoding="utf-8"
    )

    # ── HTML viewer + serve script ────────────────────────────────────────────
    all_groups = sorted({n["group"] for n in nodes})
    html = build_viewer_html(
        building_tileset_url=args.building_tileset_url,
        label_colors=LABEL_COLORS,
        n_nodes=len(nodes),
        n_edges=len(edges),
        title=out_dir.name,
    )
    (out_dir / "viewer.html").write_text(html, encoding="utf-8")
    (out_dir / "serve.py").write_text(_SERVE_PY, encoding="utf-8")

    # ── Summary ───────────────────────────────────────────────────────────────
    total_pnts = sum(f.stat().st_size for f in (out_dir / "tiles").glob("*.pnts"))
    n_tiles    = len(list((out_dir / "tiles").glob("*.pnts")))
    print()
    print(f"Output → {out_dir}/")
    print(f"  {n_tiles} tiles  ({total_pnts // 1024} KB pnts)")
    print(f"  nodes.json   {(out_dir/'nodes.json').stat().st_size // 1024} KB")
    print(f"  edges.json   {(out_dir/'edges.json').stat().st_size // 1024} KB")
    print()
    print("To view:")
    print(f"  cd {out_dir} && python serve.py          (graph tiles at :8081)")
    print(f"  cd output/3dtiles && python serve.py     (buildings at :8080, optional)")
    print(f"  Open http://localhost:8081/viewer.html")


if __name__ == "__main__":
    console()
    main()
