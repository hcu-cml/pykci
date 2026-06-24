#!/usr/bin/env python3
"""
export_graph_html.py
Queries Neo4j and exports the property graph as a self-contained HTML viewer
using vis-network (CDN). Works from file:// without a server.

By default every node and edge is exported. Use --skip-geometry to exclude
geometry leaf nodes (GeometryRing, GeometryPolygon, GeometrySolid,
GeometryMultiSurface, GeometryLineString, TerrainIntersection), which are
too numerous to be useful at the graph overview level.

Usage:
    py export_graph_html.py
    py export_graph_html.py --output output/html/mygraph.html
    py export_graph_html.py --limit 500
    py export_graph_html.py --skip-geometry
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from neo4j import GraphDatabase

NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")

GEOMETRY_LABELS = frozenset({
    "GeometryRing", "GeometryPolygon", "GeometrySolid",
    "GeometryMultiSurface", "GeometryLineString", "TerrainIntersection",
})

# Properties too large / not useful to embed in the viewer
_SKIP_PROPS = frozenset({
    "pos_list", "embedding", "embedding_ollama", "bbox_wkt",
})

# (background, border, font) per label
_COLORS = {
    "Dataset":              ("#1565c0", "#0d47a1", "#ffffff"),
    "Building":             ("#e65100", "#bf360c", "#ffffff"),
    "BoundarySurface":      ("#ef6c00", "#e65100", "#ffffff"),
    "District":             ("#2e7d32", "#1b5e20", "#ffffff"),
    "City":                 ("#1b5e20", "#003300", "#ffffff"),
    "BuildingFunction":     ("#6a1b9a", "#4a148c", "#ffffff"),
    "RoofType":             ("#880e4f", "#560027", "#ffffff"),
    "CityObject":           ("#00695c", "#004d40", "#ffffff"),
    "CityObjectGroup":      ("#004d40", "#00251a", "#ffffff"),
    "GeometryPolygon":      ("#78909c", "#546e7a", "#ffffff"),
    "GeometryRing":         ("#90a4ae", "#78909c", "#000000"),
    "GeometrySolid":        ("#607d8b", "#455a64", "#ffffff"),
    "GeometryMultiSurface": ("#546e7a", "#37474f", "#ffffff"),
    "GeometryLineString":   ("#b0bec5", "#90a4ae", "#000000"),
    "TerrainIntersection":  ("#cfd8dc", "#b0bec5", "#000000"),
}
_DEFAULT_COLOR = ("#9e9e9e", "#616161", "#ffffff")


def _node_label(labels, props):
    if "Dataset" in labels:
        return props.get("name", props.get("id", "Dataset"))
    if "City" in labels or "District" in labels:
        return props.get("name", props.get("id", labels[0] if labels else "?"))
    if "Building" in labels:
        return (props.get("id") or "")[:20]
    if "BoundarySurface" in labels:
        return props.get("surface_type", "Surface")
    if "BuildingFunction" in labels:
        return props.get("name", props.get("code", "Function"))
    if "RoofType" in labels:
        return props.get("name", props.get("code", "RoofType"))
    if "CityObject" in labels:
        ft  = props.get("feature_type", "")
        oid = (props.get("id") or "")[:12]
        return f"{ft}\n{oid}" if ft else oid
    return (props.get("name") or props.get("id") or (labels[0] if labels else "?"))[:24]


def fetch_graph(driver, skip_labels, limit):
    limit_str = str(limit) if limit else "unlimited"
    print(f"Fetching nodes ({limit_str}, skipping geometry={bool(skip_labels)})…", flush=True)
    t0 = time.perf_counter()

    query = """
        MATCH (n)
        WHERE NOT any(lbl IN labels(n) WHERE lbl IN $skip)
        RETURN elementId(n)   AS eid,
               labels(n)      AS labels,
               properties(n)  AS props
        """
    if limit:
        query += f" LIMIT {int(limit)}"

    with driver.session() as session:
        raw_nodes = session.run(query, skip=list(skip_labels)).data()

    print(f"  {len(raw_nodes)} nodes ({time.perf_counter()-t0:.2f}s)", flush=True)

    eids = [r["eid"] for r in raw_nodes]
    t1 = time.perf_counter()

    with driver.session() as session:
        raw_rels = session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE elementId(a) IN $eids AND elementId(b) IN $eids
            RETURN elementId(r)   AS eid,
                   elementId(a)   AS from_eid,
                   elementId(b)   AS to_eid,
                   type(r)        AS rel_type,
                   properties(r)  AS props
            """,
            eids=eids,
        ).data()

    print(f"  {len(raw_rels)} relationships ({time.perf_counter()-t1:.2f}s)", flush=True)

    nodes, edges = [], []

    for r in raw_nodes:
        props  = {k: v for k, v in r["props"].items() if k not in _SKIP_PROPS}
        labs   = r["labels"]
        primary = labs[0] if labs else "Unknown"
        bg, border, font = _COLORS.get(primary, _DEFAULT_COLOR)
        nodes.append({
            "id":     r["eid"],
            "label":  _node_label(labs, props),
            "title":  primary,
            "group":  primary,
            "color":  {"background": bg, "border": border},
            "font":   {"color": font},
            "labels": labs,
            "props":  props,
        })

    for r in raw_rels:
        edges.append({
            "id":    r["eid"],
            "from":  r["from_eid"],
            "to":    r["to_eid"],
            "label": r["rel_type"],
            "arrows": "to",
            "font":  {"size": 9, "align": "middle", "color": "#888"},
            "color": {"color": "#666", "highlight": "#ccc", "hover": "#aaa"},
            "props": r["props"],
        })

    return nodes, edges


