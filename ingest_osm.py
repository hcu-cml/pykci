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

Geometry is read via the ``ogr2ogr`` CLI (clip + reproject to GeoJSON; ``points`` and
``multipolygons`` unfiltered, ``lines`` restricted to the transport/water network),
then parsed with ``json`` + ``shapely`` — no GDAL Python binding required.
Completeness gate: ``eval/osm_ingestion_census.py`` proves every admitted feature is
stored losslessly and placed (anchored or standalone) — run it after each fusion.

Usage:
    python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf
    python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf --dataset hamburg_hafencity
    python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf --buffer 50 --overlap-threshold 0.5
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import shapely
from neo4j import GraphDatabase
from neo4j.exceptions import TransientError
from pyproj import Transformer
from shapely.geometry import shape, Point
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")
BATCH_SIZE = 1000
# Parallel write/read sessions. The neo4j Driver object is thread-safe; Sessions are NOT,
# so each worker opens its own session per chunk. Writes on disjoint id ranges commit
# independently; rare lock conflicts (shared endpoints at chunk borders) surface as
# TransientError and are retried with backoff. Scales with the machine (capped at 16:
# beyond that Neo4j lock contention outweighs the extra sessions).
WORKERS = min(16, os.cpu_count() or 6)

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
    # EPSG code in any common encoding: 'EPSG:6697', 'EPSG::6697' (URN),
    # or the OGC HTTP form '.../def/crs/EPSG/0/6697' (the '0/' is a version).
    m = re.search(r"EPSG[:/]+(?:\d+/)?(\d{4,6})", srs)
    if m:
        return f"EPSG:{m.group(1)}"
    u = srs.upper().replace(" ", "")
    # ADV UTM aliases (ETRS89_UTM32/33 → 25832/25833)
    m = re.search(r"UTM(\d{2})", u)
    if m:
        return f"EPSG:258{m.group(1)}"
    # Swiss national CRS sometimes given as a bare name with no EPSG code
    if "CH1903+" in u or "LV95" in u:
        return "EPSG:2056"
    if "CH1903" in u or "LV03" in u:
        return "EPSG:21781"
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
    geom_type = (feat.get("geometry") or {}).get("type", "")
    # GDAL id semantics: in the `multipolygons` layer a closed-way polygon
    # carries `osm_way_id` (osm_id NULL) while a relation-assembled polygon
    # carries the *positive* relation id in `osm_id` (osm_way_id NULL). OSM
    # ids are unique per object type only, so typing relations as ways
    # conflates e.g. relation/5713700 (a marina) with way/5713700 (a road)
    # into one node, racing tag writes and losing data. Type by field
    # presence, not by the legacy negative-id convention.
    osm_way_id = props.get("osm_way_id")
    raw_id = props.get("osm_id")
    if geom_type in ("Polygon", "MultiPolygon"):
        if osm_way_id:
            osm_type, osm_id = "way", osm_way_id
        else:
            osm_type, osm_id = "relation", raw_id or ""
    elif geom_type in ("LineString", "MultiLineString"):
        osm_type, osm_id = "way", raw_id or osm_way_id or ""
    else:
        osm_type, osm_id = "node", raw_id or ""
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
    # `points` and `multipolygons` are ingested UNFILTERED: every feature GDAL
    # assembles inside the city bbox becomes an OsmFeature node (untagged-kind ones
    # land in kind 'other'). Historical note: these two layers once carried a
    # tag-filter WHERE clause, but it referenced fields outside GDAL's default
    # osmconf schema (`amenity` on points, `water` on multipolygons), so
    # SetAttributeFilter failed and `-skipfailures` proceeded without any filter
    # — the clause was dead code and is removed for honesty (behaviour, and the
    # published Hamburg node counts, are unchanged). Verified by the scope pass
    # of eval/osm_ingestion_census.py (unfiltered == admitted for both layers).
    # `lines` IS filtered (its fields exist): only the transport/water network
    # is ingested; barrier/power/etc. lines are documented out of scope.
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


