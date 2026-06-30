#!/usr/bin/env python3
"""
ingest_osm.py — OpenStreetMap (.osm.pbf) → pykci knowledge graph enrichment.

Enriches an existing pykci graph (built from CityGML 2.0 / ALKIS by
``ingest_citygml.py``) with OpenStreetMap data over the same area:

  * one ``(:OsmFeature)`` node per OSM object (the provenance "hint"), carrying all
    tags + geometry, reprojected into the dataset CRS and registered in a dedicated
    ``osm_features`` R-tree layer;
  * ``(:Building|:CityObject)-[:ENRICHED_BY {overlap_ratio, jaccard, is_primary,
    match_type}]->(:OsmFeature)`` links, resolved across the 1:1 / 1:n / n:1 / n:m
    footprint cases via an overlap-graph + connected-component method;
  * curated ``osm_*`` scalars copied onto enriched pykci nodes (never overwriting
    authoritative attributes);
  * point POIs attached to their containing building via ``HAS_POI``.

See OSM_INTEGRATION.md for the full concept. Mirrors ingest_citygml.py conventions:
same Neo4j driver/auth, ``UNWIND … MERGE … SET n += row, n:Entity`` batched writes,
``IF NOT EXISTS`` indexes, APOC dynamic labels with a no-APOC fallback. Idempotent
(MERGE on stable ``osm:<type>/<id>`` ids).

Geometry is read via the ``ogr2ogr`` CLI (clip + reproject + tag-filter to GeoJSON),
then parsed with ``json`` + ``shapely`` — no GDAL Python binding required.

Usage:
    python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf
    python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf --dataset hamburg_hafencity
    python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf --buffer 50 --overlap-threshold 0.5
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase
from pyproj import Transformer
from shapely.geometry import shape, Point
from shapely.ops import unary_union
from shapely.prepared import prep

NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")
BATCH_SIZE = 1000

# ── OSM-kind classification ────────────────────────────────────────────────────
# Maps OSM tags → (osm_kind, dynamic label, pykci target label, target feature_type)
# pykci target = the CityGML feature type whose footprints this kind is matched against.
# A None target means "no pykci geometric counterpart" (POIs, most trees) → enrich the
# containing building (POIs) or keep standalone.

KIND_BUILDING   = "building"
KIND_WATER      = "water"
KIND_LANDUSE    = "landuse"
KIND_VEGETATION = "vegetation"
KIND_ROAD       = "road"
KIND_POI        = "poi"
KIND_FURNITURE  = "furniture"
KIND_OTHER      = "other"

# dynamic Neo4j label + pykci match target per kind
KIND_LABEL = {
    KIND_BUILDING:   ("OsmBuilding",   "Building",   None),
    KIND_WATER:      ("OsmWater",      "CityObject", "WaterBody"),
    KIND_LANDUSE:    ("OsmLandUse",    "CityObject", "LandUse"),
    KIND_VEGETATION: ("OsmVegetation", "CityObject", "PlantCover"),
    KIND_ROAD:       ("OsmRoad",       "CityObject", "Road"),
    KIND_POI:        ("OsmPOI",        "Building",   None),
    KIND_FURNITURE:  ("OsmFurniture",  "CityObject", "CityFurniture"),
    KIND_OTHER:      ("OsmFeature",    None,         None),
}

# Tags lifted onto stable promoted keys on the OsmFeature node.
PROMOTED_TAGS = [
    "name", "building", "amenity", "shop", "office", "tourism", "leisure",
    "landuse", "natural", "man_made", "highway", "railway", "waterway", "water",
    "height", "building:levels", "building:levels:underground",
    "roof:shape", "roof:material", "start_date", "operator",
]

# Direct correspondences to authoritative pykci Building attributes (for reference / queries).
DIRECT_CORRESPONDENCE = {
    "height": "measured_height",
    "building:levels": "storeys_above_ground",
    "building:levels:underground": "storeys_below_ground",
    "roof:shape": "roof_type_code",
    "roof:material": "predicted_roof_material",
    "start_date": "year_of_construction",
}

# Shared-thematic keys safe to replicate onto all pykci buildings in a 1:n match.
SHARED_THEMATIC = {
    "osm_building", "name", "osm_amenity", "osm_height", "osm_building_levels",
    "osm_roof_shape", "osm_roof_material", "osm_operator",
    "addr_street", "addr_postcode", "addr_city",
}

# All osm_* enrichment keys written onto pykci Building/CityObject nodes — used by
# reset_osm() to strip prior enrichment for a clean idempotent re-run. (`name` is the
# OSM key; on the pykci node it lands as `osm_name`, so `name` is NOT in this list.)
ENRICH_KEYS = [
    "osm_name", "osm_building", "osm_amenity", "osm_height", "osm_building_levels",
    "osm_roof_shape", "osm_roof_material", "osm_operator",
    "addr_street", "addr_postcode", "addr_city",
    "osm_match_type", "osm_match_count", "osm_shared", "osm_best_jaccard",
]


# ── tag helpers ─────────────────────────────────────────────────────────────────

def _slug(s):
    return re.sub(r"[^a-z0-9_]", "_", s.lower())


def _epsg_from_srs(srs):
    """Extract an 'EPSG:NNNNN' string from a CityGML srsName, for ogr2ogr / pyproj."""
    if not srs:
        return None
    m = re.search(r"EPSG:+(\d{4,6})", srs)
    if m:
        return f"EPSG:{m.group(1)}"
    # ADV UTM aliases (ETRS89_UTM32/33 → 25832/25833)
    m = re.search(r"UTM(\d{2})", srs.upper())
    if m:
        return f"EPSG:258{m.group(1)}"
    return srs  # let pyproj try to resolve the raw string


def _parse_other_tags(raw):
    """Parse GDAL's HSTORE other_tags string  '"k"=>"v","k2"=>"v2"'  → dict."""
    tags = {}
    if not raw:
        return tags
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"', raw):
        k = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        v = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        tags[k] = v
    return tags


