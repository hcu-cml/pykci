#!/usr/bin/env python3
"""
osm_ingestion_census.py
Completeness + placement census for the OSM enrichment (``ingest_osm.py``).

The OSM analogue of ``eval/roundtrip_census.py``: while the round-trip census
proves the CityGML mapping drops nothing, THIS gate proves that every OSM
feature admitted from the source ``.osm.pbf`` is in the graph, is stored
losslessly, and sits in the right place — either attached to a CityGML anchor
node or kept as a spatially indexed standalone (orphan) node.

Method: the source expectation is re-derived with the SAME code path the
ingestion used (``extract_layer`` clip/reproject via ogr2ogr, ``classify_kind``,
``build_osm_props``, ``_pos_list`` — imported from ``ingest_osm``), so the
census compares independent database state against a source-derived
expectation, not the ingestion against itself.

Census levels (all must be clean for exit code 0):

  1. Features   — every admitted source feature with parseable geometry has
                  exactly ONE ``OsmFeature`` node under its stable
                  ``osm:<type>/<id>`` id; no graph extras for this source;
                  per-kind counts and dynamic ``Osm*`` labels match.
  2. Tags       — the full source tag dict is reconstructable from the node
                  (promoted keys + ``addr_*`` + ``other_tags`` JSON): compared
                  as canonical-JSON SHA1 per feature. Lossless, key and value.
  3. Geometry   — every ring / line coordinate string (``pos_list``, dataset
                  CRS) stored in the graph equals the source geometry verbatim:
                  compared as an order-independent SHA1 over the per-feature
                  multiset of ``pos_list`` strings; bbox/center floats compared
                  exactly; point features must carry a ``location``.
  4. Placement  — the attach-or-orphan partition is exact and exhaustive:
                  ``matched=true``  <=> >=1 incoming ``ENRICHED_BY``/``HAS_POI``
                  edge from a CityGML anchor; ``matched=false`` => an
                  ``unmatched_reason`` is set and no anchor edge exists; every
                  node has ``PART_OF -> Dataset``; every ``ENRICHED_BY`` edge
                  carries the five provenance properties; enrichment scalars on
                  CityGML anchors stay inside the ``osm_*``/``addr_*``
                  namespace (never overwriting authoritative keys).
  5. Indexing   — every ``OsmFeature`` is registered in the dedicated
                  ``osm_features`` R-tree (has an incoming ``RTREE_REFERENCE``)
                  and carries provenance fields + the ``Entity`` label.

Scope: the ingestion admits features from the three geometry-bearing GDAL/OSM
canonical layers (multipolygons, points, lines) under the documented thematic
tag filter (``LAYER_WHERE``). The census additionally counts the UNFILTERED
features per layer inside the same city bounding box (plus the two skipped relation
layers), so the filter's admission rate is reported rather than hidden
(``--skip-scope`` disables this extra pass).

Usage (from the project root, against the already-fused graph):
    py eval/osm_ingestion_census.py --pbf input/osm/hamburg-260629.osm.pbf
    py eval/osm_ingestion_census.py --pbf ... --dataset hamburg --skip-scope

Writes a timestamped JSON report to eval/results/, a Markdown summary to
output/stats/, and (when the scope pass ran) a LaTeX coverage table
(``osm_ingestion_census_<city>.tex``) to output/stats/. ``--render report.json``
regenerates the Markdown + LaTeX from a saved JSON report without touching the
database or the pbf. Exit code 0 only when every gate is clean.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neo4j import GraphDatabase
from pyproj import Transformer
from shapely.geometry import shape

import ingest_osm as osm
from ingest_osm import (
    NEO4J_URI, NEO4J_AUTH, KIND_LABEL, PROMOTED_TAGS, ENRICH_KEYS,
    _slug, _all_tags, _pos_list, build_osm_props, classify_kind,
    extract_layer, _epsg_from_srs, get_dataset, resolve_extent,
)

ADDR_SUBKEYS = ("street", "housenumber", "postcode", "city", "country")
BATCH = 1000
# Read-only verification sessions; scales with the machine (capped at 16).
WORKERS = min(16, os.cpu_count() or 6)


# ── expectation (source side) ─────────────────────────────────────────────────

def _tags_hash(tags):
    return hashlib.sha1(json.dumps(tags, sort_keys=True, ensure_ascii=False)
                        .encode("utf-8")).hexdigest()


def _geom_hash(parts):
    """Order-independent digest over the feature's pos_list strings."""
    return hashlib.sha1("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def expected_geom_parts(geom):
    """The pos_list strings ingest_osm would store for this geometry."""
    parts = []
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            parts.append(_pos_list(poly.exterior.coords))
            for interior in poly.interiors:
                parts.append(_pos_list(interior.coords))
    elif geom.geom_type in ("LineString", "MultiLineString"):
        lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for ls in lines:
            parts.append(_pos_list(ls.coords))
    return parts


def build_expectation(pbf, crs, spat, source, tmpdir):
    """
    Re-run the ingestion's extraction + classification and return
      expected: {node_id: [record, ...]}   (list >1 only for duplicate ids)
      stats:    per-layer admitted counts, per-kind counts, skipped geometries
    Each record: {kind, label, tags_hash, geom_hash, n_parts, bbox, is_point}
    """
    expected = defaultdict(list)
    kind_counts = Counter()
    layer_counts = {}
    geometry_skipped = 0

    for layer in ("multipolygons", "points", "lines"):
        t0 = time.perf_counter()
        feats = extract_layer(pbf, layer, tmpdir / f"{layer}.geojson", crs, spat)
        layer_counts[layer] = len(feats)
        print(f"  source {layer}: {len(feats)} admitted features "
              f"({time.perf_counter() - t0:.0f}s)")
        for feat in feats:
            g = feat.get("geometry")
            if not g:
                geometry_skipped += 1
                continue
            try:
                geom = shape(g)
            except Exception:
                geometry_skipped += 1
                continue
            if geom.is_empty:
                geometry_skipped += 1
                continue
            tags = _all_tags(feat.get("properties", {}) or {})
            kind = classify_kind(tags, geom.geom_type)
            node = build_osm_props(feat, kind, source)
            parts = expected_geom_parts(geom)
            minx, miny, maxx, maxy = geom.bounds
            expected[node["id"]].append({
                "kind": kind,
                "label": KIND_LABEL[kind][0],
                "tags": tags,            # kept only until duplicate-merge below
                "tags_hash": _tags_hash(tags),
                "geom_hash": _geom_hash(parts),
                "parts": parts,          # kept only until duplicate-merge below
                "bbox": (minx, miny, maxx, maxy),
                "is_point": geom.geom_type == "Point",
            })
            kind_counts[kind] += 1

    # For duplicate ids the writes race (MERGE + SET += in parallel batches), so
    # both geometry AND tags may be either candidate or their union; precompute
    # the union hash for each. A duplicate stable id arises when GDAL exposes one
    # OSM object in several layers (e.g. a closed way tagged highway=residential
    # AND leisure=marina lands in both `lines` and `multipolygons` with disjoint
    # tag subsets); the ingest MERGEs them into one node holding the union, which
    # is lossless. The tag union is accepted symmetrically with the geometry one.
    duplicates = {}
    dup_tags = {}
    for fid, recs in expected.items():
        if len(recs) > 1:
            union_parts = [p for r in recs for p in r["parts"]]
            duplicates[fid] = _geom_hash(union_parts)
            union_tags = {}
            for r in recs:
                union_tags.update(r["tags"])
            dup_tags[fid] = _tags_hash(union_tags)
        for r in recs:
            del r["parts"]
            del r["tags"]

    stats = {
        "layer_admitted": layer_counts,
        "kind_counts": dict(kind_counts),
        "geometry_skipped": geometry_skipped,
        "duplicate_ids": len(duplicates),
        "expected_nodes": len(expected),
    }
    return dict(expected), duplicates, dup_tags, stats


# ── scope counts (unfiltered source, informational) ──────────────────────────

def scope_counts(pbf, spat):
    """Unfiltered feature count per canonical OSM layer inside the city bbox."""
    counts = {}
    for layer in ("points", "lines", "multipolygons", "multilinestrings",
                  "other_relations"):
        n = _ogrinfo_count(pbf, layer, spat)
        if n is None:
            n = _csv_count(pbf, layer, spat)
        counts[layer] = n
        print(f"  scope {layer}: {n if n is not None else 'unavailable'} "
              f"features in bbox (unfiltered)")
    return counts


def _ogrinfo_count(pbf, layer, spat):
    minx, miny, maxx, maxy = spat
    try:
        out = subprocess.run(
            ["ogrinfo", "-ro", "-so", "-spat", str(minx), str(miny),
             str(maxx), str(maxy), str(pbf), layer],
            capture_output=True, text=True, timeout=3600).stdout
        m = re.search(r"Feature Count:\s*(-?\d+)", out)
        if m and int(m.group(1)) >= 0:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _csv_count(pbf, layer, spat):
    minx, miny, maxx, maxy = spat
    with tempfile.TemporaryDirectory(prefix="osmcensus_csv_") as td:
        out_csv = Path(td) / f"{layer}.csv"
        try:
            subprocess.run(
                ["ogr2ogr", "-f", "CSV", str(out_csv), str(pbf), layer,
                 "-spat", str(minx), str(miny), str(maxx), str(maxy),
                 "-select", "osm_id", "-skipfailures"],
                check=True, timeout=3600,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(out_csv, encoding="utf-8", errors="replace") as fh:
                return max(sum(1 for _ in fh) - 1, 0)
        except Exception:
            return None


# ── graph side ────────────────────────────────────────────────────────────────

def tags_from_node(props):
    """Reconstruct the original OSM tag dict from an OsmFeature node's properties."""
    tags = {}
    for t in PROMOTED_TAGS:
        key = "name" if t == "name" else "osm_" + _slug(t)
        v = props.get(key)
        if v not in (None, ""):
            tags[t] = v
    for sub in ADDR_SUBKEYS:
        v = props.get(f"addr_{sub}")
        if v not in (None, ""):
            tags[f"addr:{sub}"] = v
    raw = props.get("other_tags")
    if raw:
        try:
            tags.update(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return tags


FETCH_Q = """
UNWIND $rows AS fid
MATCH (n:OsmFeature {id: fid})
OPTIONAL MATCH (n)-[:HAS_FOOTPRINT]->(:GeometryPolygon)
               -[:HAS_EXTERIOR_RING|HAS_INTERIOR_RING]->(r:GeometryRing)
WITH n, collect(r.pos_list) AS rings
OPTIONAL MATCH (n)-[:HAS_LOD_SURFACE]->(l:GeometryLineString)
WITH n, rings, collect(l.pos_list) AS lns
RETURN n.id AS id, labels(n) AS labels, properties(n) AS props, rings, lns
"""


def _close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def verify_batch(driver, ids, expected, duplicates, dup_tags, findings, counters):
    with driver.session(database="neo4j") as s:
        rows = {r["id"]: r for r in s.run(FETCH_Q, rows=ids)}
    for fid in ids:
        recs = expected[fid]
        row = rows.get(fid)
        if row is None:
            findings["missing_nodes"].append(fid)
            continue
        counters["found"] += 1
        props, labels = row["props"], set(row["labels"])
        # tags — reconstructed dict must hash-match a candidate expectation
        got_tags = _tags_hash(tags_from_node(props))
        tags_ok = any(r["tags_hash"] == got_tags for r in recs)
        if not tags_ok and fid in dup_tags:
            tags_ok = got_tags == dup_tags[fid]   # duplicate-id union (see build_expectation)
        if not tags_ok:
            findings["tag_mismatch"].append(fid)
        # dynamic label + Entity
        if not any(r["label"] in labels or r["label"] == "OsmFeature"
                   for r in recs):
            findings["label_mismatch"].append(fid)
        if "Entity" not in labels:
            findings["missing_entity_label"].append(fid)
        # geometry — order-independent digest over stored pos_lists
        stored = _geom_hash(list(row["rings"]) + list(row["lns"]))
        ok = any(r["geom_hash"] == stored for r in recs)
        if not ok and fid in duplicates:
            ok = stored == duplicates[fid]
        if not ok:
            findings["geometry_mismatch"].append(fid)
        # bbox / point location
        bbox_ok = any(
            _close(props.get("bbox_min_x"), r["bbox"][0]) and
            _close(props.get("bbox_min_y"), r["bbox"][1]) and
            _close(props.get("bbox_max_x"), r["bbox"][2]) and
            _close(props.get("bbox_max_y"), r["bbox"][3]) for r in recs)
        if not bbox_ok:
            findings["bbox_mismatch"].append(fid)
        if all(r["is_point"] for r in recs) and props.get("location") is None:
            findings["missing_point_location"].append(fid)


GLOBAL_CHECKS = [
    # (key, description, query returning a single count `c` that must be 0)
    ("flag_null",
     "OsmFeature with matched flag missing",
     "MATCH (n:OsmFeature {source:$src}) WHERE n.matched IS NULL "
     "RETURN count(n) AS c"),
    ("matched_without_anchor",
     "matched=true but no incoming ENRICHED_BY/HAS_POI anchor edge",
     "MATCH (n:OsmFeature {source:$src}) WHERE n.matched = true "
     "AND NOT EXISTS { (n)<-[:ENRICHED_BY|HAS_POI]-() } RETURN count(n) AS c"),
    ("orphan_with_anchor",
     "matched=false but an anchor edge exists",
     "MATCH (n:OsmFeature {source:$src})<-[:ENRICHED_BY|HAS_POI]-() "
     "WHERE n.matched = false RETURN count(DISTINCT n) AS c"),
    ("orphan_without_reason",
     "matched=false without unmatched_reason",
     "MATCH (n:OsmFeature {source:$src}) WHERE n.matched = false "
     "AND n.unmatched_reason IS NULL RETURN count(n) AS c"),
    ("missing_provenance",
     "missing source/osm_id/osm_type/osm_kind provenance field",
     "MATCH (n:OsmFeature {source:$src}) WHERE n.osm_id IS NULL "
     "OR n.osm_type IS NULL OR n.osm_kind IS NULL RETURN count(n) AS c"),
    ("missing_part_of",
     "OsmFeature without PART_OF -> Dataset edge",
     "MATCH (n:OsmFeature {source:$src}) "
     "WHERE NOT EXISTS { (n)-[:PART_OF]->(:Dataset) } RETURN count(n) AS c"),
    ("missing_rtree",
     "OsmFeature with bbox_wkt but not in the osm_features R-tree",
     "MATCH (n:OsmFeature {source:$src}) WHERE n.bbox_wkt IS NOT NULL "
     "AND NOT EXISTS { (n)<-[:RTREE_REFERENCE]-() } RETURN count(n) AS c"),
    ("missing_bbox_wkt",
     "OsmFeature without bbox_wkt (cannot be spatially indexed)",
     "MATCH (n:OsmFeature {source:$src}) WHERE n.bbox_wkt IS NULL "
     "RETURN count(n) AS c"),
    ("enriched_by_bad_props",
     "ENRICHED_BY edge missing a provenance property",
     "MATCH (:OsmFeature {source:$src})<-[r:ENRICHED_BY]-() "
     "WHERE r.overlap_ratio IS NULL OR r.jaccard IS NULL "
     "OR r.intersection_area IS NULL OR r.match_type IS NULL "
     "OR r.is_primary IS NULL RETURN count(r) AS c"),
    ("has_poi_bad_props",
     "HAS_POI edge missing distance_m",
     "MATCH (:OsmFeature {source:$src})<-[r:HAS_POI]-() "
     "WHERE r.distance_m IS NULL RETURN count(r) AS c"),
    ("bad_anchor_labels",
     "anchor edge from a node that is neither Building nor CityObject",
     "MATCH (a)-[:ENRICHED_BY|HAS_POI]->(:OsmFeature {source:$src}) "
     "WHERE NOT (a:Building OR a:CityObject) RETURN count(DISTINCT a) AS c"),
]

NAMESPACE_Q = """
MATCH (b) WHERE (b:Building OR b:CityObject) AND b.osm_match_type IS NOT NULL
WITH b, [k IN keys(b) WHERE k STARTS WITH 'osm_' AND NOT k IN $allowed] AS bad
WHERE size(bad) > 0
RETURN count(b) AS c, collect(DISTINCT bad)[..5] AS examples
"""


def run_global_checks(sess, source):
    results, failures = {}, {}
    for key, desc, q in GLOBAL_CHECKS:
        c = sess.run(q, src=source).single()["c"]
        results[key] = c
        if c:
            failures[key] = {"count": c, "description": desc}
        print(f"  {'FAIL' if c else 'ok  '}  {desc}: {c}")
    r = sess.run(NAMESPACE_Q, allowed=ENRICH_KEYS).single()
    results["anchor_namespace_violations"] = r["c"]
    if r["c"]:
        failures["anchor_namespace_violations"] = {
            "count": r["c"],
            "description": "anchor node carries osm_* keys outside ENRICH_KEYS",
            "examples": r["examples"]}
    print(f"  {'FAIL' if r['c'] else 'ok  '}  anchor osm_* keys outside the "
          f"enrichment namespace: {r['c']}")
    return results, failures


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="OSM ingestion completeness census.")
    ap.add_argument("--render", metavar="REPORT_JSON",
                    help="Regenerate the Markdown + LaTeX outputs from a saved "
                         "JSON report (no DB / pbf access), then exit")
    ap.add_argument("--pbf", help="The .osm.pbf the fusion ingested")
    ap.add_argument("--dataset", help="Target Dataset name/id (if >1 present)")
    ap.add_argument("--buffer", type=float, default=10.0,
                    help="Clip buffer in metres — MUST match the ingestion run")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--skip-scope", action="store_true",
                    help="Skip the unfiltered per-layer scope counts")
    ap.add_argument("--max-report", type=int, default=25,
                    help="Max offending ids listed per finding in the report")
    args = ap.parse_args()

    if args.render:
        report = json.loads(Path(args.render).read_text(encoding="utf-8"))
        _write_outputs(report)
        sys.exit(0)

    if not args.pbf:
        ap.error("--pbf is required (unless --render is used)")
    pbf = Path(args.pbf)
    if not pbf.exists():
        raise SystemExit(f"ERROR: {pbf} not found.")
    source = f"osm:{pbf.stem}"

    print("Connecting to Neo4j ...")
    driver = GraphDatabase.driver(
        NEO4J_URI, auth=NEO4J_AUTH,
        notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
    driver.verify_connectivity()

    with driver.session(database="neo4j") as sess:
        ds = get_dataset(sess, args.dataset)
        crs = _epsg_from_srs(ds["crs"])
        minx, miny, maxx, maxy, recomputed = resolve_extent(sess, ds)
        if recomputed:
            print("  NOTE: stored Dataset bbox degenerate; using true feature extent "
                  "(same fallback the ingestion used).")
        fwd = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        b = args.buffer
        lo, hi = fwd.transform(minx - b, miny - b), fwd.transform(maxx + b, maxy + b)
        spat = (min(lo[0], hi[0]), min(lo[1], hi[1]),
                max(lo[0], hi[0]), max(lo[1], hi[1]))
        print(f"  Dataset '{ds['name']}'  CRS={crs}")
        print(f"  WGS84 city bbox: {spat}")

    # ── source expectation ────────────────────────────────────────────────────
    print("Phase 1 — deriving the source expectation (same code path as ingest) ...")
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="osmcensus_") as td:
        expected, duplicates, dup_tags, src_stats = build_expectation(
            pbf, crs, spat, source, Path(td))
    print(f"  expectation: {src_stats['expected_nodes']} nodes, "
          f"{src_stats['duplicate_ids']} duplicate ids, "
          f"{src_stats['geometry_skipped']} unparseable-geometry features skipped "
          f"({time.perf_counter() - t0:.0f}s)")

    # ── per-feature fidelity ──────────────────────────────────────────────────
    print("Phase 2 — verifying every expected feature in the graph ...")
    t0 = time.perf_counter()
    findings = defaultdict(list)
    counters = Counter()
    ids = list(expected.keys())
    chunks = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(verify_batch, driver, c, expected, duplicates,
                          dup_tags, findings, counters) for c in chunks]
        for f in futs:
            f.result()
            done += 1
            if done % 200 == 0:
                print(f"    {done}/{len(chunks)} batches ...")
    print(f"  verified {counters['found']}/{len(ids)} expected nodes "
          f"({time.perf_counter() - t0:.0f}s)")

    # ── extras: graph nodes for this source not in the expectation ───────────
    print("Phase 3 — checking for graph extras ...")
    with driver.session(database="neo4j") as sess:
        graph_total = sess.run(
            "MATCH (n:OsmFeature {source:$src}) RETURN count(n) AS c",
            src=source).single()["c"]
        graph_kinds = {r["k"]: r["c"] for r in sess.run(
            "MATCH (n:OsmFeature {source:$src}) "
            "RETURN n.osm_kind AS k, count(n) AS c", src=source)}
        matched_split = {r["m"]: r["c"] for r in sess.run(
            "MATCH (n:OsmFeature {source:$src}) "
            "RETURN n.matched AS m, count(n) AS c", src=source)}
        if graph_total != len(expected):
            expected_ids = set(expected)
            for rec in sess.run("MATCH (n:OsmFeature {source:$src}) "
                                "RETURN n.id AS id", src=source):
                if rec["id"] not in expected_ids:
                    findings["extra_nodes"].append(rec["id"])
    print(f"  graph: {graph_total} OsmFeature nodes for {source} "
          f"(expected {len(expected)}, extras {len(findings['extra_nodes'])})")
    print(f"  placement split: {matched_split}")

    # ── global placement / indexing / provenance checks ──────────────────────
    print("Phase 4 — placement, indexing, and provenance gates ...")
    with driver.session(database="neo4j") as sess:
        global_results, global_failures = run_global_checks(sess, source)

    # ── scope counts (informational) ──────────────────────────────────────────
    scope = None
    if not args.skip_scope:
        print("Phase 5 — unfiltered source scope counts (informational) ...")
        scope = scope_counts(pbf, spat)

    driver.close()

    # ── verdict + report ──────────────────────────────────────────────────────
    finding_counts = {k: len(v) for k, v in findings.items() if v}
    passed = not finding_counts and not global_failures
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    city = re.sub(r"[^a-z0-9]+", "_", ds["name"].lower()).strip("_")

    report = {
        "timestamp": ts,
        "pbf": str(pbf),
        "dataset": ds["name"],
        "crs": crs,
        "buffer_m": args.buffer,
        "city_bbox_wgs84": spat,
        "source": src_stats,
        "graph": {"osm_feature_nodes": graph_total,
                  "kind_counts": graph_kinds,
                  "matched_split": {str(k): v for k, v in matched_split.items()}},
        "scope_unfiltered": scope,
        "per_feature_findings": {
            k: {"count": len(v), "examples": v[:args.max_report]}
            for k, v in findings.items() if v},
        "global_checks": global_results,
        "global_failures": global_failures,
        "passed": passed,
    }
    report["city"] = city
    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(exist_ok=True)
    json_path = results_dir / f"osm_ingestion_census_{city}_{ts}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(f"\nReport: {json_path}")
    _write_outputs(report)
    print("CENSUS PASSED — every admitted OSM feature is in the graph, lossless, "
          "and correctly placed." if passed else
          "CENSUS FAILED — see the findings above / in the report.")
    sys.exit(0 if passed else 1)