def resolve_extent(sess, ds, min_span=50.0):
    """
    Return (minx, miny, maxx, maxy, recomputed) for the OSM clip / match window.

    Prefer the stored Dataset bbox, but fall back to the true extent unioned from the
    feature bounding boxes when the stored bbox is missing or degenerate (some ingests
    leave Dataset.bbox reflecting a single feature rather than the union — a span far too
    small to clip OSM against would silently return almost no OSM data).
    """
    minx, miny, maxx, maxy = ds["minx"], ds["miny"], ds["maxx"], ds["maxy"]
    ok = (all(v is not None for v in (minx, miny, maxx, maxy))
          and (maxx - minx) > min_span and (maxy - miny) > min_span)
    if ok:
        return minx, miny, maxx, maxy, False
    r = sess.run(
        "MATCH (f) WHERE (f:Building OR f:CityObject) AND f.bbox_min_x IS NOT NULL "
        "RETURN min(f.bbox_min_x) AS minx, min(f.bbox_min_y) AS miny, "
        "max(f.bbox_max_x) AS maxx, max(f.bbox_max_y) AS maxy").single()
    if not r or r["minx"] is None:
        raise SystemExit("ERROR: Dataset bbox is degenerate and no feature bboxes were found.")
    return r["minx"], r["miny"], r["maxx"], r["maxy"], True


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

    Deletes run in CALL {} IN TRANSACTIONS batches: a whole-city run leaves millions of
    OSM nodes, and a single DETACH DELETE transaction over them overruns
    dbms.memory.transaction.total.max.
    """
    try:
        sess.run("CALL spatial.removeLayer($l)", l=layer)
    except Exception:
        pass
    removes = ", ".join(f"b.{k}" for k in ENRICH_KEYS)
    # Label-anchored (one scan per label), NOT `MATCH (b) WHERE b:Building OR b:CityObject`:
    # the label-less form is an AllNodesScan that streams the whole store and overruns
    # dbms.memory.transaction.total.max on a city-scale graph (Tokyo, 64M nodes). Per-label
    # scans are bounded to the Building / CityObject counts.
    for lbl in ("Building", "CityObject"):
        sess.run(f"MATCH (b:{lbl}) WHERE b.osm_match_type IS NOT NULL "
                 f"CALL {{ WITH b SET b.name = b.id REMOVE {removes} }} "
                 f"IN TRANSACTIONS OF 10000 ROWS")
    for lbl in ("GeometryRing", "GeometryPolygon", "GeometryLineString"):
        sess.run(f"MATCH (n:{lbl}) WHERE n.id STARTS WITH 'osm:' "
                 f"CALL {{ WITH n DETACH DELETE n }} IN TRANSACTIONS OF 10000 ROWS")
    sess.run("MATCH (n:OsmFeature) "
             "CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")


# ── parallel batched writes ───────────────────────────────────────────────────────

def _run_chunk(driver, query, chunk, retries=8, **params):
    """Run one batch in its own session/transaction, retrying transient lock conflicts
    (deadlocks/lock timeouts) with capped backoff before giving up."""
    for attempt in range(retries):
        try:
            with driver.session(database="neo4j") as s:
                s.run(query, rows=chunk, **params).consume()
            return
        except TransientError:
            time.sleep(min(0.25 * (attempt + 1), 2.0))
    with driver.session(database="neo4j") as s:  # last try: let it raise
        s.run(query, rows=chunk, **params).consume()


def _batch_parallel(driver, query, rows, workers=WORKERS, **params):
    """UNWIND-batch `rows` through the query across parallel sessions (thread-safe:
    one session per submitted chunk, chunks are disjoint row ranges)."""
    if not rows:
        return
    chunks = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in [ex.submit(_run_chunk, driver, query, c, **params) for c in chunks]:
            f.result()


def ensure_spatial_layer(sess, layer="osm_features"):
    try:
        existing = {r["name"] for r in sess.run("CALL spatial.layers() YIELD name RETURN name")}
        if layer not in existing:
            sess.run("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", layer=layer)
        return True
    except Exception as exc:
        print(f"  WARNING: spatial layer setup failed ({exc})")
        return False


def index_osm_spatial(sess, layer="osm_features", batch=10000):
    """
    Register OsmFeature nodes in the R-tree in batches. A single
    spatial.addNodes(collect(n)) over the whole city collects millions of nodes into one
    transaction and overruns dbms.memory.transaction.total.max (indexing 0); batching
    keeps each addNodes call within the transaction memory budget.
    """
    try:
        ids = [r["id"] for r in sess.run(
            "MATCH (n:OsmFeature) WHERE n.bbox_wkt IS NOT NULL RETURN n.id AS id")]
        total = 0
        for i in range(0, len(ids), batch):
            r = sess.run(
                "UNWIND $ids AS id MATCH (n:OsmFeature {id: id}) "
                "WITH collect(n) AS nodes CALL spatial.addNodes($layer, nodes) YIELD count "
                "RETURN count AS c", ids=ids[i:i + batch], layer=layer).single()
            total += (r["c"] if r else 0)
        return total
    except Exception as exc:
        print(f"  WARNING: spatial indexing failed ({exc})")
        return 0


# ── Neo4j: write OsmFeature nodes + geometry ──────────────────────────────────────

def write_osm_nodes(driver, sess, nodes, polys, rings, foot_rels, ext_rels, int_rels,
                    lines, line_rels, dataset_id, district_id):
    # The dynamic label (e.g. 'OsmBuilding') is transient, NOT a stored property.
    # Group the rows BY label and bake the label statically into each group's query:
    # no APOC call, and no per-batch label map (serializing a whole-city id→label map
    # with every batch dominated the write time at ~1M nodes).
    by_label = defaultdict(list)
    for n in nodes:
        by_label[n.pop("label", None)].append(n)
    for lbl, rows in by_label.items():
        extra = f", n:{lbl}" if lbl and lbl != "OsmFeature" else ""
        _batch_parallel(
            driver,
            f"UNWIND $rows AS row MERGE (n:OsmFeature {{id: row.id}}) "
            f"SET n += row, n:Entity{extra} "
            "WITH n WHERE n.center_x IS NOT NULL "
            "SET n.location = point({x: n.center_x, y: n.center_y})",
            rows)
    # geometry nodes (disjoint ids → embarrassingly parallel)
    _batch_parallel(driver, "UNWIND $rows AS row MERGE (n:GeometryPolygon {id: row.id}) "
                            "SET n.source = row.source, n:Entity", polys)
    _batch_parallel(driver, "UNWIND $rows AS row MERGE (n:GeometryRing {id: row.id}) "
                            "SET n += row", rings)
    _batch_parallel(driver, "UNWIND $rows AS row MERGE (n:GeometryLineString {id: row.id}) "
                            "SET n += row", lines)
    # geometry edges — index-backed MATCHes on both endpoints (OsmFeature.id /
    # GeometryPolygon.id / GeometryRing.id range indexes); rows for one parent are
    # contiguous, so cross-chunk lock conflicts are rare and retried.
    _batch_parallel(driver, "UNWIND $rows AS row MATCH (a:OsmFeature {id: row.src}), "
                            "(b:GeometryPolygon {id: row.tgt}) "
                            "MERGE (a)-[r:HAS_FOOTPRINT]->(b) SET r.order = row.order", foot_rels)
    _batch_parallel(driver, "UNWIND $rows AS row MATCH (a:GeometryPolygon {id: row.src}), "
                            "(b:GeometryRing {id: row.tgt}) "
                            "MERGE (a)-[:HAS_EXTERIOR_RING]->(b)", ext_rels)
    _batch_parallel(driver, "UNWIND $rows AS row MATCH (a:GeometryPolygon {id: row.src}), "
                            "(b:GeometryRing {id: row.tgt}) "
                            "MERGE (a)-[r:HAS_INTERIOR_RING]->(b) "
                            "SET r.ring_index = row.ring_index", int_rels)
    _batch_parallel(driver, "UNWIND $rows AS row MATCH (a:OsmFeature {id: row.src}), "
                            "(b:GeometryLineString {id: row.tgt}) "
                            "MERGE (a)-[r:HAS_LOD_SURFACE]->(b) SET r.order = row.order", line_rels)
    # PART_OF dataset + LOCATED_IN district — batched: one MERGE-per-node transaction
    # over a whole city would blow the transaction memory limit.
    src = nodes[0]["source"] if nodes else ""
    sess.run("MATCH (n:OsmFeature {source: $src}) "
             "CALL { WITH n MATCH (d:Dataset {id: $did}) MERGE (n)-[:PART_OF]->(d) } "
             "IN TRANSACTIONS OF 10000 ROWS", src=src, did=dataset_id)
    if district_id:
        sess.run("MATCH (n:OsmFeature {source: $src}) "
                 "CALL { WITH n MATCH (d:District {id: $did}) MERGE (n)-[:LOCATED_IN]->(d) } "
                 "IN TRANSACTIONS OF 10000 ROWS", src=src, did=district_id)


def _batch(sess, query, rows):
    for i in range(0, len(rows), BATCH_SIZE):
        sess.run(query, rows=rows[i:i + BATCH_SIZE])


# ── matching: overlap graph + connected components ────────────────────────────────

def fetch_pykci_footprints(driver, target_type, bbox, tile_m=6000.0, workers=WORKERS):
    """
    Return {pykci_id: shapely.Polygon} for the target pykci feature type within bbox,
    using the CityGML R-tree to scope candidates, then GroundSurface (or any surface)
    exterior rings for the footprint. Falls back to the bbox polygon.

    The window is split into a grid of tiles and each tile is fetched by its own
    spatial.intersects('features', tileWKT) call in its own session across a thread
    pool: the graph R-tree prunes per tile, the per-tile traversals run server-side in
    parallel, and ring parsing overlaps with I/O. Features straddling a tile border
    come back twice and are deduped by id.
    """
    from shapely.geometry import Polygon, box
    minx, miny, maxx, maxy = bbox
    # spatial.intersects(layer, wkt) is the reliable candidate procedure (spatial.bbox
    # NPEs on WKT layers in some neo4j-spatial builds); GRAPH.md's overlap example uses it.
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

    tiles = []
    y = miny
    while y < maxy:
        x = minx
        while x < maxx:
            tiles.append((x, y, min(x + tile_m, maxx), min(y + tile_m, maxy)))
            x += tile_m
        y += tile_m

    def fetch_tile(t):
        tx0, ty0, tx1, ty1 = t
        wkt = (f"POLYGON(({tx0} {ty0}, {tx1} {ty0}, {tx1} {ty1}, "
               f"{tx0} {ty1}, {tx0} {ty0}))")
        part = {}
        with driver.session(database="neo4j") as s:
            for r in s.run(q, wkt=wkt, ft=target_type):
                poly = None
                for pl in (r["rings"] or []):
                    ring = _ring_xy(pl)
                    if len(ring) >= 4:
                        try:
                            p = Polygon(ring)
                            if not p.is_valid:
                                p = p.buffer(0)
                            if p.is_valid and p.area > 0:
                                poly = p if poly is None else poly.union(p)
                        except Exception:
                            pass
                if poly is None and r["minx"] is not None:
                    poly = box(r["minx"], r["miny"], r["maxx"], r["maxy"])
                if poly is not None and not poly.is_empty:
                    part[r["id"]] = poly
        return part

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(fetch_tile, tiles):
            for pid, poly in part.items():
                out.setdefault(pid, poly)   # dedupe straddlers
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

    Candidate cadastral footprints are already scoped to the OSM coverage window by the
    graph R-tree (fetch_pykci_footprints uses spatial.intersects on the 'features' layer);
    within that window the fine matching is one shapely 2.x BULK spatial join: a single
    STRtree.query(all_osm, predicate='intersects') produces the candidate-pair arrays in
    C, and intersection/area/length run vectorized over the pairs (GIL-releasing GEOS
    loops), so whole-city fusion (hundreds of thousands of footprints per side) takes
    minutes instead of an O(|OSM| × |pykci|) Python cross product.

      edges:      list of {osm, pykci, overlap_ratio, jaccard, inter_area}
      components: list of (set_osm_ids, set_pykci_ids) connected components
    """
    if not osm_geoms or not pykci_polys:
        return [], []
    oids = list(osm_geoms.keys())
    ogeoms = shapely.make_valid(np.array([osm_geoms[k] for k in oids], dtype=object))
    pids = list(pykci_polys.keys())
    pgeoms = shapely.make_valid(np.array([pykci_polys[k] for k in pids], dtype=object))

    tree = STRtree(pgeoms)
    qi, ti = tree.query(ogeoms, predicate="intersects")   # candidate pairs, bulk (C loop)
    if len(qi) == 0:
        return [], []

    o_sel, p_sel = ogeoms[qi], pgeoms[ti]
    inter_geom = shapely.intersection(o_sel, p_sel)
    oa, pa = shapely.area(o_sel), shapely.area(p_sel)
    olen = shapely.length(o_sel)
    inter_area, inter_len = shapely.area(inter_geom), shapely.length(inter_geom)

    is_line = (oa == 0) & (olen > 0)
    denom = np.minimum(oa, pa)
    ratio = np.where(
        is_line,
        np.divide(inter_len, olen, out=np.zeros_like(olen), where=olen > 0),
        np.divide(inter_area, denom, out=np.zeros_like(denom), where=denom > 0))
    union = oa + pa - inter_area
    jacc = np.where(
        is_line, ratio,
        np.divide(inter_area, union, out=np.zeros_like(union), where=union > 0))
    inter_val = np.where(is_line, inter_len, inter_area)
    keep = (inter_val > 0) & (ratio >= tau)

    edges = []
    adj = defaultdict(set)  # node-key -> neighbours (bipartite, keys are ('o',id)/('p',id))
    for k in np.flatnonzero(keep):
        oid, pid = oids[qi[k]], pids[ti[k]]
        edges.append({"osm": oid, "pykci": pid, "overlap_ratio": float(ratio[k]),
                      "jaccard": float(jacc[k]), "inter_area": float(inter_val[k])})
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

    # group edges per component ONCE (an edge's component is its osm endpoint's
    # component) — re-filtering all edges inside the per-component loop is
    # O(components × edges), which at whole-city scale (hundreds of thousands of each)
    # is the difference between seconds and days.
    comp_of = {}
    for ci, (osm_ids, _) in enumerate(comps):
        for oid in osm_ids:
            comp_of[oid] = ci
    edges_by_comp = defaultdict(list)
    for e in edges:
        edges_by_comp[comp_of[e["osm"]]].append(e)

    rels, scalar = [], defaultdict(dict)
    for ci, (osm_ids, pyk_ids) in enumerate(comps):
        case = classify_case(len(osm_ids), len(pyk_ids))
        comp_edges = edges_by_comp.get(ci, [])

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