def _all_tags(props):
    """Merge GDAL promoted columns + parsed other_tags into one flat tag dict."""
    tags = {}
    for k, v in props.items():
        if k in ("osm_id", "osm_way_id", "other_tags") or v in (None, ""):
            continue
        tags[k] = v
    tags.update(_parse_other_tags(props.get("other_tags")))
    return tags


def classify_kind(tags, geom_type):
    """Route an OSM feature to a pykci kind based on its tags + geometry type."""
    if "building" in tags or "building:part" in tags:
        return KIND_BUILDING
    if tags.get("natural") == "water" or "water" in tags or tags.get("waterway") == "riverbank":
        return KIND_WATER
    if tags.get("natural") in ("wood", "scrub", "heath") or tags.get("landuse") == "forest" \
            or tags.get("leisure") == "park":
        return KIND_VEGETATION
    if "landuse" in tags:
        return KIND_LANDUSE
    if "highway" in tags or "railway" in tags:
        return KIND_ROAD
    if tags.get("natural") == "tree" or "man_made" in tags or "barrier" in tags:
        return KIND_FURNITURE
    # POIs: amenity/shop/office/tourism on a point (or a small area)
    if any(k in tags for k in ("amenity", "shop", "office", "tourism")):
        return KIND_POI
    return KIND_OTHER


def build_osm_props(feat, kind, source):
    """Assemble the OsmFeature node property dict from a GeoJSON feature."""
    props = feat.get("properties", {}) or {}
    tags = _all_tags(props)
    osm_id = props.get("osm_id") or props.get("osm_way_id") or ""
    geom_type = (feat.get("geometry") or {}).get("type", "")
    osm_type = ("relation" if str(osm_id).startswith("-") else
                "way" if geom_type in ("Polygon", "MultiPolygon", "LineString", "MultiLineString")
                else "node")
    osm_id = str(osm_id).lstrip("-")
    node = {
        "id": f"osm:{osm_type}/{osm_id}",
        "osm_id": osm_id,
        "osm_type": osm_type,
        "osm_kind": kind,
        "source": source,
    }
    # promoted, stable keys
    for t in PROMOTED_TAGS:
        if t in tags:
            key = "name" if t == "name" else "osm_" + _slug(t)
            node[key] = tags[t]
    # addr:* parsed out
    for sub in ("street", "housenumber", "postcode", "city", "country"):
        if f"addr:{sub}" in tags:
            node[f"addr_{sub}"] = tags[f"addr:{sub}"]
    # provenance metadata, if GDAL exposed it
    for meta in ("osm_version", "osm_timestamp"):
        if meta in props and props[meta]:
            node[meta] = props[meta]
    # lossless long tail: everything not already promoted
    promoted_keys = set(PROMOTED_TAGS) | {f"addr:{s}" for s in
                                          ("street", "housenumber", "postcode", "city", "country")}
    leftover = {k: v for k, v in tags.items() if k not in promoted_keys}
    if leftover:
        node["other_tags"] = json.dumps(leftover, ensure_ascii=False, sort_keys=True)
    return node


# ── geometry helpers ────────────────────────────────────────────────────────────

def geom_bbox_fields(geom):
    """Return bbox_* / center_* / bbox_wkt / location-ready dict for a shapely geom."""
    minx, miny, maxx, maxy = geom.bounds
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    bbox_wkt = (f"POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, "
                f"{minx} {maxy}, {minx} {miny}))")
    return {
        "bbox_min_x": minx, "bbox_min_y": miny,
        "bbox_max_x": maxx, "bbox_max_y": maxy,
        "center_x": cx, "center_y": cy, "bbox_wkt": bbox_wkt,
    }


