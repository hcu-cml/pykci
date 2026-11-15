#!/usr/bin/env python3
"""
visualize_graph_3d.py
Combines a 3D Tiles tileset with Neo4j graph nodes and edges in a CesiumJS scene.

Building and CityObject nodes that have center coordinates are placed as 3D spheres
at their centroid (ground_z + measured_height/2). Edges between spatial nodes are
drawn as 3D polylines. The 3D Tiles tileset is semi-transparent so graph elements
are visible through the building models.

Requirements:
  - Neo4j running with ingested CityGML data (ingest_citygml.py)
  - 3D Tiles served locally:  cd output/3dtiles && python serve.py
    Then open the generated HTML in a browser.

Usage:
  py visualize_graph_3d.py
  py visualize_graph_3d.py --tileset-url http://localhost:8080/tileset.json
  py visualize_graph_3d.py --output output/html/graph_3d.html
  py visualize_graph_3d.py --opacity 0.4
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from pyproj import Transformer

from pykci.crs import geoid_undulation, horiz_epsg_from_srs
from pykci.graphview import (DEFAULT_COLOR, LABEL_COLORS, PIXEL_SIZE, SKIP_PROPS, fetch_all_edges,
                             fetch_all_nodes, fetch_dataset, node_group, node_label, xy_jitter)
from pykci.runtime import connect, console

# ── Data transformation ───────────────────────────────────────────────────────

def build_payload(raw_nodes: list, raw_edges: list, crs_string: str, geoid_m: float):
    horiz       = horiz_epsg_from_srs(crs_string)
    transformer = Transformer.from_crs(horiz, "EPSG:4326", always_xy=True)

    nodes: list  = []
    eid_map: dict = {}

    for r in raw_nodes:
        eid   = r["eid"]
        labs  = r["labels"]
        props = {k: v for k, v in r["props"].items() if k not in SKIP_PROPS}

        cx, cy = r["cx"], r["cy"]
        if r.get("jitter"):
            dx, dy = xy_jitter(eid)
            cx += dx
            cy += dy

        try:
            lon, lat = transformer.transform(cx, cy)
        except Exception:
            continue

        alt   = r["cz"] + geoid_m + r.get("alt_extra", 0.0)
        group = node_group(labs, props)

        idx          = len(nodes)
        eid_map[eid] = idx
        nodes.append({
            "lon":       round(lon, 8),
            "lat":       round(lat, 8),
            "alt":       round(alt, 3),
            "label":     node_label(labs, props),
            "group":     group,
            "color":     LABEL_COLORS.get(group, DEFAULT_COLOR),
            "labels":    labs,
            "props":     props,
            "pixelSize": PIXEL_SIZE.get(group, 7),
        })

    edges: list = []
    for r in raw_edges:
        fi = eid_map.get(r["from_eid"])
        ti = eid_map.get(r["to_eid"])
        if fi is None or ti is None:
            continue
        edges.append({"fi": fi, "ti": ti, "type": r["rel_type"]})

    return nodes, edges


# ── HTML generation ───────────────────────────────────────────────────────────

def build_html(nodes, edges, tileset_url, tile_opacity, title):
    all_groups = sorted({n["group"] for n in nodes})

    legend_rows = []
    for g in all_groups:
        color = LABEL_COLORS.get(g, DEFAULT_COLOR)
        legend_rows.append(
            f'<div class="leg-item" data-group="{g}">'
            f'<span class="leg-dot" style="background:{color}"></span>'
            f'<span>{g}</span></div>'
        )
    legend_html = "\n".join(legend_rows)

    nodes_json        = json.dumps(nodes,  ensure_ascii=False, default=str)
    edges_json        = json.dumps(edges,  ensure_ascii=False, default=str)
    label_colors_json = json.dumps({**LABEL_COLORS, "default": DEFAULT_COLOR})
    tileset_url_json  = json.dumps(tileset_url)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_nodes, n_edges  = len(nodes), len(edges)
    opacity_pct       = int(tile_opacity * 100)

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
           font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;background:#d8eecf}}
#cesiumContainer{{width:100%;height:100%}}

/* ── Overlays ─────────────────────────────────────────────────── */
#topbar{{
  position:absolute;top:0;left:0;right:0;z-index:100;
  background:rgba(18,28,50,.85);backdrop-filter:blur(6px);
  display:flex;align-items:center;gap:8px;padding:6px 12px;
  border-bottom:1px solid rgba(15,52,96,.7);
}}
#topbar h1{{font-size:13px;font-weight:600;color:#90caf9;margin-right:auto;white-space:nowrap}}
.ctrl-lbl{{font-size:11px;color:#90caf9;white-space:nowrap}}
#opacity-slider{{width:80px;cursor:pointer;accent-color:#1565c0;vertical-align:middle}}
#opacity-val{{font-size:11px;color:#90caf9;min-width:28px}}
.btn{{background:#1565c0;border:none;border-radius:4px;color:#fff;
      padding:3px 9px;cursor:pointer;font-size:11px;white-space:nowrap}}
.btn:hover{{background:#1976d2}}
.btn.off{{background:#37474f}}
.stat{{font-size:10px;color:#546e7a;white-space:nowrap}}

#left-panel{{
  position:absolute;top:40px;left:0;bottom:0;width:185px;z-index:100;
  background:rgba(18,28,50,.85);backdrop-filter:blur(6px);
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
  background:rgba(18,28,50,.85);backdrop-filter:blur(6px);
  border-left:1px solid rgba(15,52,96,.6);
  display:flex;flex-direction:column;overflow:hidden;
}}
#info-body{{overflow-y:auto;padding:10px;flex:1}}
.prop{{margin-bottom:8px}}
.prop-key{{font-size:10px;color:#546e7a;text-transform:uppercase;letter-spacing:.4px}}
.prop-val{{font-size:12px;color:#e0e0e0;word-break:break-all;margin-top:1px}}
.chip{{display:inline-block;border-radius:3px;padding:1px 6px;font-size:10px;margin:1px;color:#fff}}
.empty{{color:#37474f;font-size:12px;padding:10px}}

/* hide Cesium credit bar */
.cesium-credit-container{{display:none!important}}

/* ── Light beige / warm-yellow mode ──────────────────────────────── */
body.light{{background:#f0e0a8}}
body.light #topbar{{
  background:rgba(245,228,175,.94);border-bottom-color:rgba(160,115,35,.30);
}}
body.light #topbar h1{{color:#2e1e06}}
body.light .ctrl-lbl{{color:#2e1e06}}
body.light #opacity-val{{color:#2e1e06}}
body.light #opacity-slider{{accent-color:#8a6010}}
body.light .btn{{background:#8a6010}}
body.light .btn:hover{{background:#aa7818}}
body.light .btn.off{{background:#c8a458}}
body.light .stat{{color:#7a5a18}}
body.light #left-panel{{
  background:rgba(245,228,175,.94);border-right-color:rgba(160,115,35,.30);
}}
body.light #right-panel{{
  background:rgba(245,228,175,.94);border-left-color:rgba(160,115,35,.30);
}}
body.light .panel-title{{color:#6a4a08;border-bottom-color:rgba(160,115,35,.30)}}
body.light .leg-item{{color:#1e1406}}
body.light .leg-item:hover{{background:rgba(160,115,35,.18)}}
body.light .prop-key{{color:#6a4a08}}
body.light .prop-val{{color:#1a1004}}
body.light .empty{{color:#8a6a30}}
</style>
</head>
<body>
<div id="cesiumContainer"></div>

<div id="topbar">
  <h1>Neo4j 3D &mdash; {title}</h1>
  <span class="ctrl-lbl">Tiles</span>
  <input id="opacity-slider" type="range" min="0" max="100" value="{opacity_pct}" step="5"
         title="3D Tiles opacity">
  <span id="opacity-val">{opacity_pct}%</span>
  <button class="btn" id="btn-edges">Edges ON</button>
  <button class="btn off" id="btn-labels">Node Labels</button>
  <button class="btn off" id="btn-edge-labels">Edge Labels</button>
  <button class="btn" id="btn-mode">Dark Mode</button>
  <span class="stat">{n_nodes} nodes &middot; {n_edges} edges &middot; {ts}</span>
</div>

<div id="left-panel">
  <div class="panel-title">Node types</div>
  <div id="legend">{legend_html}</div>
</div>

<div id="right-panel">
  <div class="panel-title">Properties</div>
  <div id="info-body"><div class="empty">Click a node</div></div>
</div>

<script>
const NODES        = {nodes_json};
const EDGES        = {edges_json};
const LABEL_COLORS = {label_colors_json};
const TILESET_URL  = {tileset_url_json};

// ── Viewer ─────────────────────────────────────────────────────────────────
const _CARTO_CREDIT = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>';
const viewer = new Cesium.Viewer('cesiumContainer', {{
  imageryProvider:      false,   // suppress deprecated constructor param; add layers manually
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
viewer.imageryLayers.removeAll(); // clear any default layer Cesium may have added

// Light basemap — CartoDB Voyager: colorful but minimal, no clutter
const layerLight = viewer.imageryLayers.addImageryProvider(
  new Cesium.UrlTemplateImageryProvider({{
    url: 'https://basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
    credit: _CARTO_CREDIT, maximumLevel: 19,
  }})
);
layerLight.alpha      = 0.82;   // mostly opaque — globe base fills oceans/edges
layerLight.contrast   = 1.06;   // slight crispness
layerLight.brightness = 1.02;

// Dark basemap (hidden by default)
const layerDark = viewer.imageryLayers.addImageryProvider(
  new Cesium.UrlTemplateImageryProvider({{
    url: 'https://basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}.png',
    credit: _CARTO_CREDIT, maximumLevel: 19,
  }})
);
layerDark.show = false;

// Grid — light beige/warm mode (very subtle — just coordinate reference)
const layerGridLight = viewer.imageryLayers.addImageryProvider(
  new Cesium.GridImageryProvider({{
    cells: 8,
    color: new Cesium.Color(0.55, 0.35, 0.04, 0.20),  // faint amber lines
    glowWidth: 0,
    backgroundColor: new Cesium.Color(0.88, 0.74, 0.30, 0.03),
  }})
);

// Grid — dark mode (subtle)
const layerGridDark = viewer.imageryLayers.addImageryProvider(
  new Cesium.GridImageryProvider({{
    cells: 8,
    color: new Cesium.Color(0.20, 0.65, 0.30, 0.16),
    glowWidth: 0,
    backgroundColor: Cesium.Color.TRANSPARENT,
  }})
);
layerGridDark.show = false;

// ── Light / Dark mode toggle ───────────────────────────────────────────────
let isDark = false;

function applyMode(dark) {{
  isDark = dark;
  layerLight.show     = !dark;
  layerDark.show      =  dark;
  layerGridLight.show = !dark;
  layerGridDark.show  =  dark;
  viewer.scene.globe.baseColor = dark
    ? new Cesium.Color(0.06, 0.06, 0.08, 1.0)   // near-black for dark mode
    : new Cesium.Color(0.88, 0.76, 0.42, 1.0);  // warm sandy wheat for light mode
  document.body.classList.toggle('light', !dark);
  const btn = document.getElementById('btn-mode');
  if (btn) btn.textContent = dark ? 'Light Mode' : 'Dark Mode';
}}
applyMode(false); // start in light-green mode

// ── Initial camera — zoom to data extent immediately (no waiting for tiles) ─
if (NODES.length) {{
  const lons = NODES.map(n => n.lon), lats = NODES.map(n => n.lat);
  const pad  = 0.002;
  viewer.camera.setView({{
    destination: Cesium.Rectangle.fromDegrees(
      Math.min(...lons) - pad, Math.min(...lats) - pad,
      Math.max(...lons) + pad, Math.max(...lats) + pad
    ),
  }});
}}

// ── 3D Tiles ───────────────────────────────────────────────────────────────
let tileset     = null;
let tileOpacity = {tile_opacity};

function applyTileOpacity(v) {{
  tileOpacity = v;
  if (!tileset) return;
  // Multiply material colours by white → preserves GLB material palette with opacity
  tileset.style = new Cesium.Cesium3DTileStyle({{
    color: `color('white', ${{v}})`,
  }});
}}

// Per-feature hover / selection state
let hoveredFeature  = null;
let selectedFeature = null;
const HOVER_COLOR    = Cesium.Color.fromCssColorString('#FFF9C4').withAlpha(1.0); // warm highlight
const SELECT_COLOR   = Cesium.Color.fromCssColorString('#B3E5FC').withAlpha(1.0); // cool blue selection
const FEATURE_RESET  = Cesium.Color.WHITE;

function _setFeatureColor(f, col) {{ if (f) f.color = col; }}
function _resetFeature(f) {{ _setFeatureColor(f, FEATURE_RESET); }}

(async () => {{
  try {{
    tileset = await Cesium.Cesium3DTileset.fromUrl(TILESET_URL);
    viewer.scene.primitives.add(tileset);
    applyTileOpacity(tileOpacity);
    await viewer.zoomTo(tileset);
  }} catch (e) {{
    console.warn('3D Tiles not loaded:', e.message);
    const msg = document.createElement('div');
    msg.style.cssText = 'position:absolute;bottom:50px;left:50%;transform:translateX(-50%);'
      + 'background:rgba(180,40,40,.85);color:#fff;padding:7px 16px;border-radius:5px;'
      + 'font-size:12px;z-index:200;white-space:nowrap;pointer-events:none';
    msg.textContent = '3D Tiles not loaded — cd output/3dtiles && python serve.py';
    document.body.appendChild(msg);
    setTimeout(() => msg.remove(), 10000);
    flyToNodes();
  }}
}})();

// ── Primitives ─────────────────────────────────────────────────────────────
// One PointPrimitiveCollection per node group → easy group visibility toggle
const pointCollections = {{}};
// Labels (off by default)
const labelColl = viewer.scene.primitives.add(new Cesium.LabelCollection());
// Edges
const lineColl  = viewer.scene.primitives.add(new Cesium.PolylineCollection());

NODES.forEach((n, idx) => {{
  if (!pointCollections[n.group]) {{
    pointCollections[n.group] = viewer.scene.primitives.add(
      new Cesium.PointPrimitiveCollection()
    );
  }}
  pointCollections[n.group].add({{
    position:                 Cesium.Cartesian3.fromDegrees(n.lon, n.lat, n.alt),
    color:                    Cesium.Color.fromCssColorString(n.color),
    pixelSize:                n.pixelSize || 10,
    outlineColor:             Cesium.Color.BLACK.withAlpha(0.5),
    outlineWidth:             1.5,
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
    id: idx,
  }});

  labelColl.add({{
    position:    Cesium.Cartesian3.fromDegrees(n.lon, n.lat, n.alt + 5),
    text:        (n.label || '').split('\\n')[0],
    font:        '11px Segoe UI, sans-serif',
    fillColor:   Cesium.Color.WHITE,
    outlineColor: Cesium.Color.BLACK,
    outlineWidth: 2,
    style:        Cesium.LabelStyle.FILL_AND_OUTLINE,
    verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
    show: false,
  }});
}});

const edgeMat = Cesium.Material.fromType('Color', {{
  color: Cesium.Color.fromCssColorString('#546E7A').withAlpha(0.50),
}});

// Edge labels at midpoint (off by default)
const edgeLabelColl = viewer.scene.primitives.add(new Cesium.LabelCollection());

EDGES.forEach(e => {{
  const n1 = NODES[e.fi], n2 = NODES[e.ti];
  if (!n1 || !n2) return;
  lineColl.add({{
    positions: Cesium.Cartesian3.fromDegreesArrayHeights([
      n1.lon, n1.lat, n1.alt,
      n2.lon, n2.lat, n2.alt,
    ]),
    width:    1.5,
    material: edgeMat,
  }});
  edgeLabelColl.add({{
    position:    Cesium.Cartesian3.fromDegrees(
      (n1.lon + n2.lon) / 2, (n1.lat + n2.lat) / 2, (n1.alt + n2.alt) / 2 + 2
    ),
    text:        e.type,
    font:        '9px Segoe UI, sans-serif',
    fillColor:   Cesium.Color.fromCssColorString('#b0bec5'),
    outlineColor: Cesium.Color.BLACK,
    outlineWidth: 1,
    style:        Cesium.LabelStyle.FILL_AND_OUTLINE,
    verticalOrigin:           Cesium.VerticalOrigin.BOTTOM,
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
    show: false,
  }});
}});

// ── Camera helpers ─────────────────────────────────────────────────────────
function flyToNodes() {{
  if (!NODES.length) return;
  const lons = NODES.map(n => n.lon), lats = NODES.map(n => n.lat);
  viewer.camera.flyTo({{
    destination: Cesium.Rectangle.fromDegrees(
      Math.min(...lons), Math.min(...lats),
      Math.max(...lons), Math.max(...lats)
    ),
  }});
}}

// ── Click → properties panel ───────────────────────────────────────────────
const LABEL_BG = LABEL_COLORS;

function renderPanel(n) {{
  let h = '<div class="prop"><span class="prop-key">Labels</span><div class="prop-val">';
  (n.labels || []).forEach(l => {{
    h += `<span class="chip" style="background:${{LABEL_BG[l] || '#616161'}}">${{l}}</span>`;
  }});
  h += '</div></div>';
  Object.entries(n.props || {{}})
    .sort(([a],[b]) => a.localeCompare(b))
    .forEach(([k, v]) => {{
      h += `<div class="prop"><span class="prop-key">${{k}}</span><div class="prop-val">${{String(v)}}</div></div>`;
    }});
  document.getElementById('info-body').innerHTML = h;
}}

const screenHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);

// ── Click: node primitives take priority; 3D Tile features second ─────────
screenHandler.setInputAction(click => {{
  const picks = viewer.scene.drillPick(click.position);

  // Graph node (point primitive)
  const ptHit = picks.find(p => p.primitive && typeof p.primitive.id === 'number');
  if (ptHit) {{
    renderPanel(NODES[ptHit.primitive.id]);
    // Clear tile selection when clicking a node
    if (selectedFeature) {{ _resetFeature(selectedFeature); selectedFeature = null; }}
    return;
  }}

  // 3D tile feature (individual building via EXT_mesh_features)
  const featHit = picks.find(p => p instanceof Cesium.Cesium3DTileFeature);
  if (featHit) {{
    if (selectedFeature && selectedFeature !== featHit) _resetFeature(selectedFeature);
    selectedFeature = featHit;
    _setFeatureColor(selectedFeature, SELECT_COLOR);
    // Also show the nearest Building node's properties
    const worldPos = viewer.scene.pickPosition(click.position);
    if (worldPos) {{
      const cart = Cesium.Cartographic.fromCartesian(worldPos);
      const lon  = Cesium.Math.toDegrees(cart.longitude);
      const lat  = Cesium.Math.toDegrees(cart.latitude);
      let closest = null, minD = Infinity;
      NODES.forEach((n, i) => {{
        if (n.group !== 'Building') return;
        const d = Math.hypot(n.lon - lon, n.lat - lat);
        if (d < minD) {{ minD = d; closest = i; }}
      }});
      if (closest !== null) renderPanel(NODES[closest]);
    }}
    return;
  }}

  // Empty space — clear selection
  if (selectedFeature) {{ _resetFeature(selectedFeature); selectedFeature = null; }}
  document.getElementById('info-body').innerHTML = '<div class="empty">Click a node</div>';
}}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

// ── Hover: highlight individual building under cursor ─────────────────────
screenHandler.setInputAction(movement => {{
  const picked = viewer.scene.pick(movement.endPosition);
  const feat   = (picked instanceof Cesium.Cesium3DTileFeature) ? picked : null;

  if (feat !== hoveredFeature) {{
    // Reset previous hover (unless it is the selected feature)
    if (hoveredFeature && hoveredFeature !== selectedFeature) _resetFeature(hoveredFeature);
    hoveredFeature = feat;
    if (hoveredFeature && hoveredFeature !== selectedFeature) {{
      _setFeatureColor(hoveredFeature, HOVER_COLOR);
    }}
    viewer.container.style.cursor = feat ? 'pointer' : '';
  }}
}}, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

// ── Controls ───────────────────────────────────────────────────────────────
const opacitySlider = document.getElementById('opacity-slider');
const opacityVal    = document.getElementById('opacity-val');
opacitySlider.addEventListener('input', () => {{
  opacityVal.textContent = opacitySlider.value + '%';
  applyTileOpacity(opacitySlider.value / 100);
}});

let showEdges = true;
document.getElementById('btn-edges').addEventListener('click', e => {{
  showEdges = !showEdges;
  lineColl.show = showEdges;
  e.target.textContent = showEdges ? 'Edges ON' : 'Edges OFF';
  e.target.classList.toggle('off', !showEdges);
}});

let showLabels = false;
document.getElementById('btn-labels').addEventListener('click', e => {{
  showLabels = !showLabels;
  for (let i = 0; i < labelColl.length; i++) labelColl.get(i).show = showLabels;
  e.target.textContent = showLabels ? 'Node Labels ON' : 'Node Labels';
  e.target.classList.toggle('off', !showLabels);
}});

let showEdgeLabels = false;
document.getElementById('btn-edge-labels').addEventListener('click', e => {{
  showEdgeLabels = !showEdgeLabels;
  for (let i = 0; i < edgeLabelColl.length; i++) edgeLabelColl.get(i).show = showEdgeLabels;
  e.target.textContent = showEdgeLabels ? 'Edge Labels ON' : 'Edge Labels';
  e.target.classList.toggle('off', !showEdgeLabels);
}});

document.getElementById('btn-mode').addEventListener('click', () => applyMode(!isDark));

// Legend: click to toggle a node group
const hiddenGroups = new Set();
document.querySelectorAll('.leg-item').forEach(item => {{
  item.addEventListener('click', () => {{
    const g = item.dataset.group;
    hiddenGroups.has(g) ? hiddenGroups.delete(g) : hiddenGroups.add(g);
    item.classList.toggle('off');
    if (pointCollections[g]) pointCollections[g].show = !hiddenGroups.has(g);
  }});
}});
</script>
</body>
</html>
"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Combine 3D Tiles with Neo4j graph nodes/edges in a CesiumJS viewer."
    )
    ap.add_argument(
        "--tileset-url", default="http://localhost:8080/tileset.json",
        help="URL of the 3D Tiles tileset.json (default: http://localhost:8080/tileset.json)",
    )
    ap.add_argument(
        "--output", default="",
        help="Output HTML path (default: output/html/graph_3d_<timestamp>.html)",
    )
    ap.add_argument(
        "--opacity", type=float, default=0.5,
        help="Initial 3D Tiles opacity 0.0–1.0 (default: 0.5)",
    )
    args = ap.parse_args()

    opacity  = max(0.0, min(1.0, args.opacity))
    out_path = Path(args.output) if args.output else \
        Path(f"output/html/graph_3d_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    driver = connect()
    try:
        crs, sx, sy, sz = fetch_dataset(driver)
        raw_nodes  = fetch_all_nodes(driver, jitter_lookups=True)
        raw_edges  = fetch_all_edges(driver)
    finally:
        driver.close()

    if not raw_nodes:
        print("No nodes found — is the graph populated?", file=sys.stderr)
        sys.exit(1)

    # Geoid undulation so node altitudes align with the 3D Tiles ECEF transform
    geoid_m, _ = geoid_undulation(crs, sx, sy, sz)
    print(f"  Geoid offset: {geoid_m:+.3f} m")

    nodes, edges = build_payload(raw_nodes, raw_edges, crs, geoid_m)
    html = build_html(nodes, edges, args.tileset_url, opacity, out_path.stem)
    out_path.write_text(html, encoding="utf-8")
    kb = out_path.stat().st_size // 1024
    print(f"Written -> {out_path}  ({kb} KB, {len(nodes)} nodes, {len(edges)} edges)")
    print()
    print("To view:")
    print(f"  1. cd output/3dtiles && python serve.py   (serves tiles at :8080)")
    print(f"  2. Open {out_path} in a browser")


if __name__ == "__main__":
    console()
    main()
