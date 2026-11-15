#!/usr/bin/env python3
"""
visualize_buildings_3d.py
Reads gml IDs from a CSV, fetches 3D surface geometry from Neo4j,
and writes a self-contained Three.js HTML viewer (works from file://).

Usage:
    py visualize_buildings_3d.py ids.csv
    py visualize_buildings_3d.py ids.csv --radius 100
    py visualize_buildings_3d.py ids.csv --output output/html/my_view.html --title "HafenCity"
    py visualize_buildings_3d.py ids.csv --ref-id DEHHALKA4IB000Cv --radius 100

CSV requirements:
    One column named gmlid / id / building_id / fid / feature_id / gml_id  (auto-detected).
    Optional 'role'  column: reference | neighbour | highlight | <any string>
    Optional 'label' column: custom display label (defaults to the ID)
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

# ── Neo4j connection ──────────────────────────────────────────────────────────
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")

# ── Colour map: role → surface-type → hex ────────────────────────────────────
ROLE_COLORS = {
    "reference": {"WallSurface": "#558b2f", "RoofSurface": "#8bc34a",
                  "GroundSurface": "#33691e", "Surface": "#66bb6a"},
    "neighbour":  {"WallSurface": "#c62828", "RoofSurface": "#ef9a9a",
                   "GroundSurface": "#7f0000", "Surface": "#ef5350"},
    "highlight":  {"WallSurface": "#e65100", "RoofSurface": "#ffcc80",
                   "GroundSurface": "#bf360c", "Surface": "#ff9800"},
}
_PALETTE = [
    {"WallSurface": "#1565c0", "RoofSurface": "#90caf9", "GroundSurface": "#0d47a1", "Surface": "#42a5f5"},
    {"WallSurface": "#6a1b9a", "RoofSurface": "#ce93d8", "GroundSurface": "#4a148c", "Surface": "#ab47bc"},
    {"WallSurface": "#004d40", "RoofSurface": "#80cbc4", "GroundSurface": "#00251a", "Surface": "#26a69a"},
    {"WallSurface": "#4e342e", "RoofSurface": "#bcaaa4", "GroundSurface": "#3e2723", "Surface": "#8d6e63"},
    {"WallSurface": "#880e4f", "RoofSurface": "#f48fb1", "GroundSurface": "#560027", "Surface": "#ec407a"},
]

_ID_COLUMN_NAMES = {"gmlid", "id", "building_id", "fid", "feature_id", "gml_id"}


def _detect_id_col(headers):
    for h in headers:
        if h.strip().lower() in _ID_COLUMN_NAMES:
            return h.strip()
    raise ValueError(
        f"No recognised ID column in: {headers}\n"
        f"Expected one of: {', '.join(sorted(_ID_COLUMN_NAMES))}"
    )


def read_csv(path):
    """Return list of dicts: {id, role, label}."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        id_col    = _detect_id_col(reader.fieldnames or [])
        role_col  = next((h for h in (reader.fieldnames or []) if h.strip().lower() == "role"),  None)
        label_col = next((h for h in (reader.fieldnames or []) if h.strip().lower() == "label"), None)
        for row in reader:
            fid = row[id_col].strip()
            if not fid:
                continue
            rows.append({
                "id":    fid,
                "role":  row[role_col].strip().lower()  if role_col  else "",
                "label": row[label_col].strip()          if label_col else fid,
            })
    return rows