def write_enrichment(driver, rels, scalar, target_label):
    # Relationship MERGEs are written SERIALLY (workers=1): a dense city (Tokyo) has
    # many OSM features matching the same building (n:1 / n:m), so parallel chunks
    # MERGE-ing ENRICHED_BY edges contend on the shared building's / OSM node's
    # relationship group and deadlock (NODE_RELATIONSHIP_GROUP_DELETE) — and because
    # the match is bipartite, no row partition makes the chunks node-disjoint. Serial
    # is deadlock-free and cheap (edge writes are a small fraction of fusion time,
    # which is dominated by node+geometry writing). Sorted by building so each
    # building's edges are contiguous (locality). The scalar SET has one row per
    # building (node-disjoint) so it stays parallel.
    rels_sorted = sorted(rels, key=lambda e: e["pykci"])
    _batch_parallel(driver,
                    f"UNWIND $rows AS row MATCH (b:{target_label} {{id: row.pykci}}), "
                    "(o:OsmFeature {id: row.osm}) "
                    "MERGE (b)-[r:ENRICHED_BY]->(o) "
                    "SET r.overlap_ratio = row.overlap_ratio, r.jaccard = row.jaccard, "
                    "r.intersection_area = row.inter_area, r.match_type = row.match_type, "
                    "r.is_primary = row.is_primary", rels_sorted, workers=1)
    scalar_rows = [{"id": pid, "props": props} for pid, props in scalar.items()]
    _batch_parallel(driver,
                    f"UNWIND $rows AS row MATCH (b:{target_label} {{id: row.id}}) "
                    "SET b += row.props", scalar_rows)