def build_html(nodes, edges, title):
    all_labels = sorted({lab for n in nodes for lab in n["labels"]})

    legend_rows = []
    for lab in all_labels:
        bg, border, _ = _COLORS.get(lab, _DEFAULT_COLOR)
        legend_rows.append(
            f'<div class="leg-item" data-label="{lab}">'
            f'<span class="leg-dot" style="background:{bg};border-color:{border}"></span>'
            f'<span>{lab}</span>'
            f'</div>'
        )
    legend_html = "\n".join(legend_rows)

    nodes_json = json.dumps(nodes, ensure_ascii=False, default=str)
    edges_json = json.dumps(edges, ensure_ascii=False, default=str)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_nodes, n_edges = len(nodes), len(edges)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/dist/vis-network.min.js"></script>
<style>
/* ── Theme tokens ─────────────────────────────────────────────────────── */
:root {{
  --bg:           #1a1a2e;
  --panel:        #16213e;
  --border:       #0f3460;
  --text:         #e0e0e0;
  --text-muted:   #546e7a;
  --text-faint:   #37474f;
  --input-bg:     #0f3460;
  --input-border: #1565c0;
  --btn-bg:       #1565c0;
  --btn-hover:    #1976d2;
  --h1:           #90caf9;
  --leg-hover:    #0f3460;
  --canvas-bg:    #0d1117;
  --edge:         #555;
  --edge-hi:      #bbb;
  --edge-font:    #777;
}}
:root.light {{
  --bg:           #f4f6f8;
  --panel:        #ffffff;
  --border:       #dde1e7;
  --text:         #1c1c1e;
  --text-muted:   #6b7280;
  --text-faint:   #c0c4cc;
  --input-bg:     #eef0f3;
  --input-border: #1565c0;
  --btn-bg:       #1565c0;
  --btn-hover:    #1976d2;
  --h1:           #1565c0;
  --leg-hover:    #eef0f3;
  --canvas-bg:    #f9fafb;
  --edge:         #aaa;
  --edge-hi:      #333;
  --edge-font:    #888;
}}
/* ── Layout ───────────────────────────────────────────────────────────── */
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;
     background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;
     transition:background .2s,color .2s}}
#topbar{{background:var(--panel);padding:8px 12px;display:flex;align-items:center;gap:8px;
         border-bottom:1px solid var(--border);flex-shrink:0;
         transition:background .2s,border-color .2s}}
#topbar h1{{font-size:14px;font-weight:600;color:var(--h1);margin-right:auto;white-space:nowrap;
            transition:color .2s}}
#search{{background:var(--input-bg);border:1px solid var(--input-border);border-radius:4px;
         color:var(--text);padding:4px 8px;width:200px;font-size:12px;
         transition:background .2s,color .2s}}
#search::placeholder{{color:var(--text-muted)}}
.btn{{background:var(--btn-bg);border:none;border-radius:4px;color:#fff;
      padding:4px 10px;cursor:pointer;font-size:12px;white-space:nowrap}}
.btn:hover{{background:var(--btn-hover)}}
#btn-theme{{background:transparent;border:1px solid var(--border);color:var(--text);
            font-size:14px;padding:3px 8px;border-radius:4px;cursor:pointer;line-height:1}}
#btn-theme:hover{{background:var(--leg-hover)}}
.stat{{font-size:11px;color:var(--text-muted);white-space:nowrap}}
#main{{display:flex;flex:1;overflow:hidden}}
#sidebar{{width:200px;background:var(--panel);border-right:1px solid var(--border);
          display:flex;flex-direction:column;flex-shrink:0;overflow:hidden;
          transition:background .2s,border-color .2s}}