def fetch_geometry(driver, fid):
    """
    Return dict with keys: cx, cy, gz, h, desc, surfaces.
    surfaces = {surface_type: [ring, ...]}  where ring = [[x,y,z], ...]
    Returns None when the feature is not found.
    """
    with driver.session() as session:
        # Feature metadata (Building or CityObject)
        meta = session.run(
            """
            MATCH (f)
            WHERE (f:Building OR f:CityObject) AND f.id = $id
            RETURN f.center_x AS cx,
                   f.center_y AS cy,
                   f.ground_z  AS gz,
                   coalesce(f.measured_height, 0.0) AS h,
                   coalesce(f.description, f.id)    AS desc
            LIMIT 1
            """,
            id=fid,
        ).single()

        if meta is None:
            print(f"  [WARN] not found in graph: {fid}", file=sys.stderr)
            return None

        result = dict(meta)

        # Try boundary surfaces (Building / Bridge / Tunnel / WaterBody)
        rows = session.run(
            """
            MATCH (f {id: $id})-[:HAS_BOUNDARY]->(bs:BoundarySurface)
                  -[:HAS_POLYGON]->(gp:GeometryPolygon)
                  -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
            RETURN bs.surface_type AS stype, gr.pos_list AS pos_list
            """,
            id=fid,
        ).data()

        if not rows:
            # Fallback: generic multi-surface (Road, LandUse, Vegetation, …)
            rows = session.run(
                """
                MATCH (f {id: $id})-[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
                      -[:HAS_SURFACE_MEMBER]->(gp:GeometryPolygon)
                      -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
                RETURN 'Surface' AS stype, gr.pos_list AS pos_list
                """,
                id=fid,
            ).data()

        surfaces = {}
        for row in rows:
            stype = row["stype"] or "Surface"
            vals  = list(map(float, row["pos_list"].split()))
            ring  = [[vals[i], vals[i + 1], vals[i + 2]] for i in range(0, len(vals) - 2, 3)]
            surfaces.setdefault(stype, []).append(ring)

        if not surfaces:
            print(f"  [WARN] no surface geometry for: {fid}", file=sys.stderr)

        result["surfaces"] = surfaces
        return result


def build_scene(csv_rows, geometries, ref_id):
    """Merge CSV metadata with fetched geometry into a list ready for JSON."""
    scene     = []
    pal_index = 0

    for row in csv_rows:
        fid  = row["id"]
        geom = geometries.get(fid)
        if geom is None:
            continue

        role = row["role"] or ("reference" if fid == ref_id else "default")

        if role in ROLE_COLORS:
            colors = ROLE_COLORS[role]
        else:
            colors   = _PALETTE[pal_index % len(_PALETTE)]
            pal_index += 1

        scene.append({
            "id":       fid,
            "label":    row["label"],
            "role":     role,
            "colors":   colors,
            "cx":       geom["cx"],
            "cy":       geom["cy"],
            "gz":       geom["gz"] or 0.0,
            "h":        geom["h"]  or 0.0,
            "desc":     geom["desc"],
            "surfaces": geom["surfaces"],
        })

    return scene


# ── HTML template ─────────────────────────────────────────────────────────────
# Uses @@PLACEHOLDER@@ so JavaScript braces need no escaping.
_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>@@TITLE@@</title>
  <!-- UMD builds: no ES-module CORS issues, works from file:// -->
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/earcut@2.2.4/dist/earcut.dev.js"></script>
  <style>
    * { box-sizing:border-box; margin:0; padding:0; }
    body { overflow:hidden; background:#1a2a3a; font-family:system-ui,sans-serif; }
    #c { display:block; width:100vw; height:100vh; }
    #info {
      position:absolute; top:16px; left:16px;
      background:rgba(10,20,30,.82); color:#eee;
      padding:14px 18px; border-radius:8px; font-size:13px; line-height:1.75;
      max-width:300px; border:1px solid rgba(255,255,255,.1);
    }
    #info strong { color:#fff; font-size:14px; }
    #info .sub   { color:#aaa; font-size:11px; }
    #legend {
      position:absolute; bottom:20px; left:16px;
      background:rgba(10,20,30,.82); color:#ddd;
      padding:11px 16px; border-radius:8px; font-size:12px; line-height:2.1;
      border:1px solid rgba(255,255,255,.1); max-height:50vh; overflow-y:auto;
    }
    .dot { display:inline-block; width:12px; height:12px; border-radius:2px; margin-right:7px; vertical-align:middle; }
    #hint {
      position:absolute; bottom:20px; right:16px;
      background:rgba(10,20,30,.65); color:rgba(255,255,255,.55);
      padding:8px 13px; border-radius:7px; font-size:11px; line-height:1.8; text-align:right;
    }
    #tooltip {
      position:fixed; background:rgba(10,20,30,.92); color:#fff;
      padding:9px 13px; border-radius:6px; font-size:12px; line-height:1.65;
      pointer-events:none; display:none; max-width:280px; z-index:999;
      border:1px solid rgba(255,255,255,.15);
    }
  </style>