def write_pois(driver, poi_rows):
    """Attach point POIs to the building whose footprint contains them."""
    # Serial (workers=1) + building-sorted for the same reason as write_enrichment:
    # many POIs share a containing building, so parallel HAS_POI MERGEs deadlock on
    # the building's relationship group on a dense city.
    poi_sorted = sorted(poi_rows, key=lambda r: r["building"])
    _batch_parallel(driver,
                    "UNWIND $rows AS row MATCH (b:Building {id: row.building}), "
                    "(o:OsmFeature {id: row.osm}) "
                    "MERGE (b)-[r:HAS_POI]->(o) SET r.distance_m = row.distance_m",
                    poi_sorted, workers=1)


def index_cityobjects_post_fusion(sess, layer="features", batch=2000):
    """Add non-Building CityObject footprints (roads, bridges, city furniture,
    vegetation, water) to the CityGML 'features' R-tree AFTER OSM fusion, so they
    are spatially queryable alongside buildings.

    Deliberately post-fusion, not in ingest_citygml: the OSM matcher pulls
    candidate footprints from this same 'features' layer, and CityObjects present
    during fusion make its per-tile spatial.intersects (roads' bbox rectangles span
    many match tiles) and its per-type match phases explode on a dense multi-module
    city (Tokyo). A CityObject's bbox is computed from its own already-reprojected
    graph geometry (GeometryRing/GeometryLineString pos_list; x=idx0, y=idx1, stride
    3) when it has none; only non-degenerate boxes are written/indexed. Serial +
    idempotent. No-op for building-only cities (few/zero CityObjects).
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

    # 1. Compute bbox_wkt for CityObjects that lack one, from their graph geometry.
    need = [r["id"] for r in sess.run(
        "MATCH (c:CityObject) WHERE c.bbox_wkt IS NULL RETURN c.id AS id")]
    for i in range(0, len(need), batch):
        rows = sess.run(
            "UNWIND $ids AS cid MATCH (c:Entity {id: cid}) "
            "OPTIONAL MATCH (c)-[*1..6]->(g) "
            "WHERE (g:GeometryRing OR g:GeometryLineString) AND g.pos_list IS NOT NULL "
            "RETURN cid AS id, collect(g.pos_list) AS pl", ids=need[i:i + batch]).data()
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
            sess.run(
                "UNWIND $rows AS row MATCH (n:Entity {id: row.id}) "
                "SET n.bbox_wkt=row.wkt, n.bbox_min_x=row.minx, n.bbox_min_y=row.miny, "
                "n.bbox_min_z=row.minz, n.bbox_max_x=row.maxx, n.bbox_max_y=row.maxy, "
                "n.bbox_max_z=row.maxz, n.center_x=row.cx, n.center_y=row.cy", rows=writes)

    # 2. Index every CityObject that has a bbox into the 'features' R-tree (serial).
    ids = [r["id"] for r in sess.run(
        "MATCH (c:CityObject) WHERE c.bbox_wkt IS NOT NULL RETURN c.id AS id")]
    indexed = 0
    for i in range(0, len(ids), batch):
        r = sess.run(
            "UNWIND $ids AS nid MATCH (n:Entity {id: nid}) WITH collect(n) AS nodes "
            "CALL spatial.addNodes($layer, nodes) YIELD count RETURN count AS n",
            ids=ids[i:i + batch], layer=layer).single()
        indexed += r["n"] if r else 0
    return indexed


# ── main pipeline ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Ingest OSM (.osm.pbf) enrichment into the pykci graph.")
    ap.add_argument("pbf", help="Path to .osm.pbf")
    ap.add_argument("--dataset", help="Target Dataset name/id (required if >1 dataset present)")
    ap.add_argument("--buffer", type=float, default=10.0, help="Clip buffer in metres (default 10)")
    ap.add_argument("--overlap-threshold", type=float, default=0.3,
                    help="Min overlap ratio (intersection / min-area) to accept a match (default 0.3)")
    ap.add_argument("--workers", type=int, default=None,
                    help="Parallel Neo4j sessions for reads/writes (default 6)")
    args = ap.parse_args()
    if args.workers:
        globals()["WORKERS"] = args.workers

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
        minx, miny, maxx, maxy, recomputed = resolve_extent(sess, ds)
        if recomputed:
            print("  NOTE: stored Dataset bbox was degenerate; using the true feature extent.")
        print(f"  Target dataset '{ds['name']}'  CRS={crs}  "
              f"bbox=({minx:.1f},{miny:.1f})-({maxx:.1f},{maxy:.1f})")

        # city bbox in WGS84 = dataset bbox + buffer, inverse-transformed
        fwd = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        b = args.buffer
        lo = fwd.transform(minx - b, miny - b)
        hi = fwd.transform(maxx + b, maxy + b)
        spat = (min(lo[0], hi[0]), min(lo[1], hi[1]), max(lo[0], hi[0]), max(lo[1], hi[1]))
        match_bbox = (minx - b, miny - b, maxx + b, maxy + b)
        print(f"  WGS84 city bbox: {spat}")

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
            print("  No OSM features in the city bbox; nothing to do.")
            driver.close(); return

        # ── write OsmFeature nodes + geometry ─────────────────────────────────
        t0 = time.perf_counter()
        print(f"  Writing OsmFeature nodes + geometry ({WORKERS} parallel sessions) …")
        write_osm_nodes(driver, sess, nodes, polys, rings, foot_rels, ext_rels, int_rels,
                        lines, line_rels, dataset_id, district_id)
        print(f"  … nodes + geometry written in {time.perf_counter()-t0:.0f}s")
        ensure_spatial_layer(sess)
        t0 = time.perf_counter()
        n_idx = index_osm_spatial(sess)
        print(f"  Indexed {n_idx} OsmFeatures in 'osm_features' R-tree "
              f"({time.perf_counter()-t0:.0f}s).")

        # ── match area/line features per target type ──────────────────────────
        matched_osm_ids = set()
        case_counts = defaultdict(int)
        buildings_enriched = 0
        building_footprints = None  # cached Building footprints, reused by the POI phase
        empty_targets = set()      # (target_label, target_ft) with no pykci footprints in area
        for (target_label, target_ft), osm_geoms in area_geoms.items():
            t0 = time.perf_counter()
            pykci_polys = fetch_pykci_footprints(driver, target_ft if target_ft else target_label,
                                                 match_bbox)
            if not pykci_polys:
                empty_targets.add((target_label, target_ft))
                print(f"    {target_label}/{target_ft}: no pykci footprints in area "
                      f"({len(osm_geoms)} OSM kept as net-new orphan nodes)")
                continue
            if target_label == "Building":
                building_footprints = pykci_polys   # reuse for the POI phase
            edges, comps = match_areas(osm_geoms, pykci_polys, tau)
            rels, scalar = build_enrich_writes(edges, comps, osm_props_by_id, target_label)
            write_enrichment(driver, rels, scalar, target_label)
            for e in edges:
                matched_osm_ids.add(e["osm"])
            for osm_ids, pyk_ids in comps:
                case_counts[classify_case(len(osm_ids), len(pyk_ids))] += 1
            if target_label == "Building":
                buildings_enriched += len(scalar)
            print(f"    {target_label}/{target_ft}: {len(edges)} edges, "
                  f"{len(comps)} components, {len(scalar)} pykci features enriched "
                  f"({time.perf_counter()-t0:.0f}s)")

        # ── POIs: point-in-footprint (vectorized bulk containment) ────────────
        pois_attached = 0
        if point_pois:
            t0 = time.perf_counter()
            bld = (building_footprints if building_footprints
                   else fetch_pykci_footprints(driver, "Building", match_bbox))
            bids = list(bld.keys())
            bgeoms = np.array([bld[b] for b in bids], dtype=object)
            pts = np.array([pt for _, pt in point_pois], dtype=object)
            tree = STRtree(bgeoms)
            # one bulk R-tree join: every (POI, containing building) pair in a C loop —
            # not the O(points × buildings) Python cross product
            pi, bi = tree.query(pts, predicate="within")
            poi_rows, seen_pt = [], set()
            for k in range(len(pi)):
                p_idx = int(pi[k])
                if p_idx in seen_pt:
                    continue        # keep first containing building per POI
                seen_pt.add(p_idx)
                osm_node_id, pt = point_pois[p_idx]
                g = bgeoms[bi[k]]
                poi_rows.append({"building": bids[int(bi[k])], "osm": osm_node_id,
                                 "distance_m": round(pt.distance(g.centroid), 2)})
                matched_osm_ids.add(osm_node_id)
            write_pois(driver, poi_rows)
            pois_attached = len(poi_rows)
            print(f"    POIs: {pois_attached} attached to buildings "
                  f"(of {len(point_pois)} points, {time.perf_counter()-t0:.0f}s)")

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
            _batch_parallel(driver, "UNWIND $rows AS row MATCH (o:OsmFeature {id: row}) "
                                    "SET o.matched = false, o.unmatched_reason = $reason",
                            ids, reason=reason)
        _batch_parallel(driver, "UNWIND $rows AS row MATCH (o:OsmFeature {id: row}) "
                                "SET o.matched = true, o.unmatched_reason = null",
                        list(matched_osm_ids))

        # ── add CityObject footprints to the CityGML R-tree (post-fusion) ──────
        # Non-Building modules are indexed only now that the building-centric OSM
        # match is done, so their footprints never enter the matcher's candidate
        # lookup (see index_cityobjects_post_fusion). No-op for building-only cities.
        t0 = time.perf_counter()
        n_co = index_cityobjects_post_fusion(sess)
        if n_co:
            print(f"  Indexed {n_co} CityObject footprint(s) in the CityGML 'features' "
                  f"R-tree, post-fusion ({time.perf_counter()-t0:.0f}s).")

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