def ring_rows(geom, osm_node_id, source):
    """
    Build GeometryPolygon/GeometryRing rows + HAS_FOOTPRINT/HAS_EXTERIOR/INTERIOR_RING
    edge rows for a (Multi)Polygon, reusing the CityGML geometry schema.
    Coordinates are 2-D here (z=0), stored verbatim in the dataset CRS.
    """
    polys, rings, foot_rels, ext_rels, int_rels = [], [], [], [], []
    geoms = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    for pi, poly in enumerate(geoms):
        poly_id = f"{osm_node_id}_poly{pi}"
        polys.append({"id": poly_id, "source": source})
        foot_rels.append({"src": osm_node_id, "tgt": poly_id, "order": pi})
        ext_id = f"{poly_id}_ext"
        rings.append({"id": ext_id, "pos_list": _pos_list(poly.exterior.coords),
                      "srs_dimension": 2, "ring_type": "exterior", "source": source})
        ext_rels.append({"src": poly_id, "tgt": ext_id})
        for ri, interior in enumerate(poly.interiors):
            int_id = f"{poly_id}_int{ri}"
            rings.append({"id": int_id, "pos_list": _pos_list(interior.coords),
                          "srs_dimension": 2, "ring_type": "interior", "source": source})
            int_rels.append({"src": poly_id, "tgt": int_id, "ring_index": ri})
    return polys, rings, foot_rels, ext_rels, int_rels


def line_rows(geom, osm_node_id, source):
    """Build GeometryLineString rows for an OSM line feature."""
    lines, rels = [], []
    geoms = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
    for li, ls in enumerate(geoms):
        ls_id = f"{osm_node_id}_line{li}"
        lines.append({"id": ls_id, "pos_list": _pos_list(ls.coords),
                      "srs_dimension": 2, "source": source})
        rels.append({"src": osm_node_id, "tgt": ls_id, "order": li})
    return lines, rels


def _pos_list(coords):
    return " ".join(f"{x} {y}" for x, y in [(c[0], c[1]) for c in coords])


# ── ogr2ogr extraction ───────────────────────────────────────────────────────────

LAYER_WHERE = {
    # only pull features we can route to a kind, to keep the GeoJSON small
    "multipolygons": "building IS NOT NULL OR landuse IS NOT NULL OR natural IS NOT NULL "
                     "OR leisure IS NOT NULL OR water IS NOT NULL OR waterway IS NOT NULL "
                     "OR man_made IS NOT NULL OR amenity IS NOT NULL OR \"natural\" IS NOT NULL",
    "points": "amenity IS NOT NULL OR shop IS NOT NULL OR office IS NOT NULL "
              "OR tourism IS NOT NULL OR \"natural\" IS NOT NULL OR man_made IS NOT NULL "
              "OR addr IS NOT NULL",
    "lines": "highway IS NOT NULL OR railway IS NOT NULL OR waterway IS NOT NULL",
}


def extract_layer(pbf, layer, out_path, t_srs, spat):
    """Run ogr2ogr: clip (-spat, WGS84) + reproject (-t_srs) one layer → GeoJSON."""
    minx, miny, maxx, maxy = spat
    cmd = [
        "ogr2ogr", "-f", "GeoJSON", str(out_path), str(pbf), layer,
        "-t_srs", t_srs,
        "-spat", str(minx), str(miny), str(maxx), str(maxy),
        "-skipfailures",
    ]
    where = LAYER_WHERE.get(layer)
    if where:
        cmd += ["-where", where]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not out_path.exists():
        return []
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh).get("features", [])


# ── Neo4j: dataset, indexes, spatial layer ────────────────────────────────────────

def get_dataset(sess, dataset_name=None):
    """Read the target Dataset's CRS + bbox corners from the graph."""
    q = ("MATCH (d:Dataset) " + ("WHERE d.name = $n OR d.id = $n " if dataset_name else "")
         + "RETURN d.name AS name, d.id AS id, d.crs AS crs, "
           "d.bbox_min_x AS minx, d.bbox_min_y AS miny, "
           "d.bbox_max_x AS maxx, d.bbox_max_y AS maxy")
    rows = list(sess.run(q, n=dataset_name))
    if not rows:
        raise SystemExit("ERROR: no matching Dataset node in the graph — run ingest_citygml.py first.")
    if len(rows) > 1 and not dataset_name:
        names = ", ".join(r["name"] for r in rows)
        raise SystemExit(f"ERROR: multiple datasets ({names}); pass --dataset <name>.")
    return rows[0]


def ensure_indexes(sess):
    for label, prop in [("OsmFeature", "id"), ("OsmFeature", "osm_id"), ("OsmFeature", "osm_kind")]:
        idx = f"{label.lower()}_{prop}_idx"
        sess.run(f"CREATE INDEX {idx} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})")
    try:
        sess.run("CREATE POINT INDEX osmfeature_location_idx IF NOT EXISTS "
                 "FOR (n:OsmFeature) ON (n.location)")
    except Exception:
        pass