</head>
<body>
<canvas id="c"></canvas>

<div id="info">
  <strong>@@TITLE@@</strong><br>
  @@FEATURE_COUNT@@ feature(s)@@REF_LABEL@@<br>
  <span class="sub">@@SUBTITLE@@</span>
</div>

<div id="legend">@@LEGEND_HTML@@</div>

<div id="hint">Left drag&nbsp;·&nbsp;rotate<br>Scroll&nbsp;·&nbsp;zoom<br>Right drag&nbsp;·&nbsp;pan<br>Hover&nbsp;·&nbsp;details</div>
<div id="tooltip"></div>

<script>
var SCENE_DATA = @@SCENE_JSON@@;
var REF_ID     = @@REF_ID_JSON@@;
var RADIUS     = @@RADIUS@@;

// ── Coordinate origin (centroid of feature centres + ref ground Z) ────────────
var OX = 0, OY = 0, OZ = 0;
(function() {
  var n = SCENE_DATA.length;
  if (!n) return;
  SCENE_DATA.forEach(function(f) { OX += f.cx; OY += f.cy; });
  OX /= n; OY /= n;
  var ref = SCENE_DATA.find(function(f) { return f.id === REF_ID; });
  OZ = ref ? ref.gz : SCENE_DATA[0].gz;
})();

// CityGML [X, Y, Z_up] → Three.js [x(east), y(up), z(-north)]
function w2t(p) { return [p[0] - OX, p[2] - OZ, -(p[1] - OY)]; }

// ── Newell normal + earcut triangulation ──────────────────────────────────────
function newellNormal(pts) {
  var nx = 0, ny = 0, nz = 0;
  for (var i = 0; i < pts.length; i++) {
    var p0 = pts[i], p1 = pts[(i + 1) % pts.length];
    nx += (p0[1] - p1[1]) * (p0[2] + p1[2]);
    ny += (p0[2] - p1[2]) * (p0[0] + p1[0]);
    nz += (p0[0] - p1[0]) * (p0[1] + p1[1]);
  }
  return [nx, ny, nz];
}

function triangulatePoly(ring) {
  var raw = ring.slice(0, -1);          // drop closing duplicate vertex
  if (raw.length < 3) return [];
  var pts = raw.map(w2t);
  if (pts.length === 3) return pts[0].concat(pts[1]).concat(pts[2]);

  var n = newellNormal(pts);
  var ax = Math.abs(n[0]), ay = Math.abs(n[1]), az = Math.abs(n[2]);
  var flat2d = [];
  if (ay >= ax && ay >= az)       // horizontal surface → project onto XZ
    for (var i = 0; i < pts.length; i++) flat2d.push(pts[i][0], pts[i][2]);
  else if (ax >= az)              // wall facing X → project onto ZY
    for (var i = 0; i < pts.length; i++) flat2d.push(pts[i][2], pts[i][1]);
  else                            // wall facing Z → project onto XY
    for (var i = 0; i < pts.length; i++) flat2d.push(pts[i][0], pts[i][1]);

  var idx = earcut(flat2d), verts = [];
  for (var i = 0; i < idx.length; i += 3)
    verts = verts.concat(pts[idx[i]], pts[idx[i+1]], pts[idx[i+2]]);
  return verts;
}