.panel-title{{font-size:10px;font-weight:600;color:var(--text-muted);padding:8px 10px;
              text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);
              flex-shrink:0}}
#legend{{overflow-y:auto;padding:6px;flex:1}}
.leg-item{{display:flex;align-items:center;gap:6px;padding:3px 5px;border-radius:4px;
           cursor:pointer;user-select:none}}
.leg-item:hover{{background:var(--leg-hover)}}
.leg-item.off{{opacity:.3}}
.leg-dot{{width:11px;height:11px;border-radius:50%;border:2px solid;flex-shrink:0}}
#network-wrap{{flex:1;background:var(--canvas-bg);transition:background .2s}}
#network{{width:100%;height:100%}}
#info-panel{{width:270px;background:var(--panel);border-left:1px solid var(--border);
             display:flex;flex-direction:column;flex-shrink:0;overflow:hidden;
             transition:background .2s,border-color .2s}}
#info-body{{overflow-y:auto;padding:10px;flex:1}}
.prop{{margin-bottom:8px}}
.prop-key{{font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.4px}}
.prop-val{{font-size:12px;color:var(--text);word-break:break-all;margin-top:1px}}
.chip{{display:inline-block;border-radius:3px;padding:1px 6px;font-size:10px;margin:1px;color:#fff}}
.empty{{color:var(--text-faint);font-size:12px;padding:10px}}
</style>
</head>
<body>
<div id="topbar">
  <h1>Neo4j Graph &mdash; {title}</h1>
  <input id="search" type="text" placeholder="Search node…">
  <button class="btn" id="btn-physics">Physics ON</button>
  <button class="btn" id="btn-fit">Fit</button>
  <button id="btn-theme" title="Toggle light / dark mode">🌙</button>
  <span class="stat">{n_nodes} nodes &middot; {n_edges} edges &middot; {ts}</span>
</div>
<div id="main">
  <div id="sidebar">
    <div class="panel-title">Labels</div>
    <div id="legend">{legend_html}</div>
  </div>
  <div id="network-wrap"><div id="network"></div></div>
  <div id="info-panel">
    <div class="panel-title">Properties</div>
    <div id="info-body"><div class="empty">Click a node or edge</div></div>
  </div>
</div>
<script>
const NODES_DATA = {nodes_json};
const EDGES_DATA = {edges_json};

const dsNodes = new vis.DataSet(NODES_DATA);
const dsEdges = new vis.DataSet(EDGES_DATA);

// ── Theme ──────────────────────────────────────────────────────────────────
const THEMES = {{
  dark:  {{ edge: '#555', edgeHi: '#bbb', edgeFont: '#777', bg: '#0d1117' }},
  light: {{ edge: '#aaa', edgeHi: '#333', edgeFont: '#888', bg: '#f9fafb' }},
}};
let isDark = true;

function applyEdgeTheme(t) {{
  dsEdges.update(EDGES_DATA.map(e => ({{
    id:    e.id,
    font:  {{size: 9, align: 'middle', color: t.edgeFont}},
    color: {{color: t.edge, highlight: t.edgeHi, hover: t.edgeHi}},
  }})));
}}

document.getElementById('btn-theme').addEventListener('click', () => {{
  isDark = !isDark;
  const root = document.documentElement;
  root.classList.toggle('light', !isDark);
  document.getElementById('btn-theme').textContent = isDark ? '🌙' : '☀️';
  applyEdgeTheme(isDark ? THEMES.dark : THEMES.light);
}});

// ── Network ────────────────────────────────────────────────────────────────
const network = new vis.Network(
  document.getElementById('network'),
  {{nodes: dsNodes, edges: dsEdges}},
  {{
    physics: {{
      enabled: true,
      solver: 'forceAtlas2Based',
      forceAtlas2Based: {{
        gravitationalConstant: -60,
        centralGravity: 0.005,
        springLength: 180,
        springConstant: 0.05,
        damping: 0.4,
      }},
      stabilization: {{iterations: 300, updateInterval: 30}},
    }},
    nodes: {{
      shape: 'dot',
      size: 12,
      borderWidth: 2,
      borderWidthSelected: 3,
      font: {{size: 11, face: 'Segoe UI'}},
    }},
    edges: {{
      smooth: {{type: 'continuous', roundness: 0.2}},
      arrows: {{to: {{enabled: true, scaleFactor: 0.5}}}},
      selectionWidth: 2,
    }},
    interaction: {{
      hover: true,
      tooltipDelay: 300,
      keyboard: {{enabled: true, speed: {{x:10, y:10, zoom:0.02}}}},
    }},
  }}
);

// Apply initial dark-mode edge colours
applyEdgeTheme(THEMES.dark);

// Auto-stop physics after stabilization
network.once('stabilizationIterationsDone', () => {{
  network.setOptions({{physics: {{enabled: false}}}});
  document.getElementById('btn-physics').textContent = 'Physics OFF';
}});

// ── Properties panel ───────────────────────────────────────────────────────
const LABEL_BG = {{
  Dataset:'#1565c0', Building:'#e65100', BoundarySurface:'#ef6c00',
  District:'#2e7d32', City:'#1b5e20', BuildingFunction:'#6a1b9a',
  RoofType:'#880e4f', CityObject:'#00695c', CityObjectGroup:'#004d40',
}};

function renderNodePanel(nodeId) {{
  const n = dsNodes.get(nodeId);
  if (!n) return;
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

function renderEdgePanel(edgeId) {{
  const e = dsEdges.get(edgeId);
  if (!e) return;
  let h = `<div class="prop"><span class="prop-key">Type</span><div class="prop-val">
    <span class="chip" style="background:#455a64">${{e.label}}</span></div></div>`;
  const props = e.props || {{}};
  Object.entries(props).sort(([a],[b]) => a.localeCompare(b)).forEach(([k, v]) => {{
    h += `<div class="prop"><span class="prop-key">${{k}}</span><div class="prop-val">${{String(v)}}</div></div>`;
  }});
  if (!Object.keys(props).length)
    h += '<div class="prop"><div class="prop-val" style="color:var(--text-faint)">No properties</div></div>';
  document.getElementById('info-body').innerHTML = h;
}}

network.on('selectNode', p => {{ if (p.nodes.length) renderNodePanel(p.nodes[0]); }});
network.on('selectEdge', p => {{ if (p.edges.length && !p.nodes.length) renderEdgePanel(p.edges[0]); }});
network.on('deselectNode', () => {{
  document.getElementById('info-body').innerHTML = '<div class="empty">Click a node or edge</div>';
}});

// ── Controls ───────────────────────────────────────────────────────────────
let physicsOn = true;
document.getElementById('btn-physics').addEventListener('click', () => {{
  physicsOn = !physicsOn;
  network.setOptions({{physics: {{enabled: physicsOn}}}});
  document.getElementById('btn-physics').textContent = physicsOn ? 'Physics ON' : 'Physics OFF';
}});

document.getElementById('btn-fit').addEventListener('click', () => network.fit());

document.getElementById('search').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase().trim();
  if (!q) {{ network.unselectAll(); return; }}
  const hit = NODES_DATA.find(n =>
    String(n.label || '').toLowerCase().includes(q) ||
    Object.values(n.props || {{}}).some(v => String(v).toLowerCase().includes(q))
  );
  if (hit) {{
    network.selectNodes([hit.id]);
    network.focus(hit.id, {{scale: 1.4, animation: {{duration: 500, easingFunction: 'easeInOutQuad'}}}});
    renderNodePanel(hit.id);
  }}
}});

// Label visibility toggle
const hiddenLabels = new Set();
document.querySelectorAll('.leg-item').forEach(item => {{
  item.addEventListener('click', () => {{
    const lbl = item.dataset.label;
    hiddenLabels.has(lbl) ? hiddenLabels.delete(lbl) : hiddenLabels.add(lbl);
    item.classList.toggle('off');
    dsNodes.update(NODES_DATA.map(n => ({{
      id: n.id,
      hidden: (n.labels || []).some(l => hiddenLabels.has(l)),
    }})));
  }});
}});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Export Neo4j graph to self-contained HTML viewer.")
    ap.add_argument("--output", default="",
                    help="Output path (default: output/html/graph_<timestamp>.html)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Max nodes to fetch (default: no limit)")
    ap.add_argument("--skip-geometry", action="store_true",
                    help="Skip geometry leaf nodes (GeometryRing, GeometryPolygon, …)")
    args = ap.parse_args()

    skip_labels = set(GEOMETRY_LABELS) if args.skip_geometry else set()

    out_path = Path(args.output) if args.output else \
        Path(f"output/html/graph_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    try:
        nodes, edges = fetch_graph(driver, skip_labels, args.limit)
    finally:
        driver.close()

    if not nodes:
        print("No nodes returned — is the graph populated?", file=sys.stderr)
        sys.exit(1)

    html = build_html(nodes, edges, out_path.stem)
    out_path.write_text(html, encoding="utf-8")
    kb = out_path.stat().st_size // 1024
    print(f"Written → {out_path}  ({kb} KB, {len(nodes)} nodes, {len(edges)} edges)")


if __name__ == "__main__":
    main()