def reset_osm(sess, layer="osm_features"):
    """
    Remove all prior OSM data so a re-run cleanly reflects the current parameters
    (idempotency across changed --buffer/--overlap-threshold). Drops the osm_features
    R-tree layer, strips osm_* enrichment props from pykci nodes (restoring name=id, the
    gml:id invariant), and deletes OsmFeature nodes + their OSM-only geometry (ids
    prefixed 'osm:'). CityGML nodes/geometry are untouched.
    """
    try:
        sess.run("CALL spatial.removeLayer($l)", l=layer)
    except Exception:
        pass
    removes = ", ".join(f"b.{k}" for k in ENRICH_KEYS)
    sess.run(f"MATCH (b) WHERE (b:Building OR b:CityObject) AND b.osm_match_type IS NOT NULL "
             f"SET b.name = b.id REMOVE {removes}")
    for lbl in ("GeometryRing", "GeometryPolygon", "GeometryLineString"):
        sess.run(f"MATCH (n:{lbl}) WHERE n.id STARTS WITH 'osm:' DETACH DELETE n")
    sess.run("MATCH (n:OsmFeature) DETACH DELETE n")


def ensure_spatial_layer(sess, layer="osm_features"):
    try:
        existing = {r["name"] for r in sess.run("CALL spatial.layers() YIELD name RETURN name")}
        if layer not in existing:
            sess.run("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", layer=layer)
        return True
    except Exception as exc:
        print(f"  WARNING: spatial layer setup failed ({exc})")
        return False


def index_osm_spatial(sess, layer="osm_features"):
    try:
        r = sess.run(
            "MATCH (n:OsmFeature) WHERE n.bbox_wkt IS NOT NULL "
            "WITH collect(n) AS nodes CALL spatial.addNodes($layer, nodes) YIELD count "
            "RETURN count AS n", layer=layer).single()
        return r["n"] if r else 0
    except Exception as exc:
        print(f"  WARNING: spatial indexing failed ({exc})")
        return 0


# ── Neo4j: write OsmFeature nodes + geometry ──────────────────────────────────────

def write_osm_nodes(sess, nodes, polys, rings, foot_rels, ext_rels, int_rels,
                    lines, line_rels, dataset_id, district_id):
    # The dynamic label (e.g. 'OsmBuilding') is a transient APOC parameter, NOT a stored
    # property — persisting it would duplicate the real Neo4j label (and osm_kind already
    # carries the type for the no-APOC case). Strip it from the rows into a side map.
    labels = {}
    for n in nodes:
        lbl = n.pop("label", None)
        if lbl:
            labels[n["id"]] = lbl
    apoc_ok = True
    for i in range(0, len(nodes), BATCH_SIZE):
        chunk = nodes[i:i + BATCH_SIZE]
        if apoc_ok:
            try:
                sess.run(
                    "UNWIND $rows AS row MERGE (n:OsmFeature {id: row.id}) "
                    "SET n += row, n:Entity "
                    "WITH n, $labels[n.id] AS lbl WHERE lbl IS NOT NULL "
                    "CALL apoc.create.addLabels(n, [lbl]) YIELD node "
                    "WITH node WHERE node.center_x IS NOT NULL "
                    "SET node.location = point({x: node.center_x, y: node.center_y})",
                    rows=chunk, labels=labels)
                continue
            except Exception:
                apoc_ok = False
        sess.run(
            "UNWIND $rows AS row MERGE (n:OsmFeature {id: row.id}) SET n += row, n:Entity "
            "WITH n WHERE n.center_x IS NOT NULL "
            "SET n.location = point({x: n.center_x, y: n.center_y})",
            rows=chunk)
    # geometry nodes
    _batch(sess, "UNWIND $rows AS row MERGE (n:GeometryPolygon {id: row.id}) "
                 "SET n.source = row.source, n:Entity", polys)
    _batch(sess, "UNWIND $rows AS row MERGE (n:GeometryRing {id: row.id}) SET n += row", rings)
    _batch(sess, "UNWIND $rows AS row MERGE (n:GeometryLineString {id: row.id}) SET n += row", lines)
    # geometry edges
    _batch(sess, "UNWIND $rows AS row MATCH (a:OsmFeature {id: row.src}), "
                 "(b:GeometryPolygon {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_FOOTPRINT]->(b) SET r.order = row.order", foot_rels)
    _batch(sess, "UNWIND $rows AS row MATCH (a:GeometryPolygon {id: row.src}), "
                 "(b:GeometryRing {id: row.tgt}) MERGE (a)-[:HAS_EXTERIOR_RING]->(b)", ext_rels)
    _batch(sess, "UNWIND $rows AS row MATCH (a:GeometryPolygon {id: row.src}), "
                 "(b:GeometryRing {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_INTERIOR_RING]->(b) SET r.ring_index = row.ring_index", int_rels)
    _batch(sess, "UNWIND $rows AS row MATCH (a:OsmFeature {id: row.src}), "
                 "(b:GeometryLineString {id: row.tgt}) "
                 "MERGE (a)-[r:HAS_LOD_SURFACE]->(b) SET r.order = row.order", line_rels)
    # PART_OF dataset + LOCATED_IN district
    sess.run("MATCH (n:OsmFeature {source: $src}), (d:Dataset {id: $did}) "
             "MERGE (n)-[:PART_OF]->(d)", src=nodes[0]["source"] if nodes else "", did=dataset_id)
    if district_id:
        sess.run("MATCH (n:OsmFeature {source: $src}), (d:District {id: $did}) "
                 "MERGE (n)-[:LOCATED_IN]->(d)", src=nodes[0]["source"] if nodes else "", did=district_id)


