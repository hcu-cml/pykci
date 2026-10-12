#!/usr/bin/env python3
"""Export building footprints colour-coded by ML-predicted roof material → HTML map.

Reads building ground-surface footprints and their ``predictedroofmaterial``
straight from the Neo4j knowledge graph (no GML needed), reprojects the
footprints to WGS84, and writes a single self-contained Leaflet HTML file with
polygons coloured by roof material, an interactive legend, and per-building
popups.  The palette matches ``build_3dtiles.py --color-by roofmat`` (the
``soft`` teaser palette) so the 2D map and the 3D teaser read as one system.

Provenance-aware: ``predictedroofmaterial`` is an ML prediction (namespaced,
never authoritative) — the popup labels it as *predicted* and buildings without
a prediction get a neutral "no prediction" coat rather than being dropped.

Run:
    py export_roofmat_footprints.py [--dataset NAME] [--output PATH]
                                    [--source-crs EPSG:N] [--basemap voyager|osm]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from neo4j import GraphDatabase
from pyproj import Transformer

NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")

# Roof-material palette — the 'soft' palette from build_3dtiles.py, as hex.
ROOFMAT_COLORS = {
    "metal":      "#5c8fb5",   # steel blue
    "concrete":   "#998f80",   # warm grey
    "tar_paper":  "#666b80",   # slate blue-grey
    "glass":      "#57bdbd",   # teal
    "roof_tiles": "#d9805c",   # terracotta
}
ROOFMAT_ORDER = ["metal", "concrete", "tar_paper", "glass", "roof_tiles"]
ROOFMAT_LABEL = {
    "metal": "Metal", "concrete": "Concrete", "tar_paper": "Tar paper",
    "glass": "Glass", "roof_tiles": "Roof tiles",
}
DEFAULT_COLOR = "#b8afa3"      # neutral — no prediction
DEFAULT_KEY = "unknown"

BASEMAPS = {
    "voyager": {
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        "attribution": '&copy; OpenStreetMap contributors &copy; CARTO',
    },
    "osm": {
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": '&copy; OpenStreetMap contributors',
    },
}


def horiz_epsg_from_srs(srs_name: str) -> str:
    """Map a CityGML srsName to a horizontal EPSG code (subset of build_3dtiles)."""
    m = re.search(r"EPSG[:\s]+(\d+)", srs_name)
    if m:
        return f"EPSG:{m.group(1)}"
    m = re.search(r"ETRS89_UTM(\d+)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{25800 + int(m.group(1))}"
    m = re.search(r"GK(\d)", srs_name, re.IGNORECASE)
    if m:
        return f"EPSG:{31464 + int(m.group(1))}"
    return srs_name


def pos_list_to_xy(pos_str: str, srs_dim: int):
    """Parse a GML pos_list into a list of (x, y) tuples, dropping z."""
    toks = pos_str.split()
    step = srs_dim if srs_dim in (2, 3) else 3
    pts = []
    for i in range(0, len(toks) - step + 1, step):
        try:
            pts.append((float(toks[i]), float(toks[i + 1])))
        except ValueError:
            continue
    return pts


def fetch_footprints(sess, source: str):
    """Yield {id, rm, height, rings:[[(x,y)…]…]} per building (ground surfaces)."""
    rows = sess.run("""
        MATCH (b:Building)-[:PART_OF]->(d:Dataset {source: $src})
        MATCH (b)-[:HAS_BOUNDARY]->(bs:BoundarySurface {surface_type:'GroundSurface'})
              -[:HAS_POLYGON]->(p:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        WHERE r.pos_list IS NOT NULL AND r.pos_list <> ''
        RETURN b.id AS id,
               b.predictedroofmaterial AS rm,
               b.measured_height AS height,
               collect(r.pos_list) AS rings,
               head(collect(coalesce(r.srs_dimension, 3))) AS dim
    """, src=source)
    for row in rows:
        yield {
            "id": row["id"],
            "rm": row["rm"],
            "height": row["height"],
            "rings": row["rings"],
            "dim": int(row["dim"] or 3),
        }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", help="Dataset source name (default: the only dataset present)")
    ap.add_argument("--output", default="output/html/roofmat_footprints.html",
                    help="Output HTML path (default: output/html/roofmat_footprints.html)")
    ap.add_argument("--source-crs", help="Override footprint CRS (e.g. EPSG:25832); "
                                         "default: derived from Dataset.crs")
    ap.add_argument("--basemap", choices=sorted(BASEMAPS), default="voyager",
                    help="Basemap tiles (default: voyager)")
    args = ap.parse_args(argv)

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    with driver.session() as sess:
        sources = [r["s"] for r in sess.run("MATCH (d:Dataset) RETURN d.source AS s")]
        if not sources:
            sys.exit("No Dataset found in the graph. Ingest a CityGML file first.")
        source = args.dataset
        if source is None:
            if len(sources) > 1:
                sys.exit("Multiple datasets present; pass --dataset NAME. "
                         f"Available: {', '.join(sources)}")
            source = sources[0]
        elif source not in sources:
            sys.exit(f"Dataset '{source}' not found. Available: {', '.join(sources)}")

        dset = dict(sess.run("MATCH (d:Dataset {source:$s}) RETURN d",
                             s=source).single()["d"])
        src_crs = args.source_crs or horiz_epsg_from_srs(dset.get("crs", "") or "")
        print(f"Dataset : {source}")
        print(f"Source CRS: {src_crs} (from '{dset.get('crs')}')")

        try:
            transformer = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
        except Exception as exc:
            sys.exit(f"Cannot build transformer {src_crs} → EPSG:4326: {exc}")

        features = []
        counts = {k: 0 for k in ROOFMAT_ORDER}
        counts[DEFAULT_KEY] = 0
        for b in fetch_footprints(sess, source):
            rings_ll = []
            for pos in b["rings"]:
                xy = pos_list_to_xy(pos, b["dim"])
                if len(xy) < 3:
                    continue
                ring = [list(transformer.transform(x, y)) for x, y in xy]  # (lon, lat)
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                rings_ll.append(ring)
            if not rings_ll:
                continue
            rm = b["rm"] if b["rm"] in ROOFMAT_COLORS else None
            counts[rm if rm else DEFAULT_KEY] += 1
            features.append({
                "type": "Feature",
                "geometry": {"type": "MultiPolygon",
                             "coordinates": [[r] for r in rings_ll]},
                "properties": {
                    "id": b["id"],
                    "roofmat": rm or "",
                    "color": ROOFMAT_COLORS.get(rm, DEFAULT_COLOR),
                    "height": b["height"],
                },
            })
    driver.close()

    if not features:
        sys.exit("No building footprints with ground surfaces found for this dataset.")

    fc = {"type": "FeatureCollection", "features": features}
    total = len(features)
    print(f"Footprints: {total}")
    for k in ROOFMAT_ORDER:
        print(f"  {ROOFMAT_LABEL[k]:<11}: {counts[k]}")
    if counts[DEFAULT_KEY]:
        print(f"  {'No prediction':<11}: {counts[DEFAULT_KEY]}")

    legend_rows = "".join(
        f'<div class="row"><span class="sw" style="background:{ROOFMAT_COLORS[k]}"></span>'
        f'{ROOFMAT_LABEL[k]} <span class="n">{counts[k]}</span></div>'
        for k in ROOFMAT_ORDER if counts[k]
    )
    if counts[DEFAULT_KEY]:
        legend_rows += (
            f'<div class="row"><span class="sw" style="background:{DEFAULT_COLOR}"></span>'
            f'No prediction <span class="n">{counts[DEFAULT_KEY]}</span></div>'
        )

    bm = BASEMAPS[args.basemap]
    html = HTML_TEMPLATE.format(
        title=f"Predicted roof materials — {dset.get('name', source)}",
        subtitle=f"{total} buildings · {dset.get('lod','')} · source: CityGML",
        geojson=json.dumps(fc),
        legend_rows=legend_rows,
        tile_url=bm["url"],
        attribution=bm["attribution"],
    )

    out = args.output
    import os
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; font-family: system-ui, sans-serif; }}
  #map {{ position: absolute; inset: 0; }}
  .panel {{
    position: absolute; z-index: 1000; background: rgba(255,255,255,.94);
    padding: 10px 14px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,.25);
    font-size: 13px; line-height: 1.45;
  }}
  #title {{ top: 12px; left: 52px; max-width: 340px; }}
  #title b {{ color: #b45309; }}
  #title .sub {{ color: #6b7280; font-size: 11px; }}
  #legend {{ bottom: 24px; right: 12px; }}
  #legend .hd {{ font-weight: 600; margin-bottom: 6px; }}
  #legend .row {{ display: flex; align-items: center; gap: 8px; margin: 3px 0; }}
  #legend .sw {{ width: 16px; height: 16px; border-radius: 3px;
                 border: 1px solid rgba(0,0,0,.25); display: inline-block; }}
  #legend .n {{ color: #6b7280; margin-left: auto; font-variant-numeric: tabular-nums; }}
  .leaflet-popup-content {{ font-size: 12px; }}
  .leaflet-popup-content .k {{ color: #6b7280; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="title" class="panel">
  <b>Predicted roof materials</b><br>
  <span class="sub">{subtitle}<br>Colour: ML-predicted roof material</span>
</div>
<div id="legend" class="panel">
  <div class="hd">Roof material</div>
  {legend_rows}
</div>
<script>
const DATA = {geojson};
const map = L.map('map');
L.tileLayer('{tile_url}', {{ attribution: '{attribution}', maxZoom: 20 }}).addTo(map);
const layer = L.geoJSON(DATA, {{
  style: f => ({{
    color: '#333', weight: 0.6, fillColor: f.properties.color, fillOpacity: 0.78,
  }}),
  onEachFeature: (f, l) => {{
    const p = f.properties;
    const rm = p.roofmat ? p.roofmat.replace('_', ' ') : '—';
    const h = (p.height != null) ? p.height + ' m' : '—';
    l.bindPopup(
      '<b>' + p.id + '</b><br>' +
      '<span class="k">predicted roof:</span> ' + rm + '<br>' +
      '<span class="k">height:</span> ' + h
    );
    l.on('mouseover', () => l.setStyle({{ weight: 2, color: '#111' }}));
    l.on('mouseout',  () => layer.resetStyle(l));
  }},
}}).addTo(map);
map.fitBounds(layer.getBounds(), {{ padding: [30, 30] }});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