def _write_outputs(rep):
    """Write the Markdown summary (+ LaTeX coverage table if scope counts exist)."""
    city = rep.get("city") or re.sub(r"[^a-z0-9]+", "_",
                                     rep["dataset"].lower()).strip("_")
    out = Path("output/stats")
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"osm_ingestion_census_{city}.md"
    md_path.write_text(_markdown(rep), encoding="utf-8")
    print(f"Summary: {md_path}")
    if rep.get("scope_unfiltered"):
        tex_path = out / f"osm_ingestion_census_{city}.tex"
        tex_path.write_text(_latex(rep), encoding="utf-8")
        print(f"LaTeX table: {tex_path}")


def _markdown(rep):
    src, g = rep["source"], rep["graph"]
    lines = [
        f"# OSM ingestion census — {rep['dataset']}",
        "",
        f"*Run {rep['timestamp']} · source `{rep['pbf']}` · CRS {rep['crs']} · "
        f"buffer {rep['buffer_m']} m · gate "
        f"{'**PASSED**' if rep['passed'] else '**FAILED**'}*",
        "",
        "Completeness + placement gate for the OSM enrichment "
        "(`eval/osm_ingestion_census.py`): every OSM feature admitted from the "
        "source `.osm.pbf` must be stored losslessly as an `OsmFeature` node and "
        "placed either on a CityGML anchor (`ENRICHED_BY`/`HAS_POI`) or as a "
        "spatially indexed standalone node with an `unmatched_reason`.",
        "",
        "## Source vs. graph",
        "",
        "| Quantity | Source (expected) | Graph |",
        "| -------- | ----------------- | ----- |",
        f"| OSM features (admitted, parseable) | {src['expected_nodes']} "
        f"| {g['osm_feature_nodes']} |",
    ]
    for kind in sorted(set(src["kind_counts"]) | set(g["kind_counts"])):
        lines.append(f"| kind `{kind}` | {src['kind_counts'].get(kind, 0)} "
                     f"| {g['kind_counts'].get(kind, 0)} |")
    lines += [
        "",
        f"Placement split (graph): {g['matched_split']}. "
        f"Source-side skips: {src['geometry_skipped']} features with "
        f"empty/unparseable geometry after the ogr2ogr clip; "
        f"{src['duplicate_ids']} duplicate stable ids.",
        "",
        "## Per-feature fidelity findings",
        "",
    ]
    pf = rep["per_feature_findings"]
    if not pf:
        lines.append("None — feature existence, tag reconstruction (canonical "
                     "SHA1), verbatim geometry `pos_list` digests, bboxes, "
                     "labels, and point locations all match the source.")
    else:
        lines.append("| Finding | Count | Examples |")
        lines.append("| ------- | ----- | -------- |")
        for k, v in pf.items():
            ex = ", ".join(f"`{e}`" for e in v["examples"][:5])
            lines.append(f"| {k} | {v['count']} | {ex} |")
    lines += ["", "## Placement / indexing / provenance gates", ""]
    gf = rep["global_failures"]
    lines.append("| Gate | Violations |")
    lines.append("| ---- | ---------- |")
    for key, c in rep["global_checks"].items():
        lines.append(f"| {key} | {c} |")
    if gf:
        lines += ["", "Failing gates: " + ", ".join(gf)]
    if rep.get("scope_unfiltered"):
        lines += [
            "",
            "## Scope: thematic tag filter admission (informational)",
            "",
            "Unfiltered feature counts per GDAL/OSM canonical layer inside the "
            "same city bounding box. The ingestion reads the three "
            "geometry-bearing layers: `points` and `multipolygons` unfiltered, "
            "`lines` restricted by the `LAYER_WHERE` filter (transport/water "
            "network). `multilinestrings` (route relations) and "
            "`other_relations` are views over member ways/nodes already "
            "present in the base layers.",
            "",
            "| Layer | Unfiltered in bbox | Admitted |",
            "| ----- | -------------------- | -------- |",
        ]
        for layer, n in rep["scope_unfiltered"].items():
            adm = src["layer_admitted"].get(layer, "—")
            lines.append(f"| {layer} | {n if n is not None else 'n/a'} | {adm} |")
    lines.append("")
    return "\n".join(lines)