def _batch(sess, query, rows):
    for i in range(0, len(rows), BATCH_SIZE):
        sess.run(query, rows=rows[i:i + BATCH_SIZE])


# ── matching: overlap graph + connected components ────────────────────────────────

def fetch_pykci_footprints(sess, target_type, bbox):
    """
    Return {pykci_id: shapely.Polygon} for the target pykci feature type within bbox,
    using the CityGML R-tree to scope candidates, then GroundSurface (or any surface)
    exterior rings for the footprint. Falls back to the bbox polygon.
    """
    from shapely.geometry import Polygon, box
    minx, miny, maxx, maxy = bbox
    # spatial.intersects(layer, wkt) is the reliable candidate procedure (spatial.bbox
    # NPEs on WKT layers in some neo4j-spatial builds); GRAPH.md's overlap example uses it.
    wkt = (f"POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, "
           f"{minx} {maxy}, {minx} {miny}))")
    if target_type == "Building":
        q = ("CALL spatial.intersects('features', $wkt) YIELD node AS f "
             "WHERE f:Building "
             "OPTIONAL MATCH (f)-[:HAS_BOUNDARY]->(bs:BoundarySurface {surface_type:'GroundSurface'}) "
             "-[:HAS_POLYGON]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(gr:GeometryRing) "
             "WITH f, collect(gr.pos_list) AS rings "
             "RETURN f.id AS id, rings, f.bbox_min_x AS minx, f.bbox_min_y AS miny, "
             "f.bbox_max_x AS maxx, f.bbox_max_y AS maxy")
    else:
        q = ("CALL spatial.intersects('features', $wkt) YIELD node AS f "
             "WHERE f:CityObject AND f.feature_type = $ft "
             "OPTIONAL MATCH (f)-[:HAS_LOD_SURFACE]->(:GeometryMultiSurface) "
             "-[:HAS_SURFACE_MEMBER]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(gr:GeometryRing) "
             "WITH f, collect(gr.pos_list) AS rings "
             "RETURN f.id AS id, rings, f.bbox_min_x AS minx, f.bbox_min_y AS miny, "
             "f.bbox_max_x AS maxx, f.bbox_max_y AS maxy")
    out = {}
    for r in sess.run(q, wkt=wkt, ft=target_type):
        poly = None
        for pl in (r["rings"] or []):
            ring = _ring_xy(pl)
            if len(ring) >= 4:
                try:
                    p = Polygon(ring)
                    if p.is_valid and p.area > 0:
                        poly = p if poly is None else poly.union(p)
                except Exception:
                    pass
        if poly is None and r["minx"] is not None:
            poly = box(r["minx"], r["miny"], r["maxx"], r["maxy"])
        if poly is not None and not poly.is_empty:
            out[r["id"]] = poly
    return out


def _ring_xy(pos_list):
    v = pos_list.split()
    # CityGML rings are usually x y z triplets; detect stride by length divisibility
    stride = 3 if len(v) % 3 == 0 else 2
    pts = []
    for i in range(0, len(v) - stride + 1, stride):
        try:
            pts.append((float(v[i]), float(v[i + 1])))
        except ValueError:
            pass
    return pts


def match_areas(osm_geoms, pykci_polys, tau):
    """
    Build the weighted overlap graph between OSM features (osm_id → shapely geom) and
    pykci footprints (pykci_id → shapely polygon); return (edges, components).

    Acceptance ratio is geometry-aware:
      * OSM polygon  → area(A∩B) / min(area(A), area(B))  (symmetric, catches 1:n & n:1)
      * OSM line     → length(A∩B) / length(A)            (fraction of the line in the area)
    Jaccard is recorded for areas (well-defined); for lines it equals the length ratio.

      edges:      list of {osm, pykci, overlap_ratio, jaccard, inter_area}
      components: list of (set_osm_ids, set_pykci_ids) connected components
    """
    edges = []
    adj = defaultdict(set)  # node-key -> neighbours (bipartite, keys are ('o',id)/('p',id))
    pid_list = list(pykci_polys.items())
    prepared = {pid: prep(g) for pid, g in pid_list}
    for oid, og in osm_geoms.items():
        if og is None or og.is_empty:
            continue
        is_line = og.area == 0 and og.length > 0
        oa = og.area
        for pid, pg in pid_list:
            if not prepared[pid].intersects(og):
                continue
            try:
                clip = og.intersection(pg)
            except Exception:
                continue
            pa = pg.area
            if is_line:
                inter = clip.length
                ratio = inter / og.length if og.length > 0 else 0.0
                jacc = ratio
            else:
                inter = clip.area
                ratio = inter / min(oa, pa) if min(oa, pa) > 0 else 0.0
                union = oa + pa - inter
                jacc = inter / union if union > 0 else 0.0
            if inter <= 0 or ratio < tau:
                continue
            edges.append({"osm": oid, "pykci": pid, "overlap_ratio": ratio,
                          "jaccard": jacc, "inter_area": inter})
            adj[("o", oid)].add(("p", pid))
            adj[("p", pid)].add(("o", oid))
    # connected components over the bipartite graph
    seen, comps = set(), []
    for start in list(adj):
        if start in seen:
            continue
        stack, comp = [start], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.add(n)
            stack.extend(adj[n] - seen)
        osm_ids = {k[1] for k in comp if k[0] == "o"}
        pyk_ids = {k[1] for k in comp if k[0] == "p"}
        comps.append((osm_ids, pyk_ids))
    return edges, comps