// ── Renderer / Scene / Camera / Controls ──────────────────────────────────────
var canvas   = document.getElementById('c');
var renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type    = THREE.PCFSoftShadowMap;

var scene = new THREE.Scene();
scene.background = new THREE.Color(0x6a9ec7);
scene.fog        = new THREE.FogExp2(0x7ab0d0, 0.0014);

var camera = new THREE.PerspectiveCamera(52, window.innerWidth / window.innerHeight, 0.5, 4000);
camera.position.set(160, 110, 200);

var controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.target.set(0, 20, 0);
controls.enableDamping = true;  controls.dampingFactor = 0.07;
controls.minDistance   = 10;    controls.maxDistance   = 2000;
controls.maxPolarAngle = Math.PI / 2 - 0.02;
controls.update();

// ── Lights ────────────────────────────────────────────────────────────────────
scene.add(new THREE.AmbientLight(0xd0e8ff, 0.55));
var sun = new THREE.DirectionalLight(0xfff5e0, 1.3);
sun.position.set(200, 350, 120);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.camera.near  = 1;   sun.shadow.camera.far = 1500;
sun.shadow.camera.left  = sun.shadow.camera.bottom = -400;
sun.shadow.camera.right = sun.shadow.camera.top    =  400;
scene.add(sun);
scene.add(new THREE.HemisphereLight(0x87ceeb, 0x5a8040, 0.45));

// ── Ground / Grid ─────────────────────────────────────────────────────────────
var gnd = new THREE.Mesh(
  new THREE.PlaneGeometry(2000, 2000),
  new THREE.MeshLambertMaterial({ color: 0x5a8a50 })
);
gnd.rotation.x = -Math.PI / 2;  gnd.position.y = -0.15;  gnd.receiveShadow = true;
scene.add(gnd);
scene.add(new THREE.GridHelper(1000, 100, 0x334433, 0x2a3a2a));

// ── Optional radius circle ────────────────────────────────────────────────────
if (RADIUS > 0) {
  var N = 256, rPts = [];
  for (var i = 0; i <= N; i++) {
    var a = (i / N) * Math.PI * 2;
    rPts.push(new THREE.Vector3(Math.cos(a) * RADIUS, 0.4, Math.sin(a) * RADIUS));
  }
  var rGeo = new THREE.BufferGeometry().setFromPoints(rPts);
  var rMat = new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 7, gapSize: 4 });
  var rLine = new THREE.Line(rGeo, rMat);
  rLine.computeLineDistances();
  scene.add(rLine);
  // Centre marker
  scene.add(new THREE.Mesh(
    new THREE.CylinderGeometry(0.4, 0.4, 2, 8),
    new THREE.MeshBasicMaterial({ color: 0xffffff })
  ));
}

// ── Build building meshes ─────────────────────────────────────────────────────
var hoverTargets = [];