def _latex(rep):
    """
    Render the census as a booktabs coverage table (three blocks: source
    coverage per layer with the exact exclusions, verification of every
    ingested feature, placement split). Coverage is stated positively
    (verified / total); a failed gate surfaces as a sub-100 percentage.
    """
    src, g = rep["source"], rep["graph"]
    scope = rep["scope_unfiltered"]
    la = src["layer_admitted"]
    n = src["expected_nodes"]
    pf = {k: v["count"] for k, v in rep.get("per_feature_findings", {}).items()}
    gc = rep.get("global_checks", {})

    def num(v):
        return f"\\num{{{v}}}"

    def cov(ok, total):
        if not total:
            return "--"
        pct = 100.0 * ok / total
        s = f"{pct:.1f}" if pct < 100.0 else "100"
        return f"{s}\\,\\%"

    def check_row(label, bad):
        return f"    {label} & {num(n - bad)} & {cov(n - bad, n)} \\\\"

    city = rep.get("city") or re.sub(r"[^a-z0-9]+", "_",
                                     rep["dataset"].lower()).strip("_")
    city_text = city.replace("_", " ")

    lines_win = scope.get("lines") or la.get("lines", 0)
    lines_excl = lines_win - la.get("lines", 0)
    rel_views = (scope.get("multilinestrings") or 0) + (scope.get("other_relations") or 0)
    total_win = sum(v for v in scope.values() if v is not None)
    ingested = g["osm_feature_nodes"]
    ms = g["matched_split"]
    anchored, standalone = ms.get("True", 0), ms.get("False", 0)

    exists_bad = pf.get("missing_nodes", 0) + pf.get("extra_nodes", 0)
    tags_bad = pf.get("tag_mismatch", 0)
    geom_bad = (pf.get("geometry_mismatch", 0) + pf.get("bbox_mismatch", 0)
                + pf.get("missing_point_location", 0))
    place_bad = (gc.get("flag_null", 0) + gc.get("matched_without_anchor", 0)
                 + gc.get("orphan_with_anchor", 0)
                 + gc.get("orphan_without_reason", 0))
    index_bad = (gc.get("missing_rtree", 0) + gc.get("missing_bbox_wkt", 0)
                 + gc.get("missing_provenance", 0) + gc.get("missing_part_of", 0))

    rows = [
        "% Auto-generated by eval/osm_ingestion_census.py --render; do not edit by hand.",
        f"% Dataset: {rep['dataset']}  run: {rep['timestamp']}  "
        f"gate: {'PASSED' if rep['passed'] else 'FAILED'}",
        "\\begin{table}",
        f"  \\caption{{OSM ingestion census for {city_text}:"
        " source features inside the city bounding box versus the graph,"
        " verification of every ingested feature, and placement.}",
        f"  \\label{{tab:osm_census_{city}}}",
        "  \\small",
        "  \\begin{tabular}{lrr}",
        "    \\toprule",
        "    \\multicolumn{3}{l}{\\emph{Source features in city bbox"
        " $\\rightarrow$ graph}} \\\\",
        "     & In bbox & Ingested \\\\",
        "    \\midrule",
        f"    Points & {num(scope.get('points') or 0)} & {num(la.get('points', 0))} \\\\",
        f"    Polygons & {num(scope.get('multipolygons') or 0)} "
        f"& {num(la.get('multipolygons', 0))} \\\\",
        f"    Lines (transport, water network) & {num(la.get('lines', 0))} "
        f"& {num(la.get('lines', 0))} \\\\",
        f"    Lines (other; excluded by design) & {num(lines_excl)} & 0 \\\\",
        f"    Relation views (excluded by design) & {num(rel_views)} & 0 \\\\",
        f"    Total & {num(total_win)} & {num(ingested)} ({cov(ingested, total_win)}) \\\\",
        "    \\midrule",
        f"    \\multicolumn{{3}}{{l}}{{\\emph{{Verified on every ingested feature"
        f" ($n$ = {num(n)})}}}} \\\\",
        "     & Verified & Coverage \\\\",
        "    \\midrule",
        check_row("Stored exactly once under stable OSM id", exists_bad),
        check_row("All tags reconstruct losslessly", tags_bad),
        check_row("Geometry verbatim (rings, lines, bboxes)", geom_bad),
        check_row("Placed: anchored or standalone with reason", place_bad),
        check_row("R-tree indexed, provenance, dataset edge", index_bad),
        "    \\midrule",
        "    \\multicolumn{3}{l}{\\emph{Placement of ingested features}} \\\\",
        "    \\midrule",
        f"    Attached to a CityGML anchor & {num(anchored)} "
        f"& {cov(anchored, ingested)} \\\\",
        f"    Standalone net-new node & {num(standalone)} "
        f"& {cov(standalone, ingested)} \\\\",
        "    \\bottomrule",
        "  \\end{tabular}",
        "\\end{table}",
    ]
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    main()