def classify_case(n_osm, n_pyk):
    if n_osm == 1 and n_pyk == 1:
        return "1:1"
    if n_osm == 1 and n_pyk > 1:
        return "1:n"
    if n_osm > 1 and n_pyk == 1:
        return "n:1"
    return "n:m"


def build_enrich_writes(edges, comps, osm_props_by_id, target_label):
    """
    Produce (enrich_rels, building_scalar_updates) applying the §6.4 attribution rules.
      enrich_rels: {pykci, osm, overlap_ratio, jaccard, inter_area, match_type, is_primary}
      scalar_updates: {pykci_id: {osm_* scalar props...}}
    """
    by_osm = defaultdict(list)   # osm_id -> edges
    by_pyk = defaultdict(list)   # pykci_id -> edges
    for e in edges:
        by_osm[e["osm"]].append(e)
        by_pyk[e["pykci"]].append(e)

    rels, scalar = [], defaultdict(dict)
    for osm_ids, pyk_ids in comps:
        case = classify_case(len(osm_ids), len(pyk_ids))
        comp_edges = [e for e in edges if e["osm"] in osm_ids and e["pykci"] in pyk_ids]

        # primary edges: per pykci building, its dominant OSM contributor = the one
        # sharing the largest intersection AREA (not overlap_ratio, which saturates to
        # ~1.0 for a small fully-contained fragment and would mis-rank tiny pieces).
        primary_pair = set()
        for pid in pyk_ids:
            best = max((e for e in comp_edges if e["pykci"] == pid),
                       key=lambda e: e["inter_area"], default=None)
            if best:
                primary_pair.add((best["osm"], best["pykci"]))
        # for 1:n, primary pykci = the highest-Jaccard building (singular-data holder)
        primary_pyk = None
        if case in ("1:n",):
            primary_pyk = max(pyk_ids, key=lambda pid: max(
                (e["jaccard"] for e in comp_edges if e["pykci"] == pid), default=0.0))

        for e in comp_edges:
            is_primary = ((e["osm"], e["pykci"]) in primary_pair
                          and (primary_pyk is None or e["pykci"] == primary_pyk))
            rels.append({**e, "match_type": case, "is_primary": is_primary})

        # scalar copy rules
        for pid in pyk_ids:
            # the OSM contributor sharing the largest area with THIS building
            best = max((e for e in comp_edges if e["pykci"] == pid),
                       key=lambda e: e["inter_area"], default=None)
            if not best:
                continue
            src_props = osm_props_by_id.get(best["osm"], {})
            upd = {f"osm_match_type": case, "osm_match_count": len(by_pyk[pid]),
                   "osm_best_jaccard": round(best["jaccard"], 4)}
            for k, v in src_props.items():
                if k in SHARED_THEMATIC:
                    # never overwrite an authoritative pykci key: the OSM feature's own
                    # `name` (== nothing on OsmFeature, but `name` IS the gml:id on a
                    # pykci Building) is namespaced to osm_name on the enriched node.
                    upd["osm_name" if k == "name" else k] = v
            if case == "1:n":
                upd["osm_shared"] = True
            scalar[pid].update(upd)
    return rels, scalar


def write_enrichment(sess, rels, scalar, target_label):
    _batch(sess,
           f"UNWIND $rows AS row MATCH (b:{target_label} {{id: row.pykci}}), "
           "(o:OsmFeature {id: row.osm}) "
           "MERGE (b)-[r:ENRICHED_BY]->(o) "
           "SET r.overlap_ratio = row.overlap_ratio, r.jaccard = row.jaccard, "
           "r.intersection_area = row.inter_area, r.match_type = row.match_type, "
           "r.is_primary = row.is_primary", rels)
    scalar_rows = [{"id": pid, "props": props} for pid, props in scalar.items()]
    _batch(sess,
           f"UNWIND $rows AS row MATCH (b:{target_label} {{id: row.id}}) SET b += row.props",
           scalar_rows)