SCENE_DATA.forEach(function(bdata) {
  var colors = bdata.colors;

  Object.keys(bdata.surfaces).forEach(function(stype) {
    var polys   = bdata.surfaces[stype];
    var hexStr  = colors[stype] || colors['Surface'] || '#888888';
    var color   = parseInt(hexStr.replace('#', ''), 16);
    var mat     = new THREE.MeshLambertMaterial({ color: color, side: THREE.DoubleSide });

    polys.forEach(function(ring) {
      var verts = triangulatePoly(ring);
      if (!verts.length) return;
      var geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
      geo.computeVertexNormals();
      var mesh = new THREE.Mesh(geo, mat);
      mesh.castShadow    = (stype !== 'GroundSurface');
      mesh.receiveShadow = true;
      mesh.userData = { id: bdata.id, stype: stype, bdata: bdata };
      scene.add(mesh);
      if (stype !== 'GroundSurface') hoverTargets.push(mesh);
    });

    // Edge outlines for walls and roof
    if (stype !== 'GroundSurface') {
      polys.forEach(function(ring) {
        var ep = ring.slice(0, -1).map(function(p) {
          var t = w2t(p); return new THREE.Vector3(t[0], t[1], t[2]);
        });
        ep.push(ep[0].clone());
        var eGeo = new THREE.BufferGeometry().setFromPoints(ep);
        scene.add(new THREE.Line(eGeo,
          new THREE.LineBasicMaterial({ color: parseInt(hexStr.replace('#',''),16), transparent:true, opacity:0.4 })));
      });
    }
  });

  // Connector line from reference centre to each other feature
  if (bdata.role !== 'reference' && REF_ID) {
    var ref = SCENE_DATA.find(function(f) { return f.id === REF_ID; });
    if (ref) {
      var a = w2t([bdata.cx, bdata.cy, bdata.gz]);
      var b = w2t([ref.cx,   ref.cy,   ref.gz]);
      var lGeo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(b[0], 0.5, b[2]),
        new THREE.Vector3(a[0], 0.5, a[2])
      ]);
      var lMat = new THREE.LineDashedMaterial({ color:0xffffff, dashSize:4, gapSize:3, transparent:true, opacity:0.4 });
      var l = new THREE.Line(lGeo, lMat);
      l.computeLineDistances();
      scene.add(l);
    }
  }
});

// ── Auto-fit camera to scene bounding box ────────────────────────────────────
(function() {
  if (!hoverTargets.length) return;
  var box = new THREE.Box3();
  hoverTargets.forEach(function(m) { box.expandByObject(m); });
  if (box.isEmpty()) return;
  var ctr = new THREE.Vector3();
  var sz  = box.getSize(new THREE.Vector3()).length();
  box.getCenter(ctr);
  controls.target.copy(ctr);
  camera.position.set(ctr.x + sz * 0.65, ctr.y + sz * 0.55, ctr.z + sz * 0.75);
  camera.far = sz * 12;
  camera.updateProjectionMatrix();
  controls.update();
})();

// ── Raycasting / tooltip ──────────────────────────────────────────────────────
var raycaster = new THREE.Raycaster();
var mouse     = new THREE.Vector2();
var tooltip   = document.getElementById('tooltip');
var lastId    = null;

window.addEventListener('mousemove', function(e) {
  mouse.x =  (e.clientX / window.innerWidth)  * 2 - 1;
  mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  var hits = raycaster.intersectObjects(hoverTargets, false);
  if (hits.length > 0) {
    var ud = hits[0].object.userData;
    if (ud.id !== lastId) {
      lastId = ud.id;
      var distStr = '';
      if (REF_ID && ud.id !== REF_ID) {
        var ref = SCENE_DATA.find(function(f) { return f.id === REF_ID; });
        if (ref) {
          var dx = ud.bdata.cx - ref.cx, dy = ud.bdata.cy - ref.cy;
          distStr = '<br>Distance from ref: ' + Math.round(Math.sqrt(dx*dx + dy*dy)) + ' m';
        }
      }
      tooltip.innerHTML = '<strong>' + ud.id + '</strong><br>' + ud.bdata.desc + distStr;
    }
    tooltip.style.display = 'block';
    tooltip.style.left    = (e.clientX + 16) + 'px';
    tooltip.style.top     = (e.clientY - 10) + 'px';
  } else {
    tooltip.style.display = 'none';
    lastId = null;
  }
});
canvas.addEventListener('mouseleave', function() { tooltip.style.display = 'none'; lastId = null; });

// ── Resize ────────────────────────────────────────────────────────────────────
window.addEventListener('resize', function() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ── Render loop ───────────────────────────────────────────────────────────────
(function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
})();
</script>
</body>
</html>
"""


def _legend_html(scene_data):
    rows = []
    seen = set()
    for f in scene_data:
        wall_color = f["colors"].get("WallSurface") or f["colors"].get("Surface", "#888")
        label = f["label"] or f["id"]
        key = f["id"]
        if key not in seen:
            seen.add(key)
            rows.append(
                f'<div><span class="dot" style="background:{wall_color}"></span>'
                f'{label} ({f["role"]})</div>'
            )
    return "\n".join(rows)


def render_html(scene_data, ref_id, radius, title, subtitle=""):
    ref_feat  = next((f for f in scene_data if f["id"] == ref_id), None)
    ref_label = f" · ref: {ref_feat['label']}" if ref_feat else ""
    sub       = subtitle or (f"radius: {radius} m" if radius > 0 else "")

    html = _HTML
    html = html.replace("@@TITLE@@",         title)
    html = html.replace("@@FEATURE_COUNT@@", str(len(scene_data)))
    html = html.replace("@@REF_LABEL@@",     ref_label)
    html = html.replace("@@SUBTITLE@@",      sub)
    html = html.replace("@@LEGEND_HTML@@",   _legend_html(scene_data))
    html = html.replace("@@SCENE_JSON@@",    json.dumps(scene_data, separators=(",", ":")))
    html = html.replace("@@REF_ID_JSON@@",   json.dumps(ref_id))
    html = html.replace("@@RADIUS@@",        str(float(radius)))
    return html


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="CSV file with feature IDs")
    p.add_argument("--output",  default="", help="Output HTML path (default: output/html/buildings_3d.html)")
    p.add_argument("--radius",  type=float, default=0.0,
                   help="Draw a dashed circle of this radius (metres) around the reference feature")
    p.add_argument("--ref-id",  dest="ref_id", default="",
                   help="Force this ID as the reference feature (green); default: first row in CSV")
    p.add_argument("--title",   default="", help="Page/panel title")
    p.add_argument("--subtitle", default="", help="Panel subtitle / dataset name")
    return p.parse_args()


def main():
    args = parse_args()

    # ── Read CSV ──────────────────────────────────────────────────────────────
    print(f"Reading {args.csv} …")
    try:
        csv_rows = read_csv(args.csv)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"[ERROR] {e}")

    if not csv_rows:
        sys.exit("[ERROR] CSV contains no feature IDs")

    print(f"  {len(csv_rows)} ID(s) found")

    # Determine reference ID
    ref_id = args.ref_id.strip() or csv_rows[0]["id"]

    # ── Fetch geometry from Neo4j ─────────────────────────────────────────────
    print(f"Connecting to Neo4j at {NEO4J_URI} …")
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    geometries = {}
    t0 = time.perf_counter()
    for row in csv_rows:
        fid = row["id"]
        print(f"  fetching {fid} …", end=" ", flush=True)
        geom = fetch_geometry(driver, fid)
        if geom:
            geometries[fid] = geom
            stypes = list(geom["surfaces"].keys())
            print(f"OK ({', '.join(stypes) or 'no geometry'})")
        else:
            print("SKIP")

    driver.close()
    print(f"  Geometry fetched in {time.perf_counter() - t0:.2f} s")

    if not geometries:
        sys.exit("[ERROR] No geometry found for any of the supplied IDs")

    # ── Build scene data ──────────────────────────────────────────────────────
    scene_data = build_scene(csv_rows, geometries, ref_id)

    title = args.title or f"3D View — {len(scene_data)} feature(s)"

    # ── Render HTML ───────────────────────────────────────────────────────────
    html = render_html(
        scene_data=scene_data,
        ref_id=ref_id,
        radius=args.radius,
        title=title,
        subtitle=args.subtitle,
    )

    # ── Write output ──────────────────────────────────────────────────────────
    out_path = args.output.strip() or "output/html/buildings_3d.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nSaved → {out_path.resolve()}")
    print("Open the file in a browser to view the 3D model.")


if __name__ == "__main__":
    main()