def write_pois(sess, poi_rows):
    """Attach point POIs to the building whose footprint contains them."""
    _batch(sess,
           "UNWIND $rows AS row MATCH (b:Building {id: row.building}), "
           "(o:OsmFeature {id: row.osm}) "
           "MERGE (b)-[r:HAS_POI]->(o) SET r.distance_m = row.distance_m", poi_rows)


# ── main pipeline ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Ingest OSM (.osm.pbf) enrichment into the pykci graph.")
    ap.add_argument("pbf", help="Path to .osm.pbf")
    ap.add_argument("--dataset", help="Target Dataset name/id (required if >1 dataset present)")
    ap.add_argument("--buffer", type=float, default=10.0, help="Clip buffer in metres (default 10)")
    ap.add_argument("--overlap-threshold", type=float, default=0.3,
                    help="Min overlap ratio (intersection / min-area) to accept a match (default 0.3)")
    args = ap.parse_args()

    if not shutil.which("ogr2ogr"):
        raise SystemExit("ERROR: ogr2ogr (GDAL) not found on PATH.")
    pbf = Path(args.pbf)
    if not pbf.exists():
        raise SystemExit(f"ERROR: {pbf} not found.")
    source = f"osm:{pbf.stem}"
    tau = args.overlap_threshold

    print("Connecting to Neo4j …")
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH,
                                  notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
    driver.verify_connectivity()

    with driver.session(database="neo4j") as sess:
        ds = get_dataset(sess, args.dataset)
        crs = _epsg_from_srs(ds["crs"])
        dataset_id, district_id = ds["id"], None
        drow = sess.run("MATCH (d:Dataset {id:$id})-[:COVERS]->(dist:District) RETURN dist.id AS id",
                        id=dataset_id).single()
        if drow:
            district_id = drow["id"]
        print(f"  Target dataset '{ds['name']}'  CRS={crs}  "
              f"bbox=({ds['minx']},{ds['miny']})-({ds['maxx']},{ds['maxy']})")

        # clip window in WGS84 = dataset bbox + buffer, inverse-transformed
        fwd = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        b = args.buffer
        lo = fwd.transform(ds["minx"] - b, ds["miny"] - b)
        hi = fwd.transform(ds["maxx"] + b, ds["maxy"] + b)
        spat = (min(lo[0], hi[0]), min(lo[1], hi[1]), max(lo[0], hi[0]), max(lo[1], hi[1]))
        match_bbox = (ds["minx"] - b, ds["miny"] - b, ds["maxx"] + b, ds["maxy"] + b)
        print(f"  WGS84 clip window: {spat}")

        ensure_indexes(sess)
        print("  Resetting any prior OSM data for a clean re-run …")
        reset_osm(sess)

        # ── read + classify OSM features ──────────────────────────────────────
        tmp = Path(tempfile.mkdtemp(prefix="osm_"))
        all_features = []
        for layer in ("multipolygons", "points", "lines"):
            feats = extract_layer(pbf, layer, tmp / f"{layer}.geojson", crs, spat)
            print(f"  ogr2ogr {layer}: {len(feats)} features")
            all_features += [(layer, f) for f in feats]

        # build OsmFeature nodes + geometry, bucket geoms by kind for matching
        nodes, polys, rings, foot_rels, ext_rels, int_rels, lines, line_rels = ([] for _ in range(8))
        area_geoms = defaultdict(dict)     # target_label/feature_type key -> {osm_id: geom}
        osm_props_by_id = {}               # osm node id -> its node prop dict
        point_pois = []                    # (osm node id, shapely Point)
        kind_counts = defaultdict(int)

        for layer, feat in all_features:
            g = feat.get("geometry")
            if not g:
                continue
            try:
                geom = shape(g)
            except Exception:
                continue
            if geom.is_empty:
                continue
            tags = _all_tags(feat.get("properties", {}) or {})
            kind = classify_kind(tags, geom.geom_type)
            label, target_label, target_ft = KIND_LABEL[kind]
            node = build_osm_props(feat, kind, source)
            node["label"] = label
            node["matched"] = False
            node.update(geom_bbox_fields(geom))
            kind_counts[kind] += 1

            if geom.geom_type in ("Polygon", "MultiPolygon"):
                p, r, fr, er, ir = ring_rows(geom, node["id"], source)
                polys += p; rings += r; foot_rels += fr; ext_rels += er; int_rels += ir
                if target_label:
                    key = (target_label, target_ft)
                    area_geoms[key][node["id"]] = geom
            elif geom.geom_type in ("LineString", "MultiLineString"):
                ln, lr = line_rows(geom, node["id"], source)
                lines += ln; line_rels += lr
                if target_label:
                    area_geoms[(target_label, target_ft)][node["id"]] = geom
            elif geom.geom_type == "Point":
                if kind == KIND_POI:
                    point_pois.append((node["id"], geom))

            osm_props_by_id[node["id"]] = node
            nodes.append(node)

        print(f"  OSM features by kind: {dict(kind_counts)}  (total {len(nodes)})")
        if not nodes:
            print("  No OSM features in clip window; nothing to do.")
            driver.close(); return

        # ── write OsmFeature nodes + geometry ─────────────────────────────────
        print("  Writing OsmFeature nodes + geometry …")
        write_osm_nodes(sess, nodes, polys, rings, foot_rels, ext_rels, int_rels,
                        lines, line_rels, dataset_id, district_id)
        ensure_spatial_layer(sess)
        n_idx = index_osm_spatial(sess)
        print(f"  Indexed {n_idx} OsmFeatures in 'osm_features' R-tree.")

        # ── match area/line features per target type ──────────────────────────
        matched_osm_ids = set()
        case_counts = defaultdict(int)
        buildings_enriched = 0
        empty_targets = set()      # (target_label, target_ft) with no pykci footprints in area
        for (target_label, target_ft), osm_geoms in area_geoms.items():
            pykci_polys = fetch_pykci_footprints(sess, target_ft if target_ft else target_label,
                                                 match_bbox)
            if not pykci_polys:
                empty_targets.add((target_label, target_ft))
                print(f"    {target_label}/{target_ft}: no pykci footprints in area "
                      f"({len(osm_geoms)} OSM kept as net-new orphan nodes)")
                continue
            edges, comps = match_areas(osm_geoms, pykci_polys, tau)
            rels, scalar = build_enrich_writes(edges, comps, osm_props_by_id, target_label)
            write_enrichment(sess, rels, scalar, target_label)
            for e in edges:
                matched_osm_ids.add(e["osm"])
            for osm_ids, pyk_ids in comps:
                case_counts[classify_case(len(osm_ids), len(pyk_ids))] += 1
            if target_label == "Building":
                buildings_enriched += len(scalar)
            print(f"    {target_label}/{target_ft}: {len(edges)} edges, "
                  f"{len(comps)} components, {len(scalar)} pykci features enriched")

        # ── POIs: point-in-footprint ──────────────────────────────────────────
        pois_attached = 0
        if point_pois:
            bld = fetch_pykci_footprints(sess, "Building", match_bbox)
            prepared = {bid: prep(g) for bid, g in bld.items()}
            poi_rows = []
            for osm_node_id, pt in point_pois:
                for bid, g in bld.items():
                    if prepared[bid].contains(pt):
                        poi_rows.append({"building": bid, "osm": osm_node_id,
                                         "distance_m": round(pt.distance(g.centroid), 2)})
                        matched_osm_ids.add(osm_node_id)
                        break
            write_pois(sess, poi_rows)
            pois_attached = len(poi_rows)
            print(f"    POIs: {pois_attached} attached to buildings (of {len(point_pois)} points)")

        # ── flag orphans, with a meaningful reason per feature ─────────────────
        def orphan_reason(kind):
            tlbl, tft = KIND_LABEL[kind][1], KIND_LABEL[kind][2]
            if kind == KIND_POI:
                return "no_containing_building"
            if tlbl is None or (tlbl, tft) in empty_targets:
                return "no_pykci_type"
            return "no_overlap"

        orphans_by_reason = defaultdict(list)
        for n in nodes:
            if n["id"] not in matched_osm_ids:
                orphans_by_reason[orphan_reason(n["osm_kind"])].append(n["id"])
        for reason, ids in orphans_by_reason.items():
            for i in range(0, len(ids), BATCH_SIZE):
                sess.run("UNWIND $rows AS row MATCH (o:OsmFeature {id: row}) "
                         "SET o.matched = false, o.unmatched_reason = $reason",
                         rows=ids[i:i + BATCH_SIZE], reason=reason)
        _batch(sess, "UNWIND $rows AS row MATCH (o:OsmFeature {id: row}) "
                     "SET o.matched = true, o.unmatched_reason = null",
               list(matched_osm_ids))

        osm_buildings = kind_counts.get("building", 0)
        summary = {
            "dataset": ds["name"], "source": source,
            "osm_features": len(nodes), "by_kind": dict(kind_counts),
            "match_cases": dict(case_counts),
            "matched": len(matched_osm_ids),
            "orphans": len(nodes) - len(matched_osm_ids),
            "orphans_by_reason": {k: len(v) for k, v in orphans_by_reason.items()},
            "osm_buildings": osm_buildings,
            "pykci_buildings_enriched": buildings_enriched,
            "building_enrich_rate": round(buildings_enriched / osm_buildings, 3) if osm_buildings else 0.0,
            "pois_attached": pois_attached,
            "note": "orphans are mostly net-new types (roads/furniture/landuse/vegetation) "
                    "with no CityObject counterpart in this LoD2 tile — they are kept as "
                    "spatially-indexed standalone nodes, not discarded.",
        }

    driver.close()
    out = Path("output") / f"{pbf.stem}_osm_summary.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("# OSM ingestion summary\n\n```json\n"
                   + json.dumps(summary, indent=2, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    print("Done.")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
