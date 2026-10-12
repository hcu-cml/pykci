#!/usr/bin/env python3
"""
osm_tau_sweep.py
Read-only, city-scale sweep of the OSM footprint-matching threshold tau
(``ingest_osm.py --overlap-threshold``).

Why: the default tau = 0.3 was tuned on the HafenCity pilot tile (81 buildings,
OSM_INTEGRATION.md section 14). This script repeats the sweep on a whole city
graph with a documented grid and keeps the sweep reproducible.

Method (mirrors ``ingest_osm.main()`` up to the matching step, WITHOUT any of
its writes; nothing in ``ingest_osm.py`` is modified, its functions are
imported the way ``eval/osm_ingestion_census.py`` does):

  1. Extent: ``get_dataset`` + ``resolve_extent``; the WGS84 clip window and
     ``match_bbox`` are computed exactly as in ``main()`` (buffer default 10 m,
     the ingest default used for the released graphs).
  2. OSM side: ``extract_layer`` for multipolygons / points / lines, then
     ``classify_kind`` + ``KIND_LABEL`` routing exactly as in ``main()``; only
     the ``("Building", None)`` match key is kept (this key also holds the
     POI polygons, as in the ingest).
  3. pykci side: ``fetch_pykci_footprints(driver, "Building", match_bbox)``,
     once, through read-access sessions.
  4. ``match_areas(osm_geoms, pykci_polys, tau=0.0)`` once: every pair with a
     positive overlap, with overlap_ratio / jaccard / inter_area. Steps 2-4 are
     cached (pickle, outside the repo) so reruns go straight to step 5.
  5. Per tau: keep the edges with overlap_ratio >= tau (order preserved),
     recompute the bipartite connected components with the same logic as
     ``match_areas``, and call ``build_enrich_writes(edges, comps, {},
     "Building")`` for ``is_primary`` / ``match_type``.

Filtering the tau = 0 edge list is exactly ``match_areas(..., tau)``: the
latter keeps ``inter_val > 0 and ratio >= tau`` from the same candidate
arrays, in the same order.

Validation gate: at tau = 0.30 the sweep must reproduce the graph's
``ENRICHED_BY`` edges exactly (edge count, enriched buildings, matched OSM
features, primary edges, per-edge match_type distribution); an edge-level
diff against the stored edges is reported as well. On failure the script
writes a diagnostic JSON to the cache dir and exits 2 without writing the
outputs.

Read-only guarantees: every session is opened with READ access mode; the
total node and relationship counts are recorded before and after the run and
must be identical (exit 3 otherwise). The script never calls
``ingest_osm.main``, ``reset_osm``, ``ensure_indexes``,
``ensure_spatial_layer``, ``index_osm_spatial`` or any ``write_*`` function.

Usage (from the project root; ogr2ogr from GDAL on PATH):
    python eval/osm_tau_sweep.py --pbf input/osm/hamburg-260629.osm.pbf \
        --cache-dir <dir outside the repo>
    python eval/osm_tau_sweep.py ... --uri bolt://localhost:7687 --city hamburg

Outputs: output/stats/osm_tau_sweep_<city>.{json,csv,md} (+ a PNG of enriched
buildings and mean primary Jaccard vs tau when matplotlib is importable).
"""

import argparse
import csv
import hashlib
import json
import os
import pickle
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import neo4j
import numpy as np
import shapely
from neo4j import GraphDatabase, READ_ACCESS
from pyproj import Transformer
from shapely.geometry import shape

import ingest_osm as osm
from ingest_osm import (
    NEO4J_URI, NEO4J_AUTH, KIND_LABEL,
    _all_tags, _epsg_from_srs, classify_kind, extract_layer,
    get_dataset, resolve_extent, fetch_pykci_footprints, match_areas,
    build_enrich_writes, classify_case,
)

# tau = 0.01, then 0.05 .. 0.95 in steps of 0.05 (20 values). Rounded so the
# grid values are the exact decimal literals (0.15, not 0.15000000000000002).
TAU_GRID = [0.01] + [round(0.05 * k, 2) for k in range(1, 20)]
MAIN_RANGE = (0.10, 0.70)          # the paper's range -> the main Markdown table
REFERENCE_TAU = 0.30               # the released default; validation + stability anchor
PAIRWISE = [(0.10, 0.30), (0.10, 0.50), (0.30, 0.50), (0.10, 0.70), (0.30, 0.70)]
EXTRA_TAU = 0.10                   # "why not this lower tau": what it adds over the reference
TARGET_KEY = ("Building", None)
CACHE_VERSION = 1


# ── read-only plumbing ──────────────────────────────────────────────────────────

class ReadOnlyDriver:
    """Driver proxy whose sessions default to READ access mode, so even the
    imported helpers (fetch_pykci_footprints opens its own sessions) run in
    read transactions; the server rejects any write in such a transaction."""

    def __init__(self, driver):
        self._driver = driver

    def session(self, **kw):
        kw.setdefault("database", "neo4j")
        kw["default_access_mode"] = READ_ACCESS
        return self._driver.session(**kw)


COUNT_QUERIES = {
    "nodes": "MATCH (n) RETURN count(n) AS c",
    "relationships": "MATCH ()-[r]->() RETURN count(r) AS c",
    "Building": "MATCH (b:Building) RETURN count(b) AS c",
    "OsmFeature": "MATCH (o:OsmFeature) RETURN count(o) AS c",
    "ENRICHED_BY": "MATCH ()-[r:ENRICHED_BY]->() RETURN count(r) AS c",
    "HAS_POI": "MATCH ()-[r:HAS_POI]->() RETURN count(r) AS c",
    "Dataset": "MATCH (d:Dataset) RETURN count(d) AS c",
}


def graph_counts(sess):
    return {k: sess.run(q).single()["c"] for k, q in COUNT_QUERIES.items()}


BASELINE_Q = """
MATCH (b:Building)-[r:ENRICHED_BY]->(o:OsmFeature)
RETURN count(r) AS edges, count(DISTINCT b) AS buildings, count(DISTINCT o) AS osm,
       sum(CASE WHEN r.is_primary THEN 1 ELSE 0 END) AS primary_edges
"""
MATCH_TYPE_Q = """
MATCH (:Building)-[r:ENRICHED_BY]->(:OsmFeature)
RETURN r.match_type AS t, count(r) AS c ORDER BY t
"""
EDGES_Q = """
MATCH (b:Building)-[r:ENRICHED_BY]->(o:OsmFeature)
RETURN b.id AS pykci, o.id AS osm, r.overlap_ratio AS ratio, r.jaccard AS jaccard,
       r.intersection_area AS inter, r.match_type AS match_type, r.is_primary AS is_primary
"""


# fetch_pykci_footprints unions a Building's GroundSurface exterior rings and falls
# back to its axis-aligned bbox rectangle when there are none (e.g. LoD1-only solids,
# geometry held only in BuildingParts), which changes how overlap ratios behave.
FOOTPRINT_Q = """
MATCH (b:Building)
RETURN count(b) AS buildings,
       count(CASE WHEN EXISTS { (b)-[:HAS_BOUNDARY]->(:BoundarySurface {surface_type: 'GroundSurface'}) }
                  THEN 1 END) AS with_ground_surface
"""


def graph_baseline(sess):
    r = sess.run(BASELINE_Q).single()
    base = {"edges": r["edges"], "enriched_buildings": r["buildings"],
            "matched_osm": r["osm"], "primary_edges": r["primary_edges"]}
    base["match_type_edges"] = {rec["t"]: rec["c"] for rec in sess.run(MATCH_TYPE_Q)}
    return base


# ── environment / provenance ────────────────────────────────────────────────────

def _cmd_out(cmd, cwd=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                              timeout=60).stdout.strip()
    except Exception:
        return None


def sha256_file(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def environment(driver):
    with driver.session() as s:
        comp = s.run("CALL dbms.components() YIELD name, versions, edition "
                     "WHERE name = 'Neo4j Kernel' "
                     "RETURN name, versions[0] AS v, edition").single()
    status = _cmd_out(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT)
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "gdal_ogr2ogr": _cmd_out(["ogr2ogr", "--version"]),
        "ogr2ogr_path": shutil.which("ogr2ogr"),
        "gdal_data": os.environ.get("GDAL_DATA"),
        "proj_lib": os.environ.get("PROJ_LIB") or os.environ.get("PROJ_DATA"),
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "numpy": np.__version__,
        "neo4j_python_driver": neo4j.__version__,
        "neo4j_server": f"{comp['name']} {comp['v']} {comp['edition']}" if comp else None,
        "git_commit": _cmd_out(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "git_dirty_tracked_files": len(status.splitlines()) if status else 0,
        "ingest_osm_sha256": sha256_file(ROOT / "ingest_osm.py"),
    }


# ── steps 2-4: OSM extraction, pykci footprints, tau = 0 match (cached) ─────────

def osm_building_geoms(pbf, crs, spat, workdir, source):
    """Replicates main()'s read + classify loop, keeping only the
    ("Building", None) bucket of area_geoms (keys are independent, so this is
    identical to building every bucket and selecting one). Same insertion
    order, same overwrite semantics for duplicate stable ids."""
    all_features, layer_counts = [], {}
    for layer in ("multipolygons", "points", "lines"):
        t0 = time.perf_counter()
        feats = extract_layer(pbf, layer, workdir / f"{layer}.geojson", crs, spat)
        layer_counts[layer] = len(feats)
        print(f"  ogr2ogr {layer}: {len(feats)} features ({time.perf_counter() - t0:.0f}s)")
        all_features += [(layer, f) for f in feats]

    geoms = {}
    kind_counts = Counter()
    key_by_geom = Counter()      # what landed in the Building key, by kind/geom type
    n_nodes = 0
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
        _label, target_label, target_ft = KIND_LABEL[kind]
        node_id = osm.build_osm_props(feat, kind, source)["id"]
        kind_counts[kind] += 1
        n_nodes += 1
        if not target_label or (target_label, target_ft) != TARGET_KEY:
            continue
        if geom.geom_type in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"):
            geoms[node_id] = geom
            key_by_geom[f"{kind}/{geom.geom_type}"] += 1
    stats = {"layer_features": layer_counts, "kind_counts": dict(kind_counts),
             "osm_nodes_incl_duplicates": n_nodes,
             "building_key_features": len(geoms),
             "building_key_by_kind_geom": dict(key_by_geom)}
    return geoms, stats


def cache_key(meta):
    blob = json.dumps(meta, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def load_or_build_pairs(args, driver, ro, crs, spat, match_bbox, source, counts, env):
    """Return (pairs, osm_ids, pykci_ids, osm_stats, timings). `pairs` is the
    tau = 0 edge list from match_areas, in match_areas order."""
    pbf = Path(args.pbf)
    key_meta = {
        "v": CACHE_VERSION, "pbf_sha256": env["pbf_sha256"], "crs": crs,
        "spat": list(spat), "match_bbox": list(match_bbox), "buffer": args.buffer,
        "gdal": env["gdal_ogr2ogr"], "geos": env["geos"],
        "ingest_osm_sha256": env["ingest_osm_sha256"],
        "graph_nodes": counts["nodes"], "graph_rels": counts["relationships"],
        "graph_buildings": counts["Building"],
    }
    key = cache_key(key_meta)
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cpath = cache_dir / f"tau0_pairs_{key}.pkl"
    if cpath.exists() and not args.rebuild_cache:
        print(f"Steps 2-4: using cached tau=0 pairs {cpath}")
        with open(cpath, "rb") as fh:
            c = pickle.load(fh)
        return c["pairs"], c["osm_ids"], c["pykci_ids"], c["osm_stats"], c["timings"], str(cpath)

    timings = {}
    print("Step 2: OSM extraction + classification (same code path as ingest_osm.main) ...")
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ogr_", dir=cache_dir) as td:
        osm_geoms, osm_stats = osm_building_geoms(pbf, crs, spat, Path(td), source)
    timings["osm_extract_s"] = round(time.perf_counter() - t0, 1)
    print(f"  Building-key OSM features: {len(osm_geoms)}  ({timings['osm_extract_s']}s)")

    print("Step 3: fetching pykci Building footprints (read-only) ...")
    t0 = time.perf_counter()
    pykci_polys = fetch_pykci_footprints(ro, "Building", match_bbox)
    timings["pykci_fetch_s"] = round(time.perf_counter() - t0, 1)
    print(f"  pykci footprints: {len(pykci_polys)}  ({timings['pykci_fetch_s']}s)")

    print("Step 4: match_areas(tau=0.0) -> every positive-overlap pair ...")
    t0 = time.perf_counter()
    edges, _comps = match_areas(osm_geoms, pykci_polys, tau=0.0)
    timings["match_tau0_s"] = round(time.perf_counter() - t0, 1)
    print(f"  tau=0 pairs: {len(edges)}  ({timings['match_tau0_s']}s)")

    pairs = [(e["osm"], e["pykci"], e["overlap_ratio"], e["jaccard"], e["inter_area"])
             for e in edges]
    payload = {"meta": key_meta, "pairs": pairs, "osm_ids": list(osm_geoms),
               "pykci_ids": list(pykci_polys), "osm_stats": osm_stats, "timings": timings}
    with open(cpath, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"  cached -> {cpath}")
    return pairs, list(osm_geoms), list(pykci_polys), osm_stats, timings, str(cpath)


# ── step 5: per-tau evaluation ──────────────────────────────────────────────────

def components(edges):
    """Bipartite connected components; same adjacency + DFS as match_areas."""
    adj = defaultdict(set)
    for e in edges:
        adj[("o", e["osm"])].add(("p", e["pykci"]))
        adj[("p", e["pykci"])].add(("o", e["osm"]))
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
        comps.append(({k[1] for k in comp if k[0] == "o"},
                      {k[1] for k in comp if k[0] == "p"}))
    return comps


def attribute_source(edges):
    """pykci id -> the edge whose OSM feature build_enrich_writes copies osm_*
    scalars from: the edge with the largest inter_area (first maximum in edge
    order, like the built-in max() used there)."""
    best = {}
    for e in edges:
        b = best.get(e["pykci"])
        if b is None or e["inter_area"] > b["inter_area"]:
            best[e["pykci"]] = e
    return best


def _pct(a, b):
    return round(100.0 * a / b, 3) if b else None


def _quantile(sorted_vals, q):
    """Linear-interpolation quantile (numpy default) on a sorted list."""
    if not sorted_vals:
        return None
    return float(np.quantile(np.asarray(sorted_vals), q))


def evaluate_tau(tau, all_edges, n_buildings_total, n_osm_key):
    t0 = time.perf_counter()
    edges = [e for e in all_edges if e["overlap_ratio"] >= tau]
    comps = components(edges)
    rels, scalar = build_enrich_writes(edges, comps, {}, "Building")

    enriched = set(scalar)
    matched_osm = {e["osm"] for e in edges}
    prim_j = sorted(r["jaccard"] for r in rels if r["is_primary"])
    n_edges = len(rels)
    case_comps = Counter(classify_case(len(o), len(p)) for o, p in comps)
    nm = [(len(p), len(o)) for o, p in comps if classify_case(len(o), len(p)) == "n:m"]
    largest = max(nm, key=lambda t: (t[0] + t[1], t[0]), default=(0, 0))

    row = {
        "tau": tau,
        "edges": n_edges,
        "enriched_buildings": len(enriched),
        "enriched_buildings_pct": _pct(len(enriched), n_buildings_total),
        "matched_osm": len(matched_osm),
        "matched_osm_pct": _pct(len(matched_osm), n_osm_key),
        "primary_edges": len(prim_j),
        "primary_jaccard_mean": round(statistics.fmean(prim_j), 4) if prim_j else None,
        "primary_jaccard_median": round(_quantile(prim_j, 0.5), 4) if prim_j else None,
        "primary_jaccard_p10": round(_quantile(prim_j, 0.1), 4) if prim_j else None,
        "edges_jaccard_lt_0_2_pct": _pct(sum(r["jaccard"] < 0.2 for r in rels), n_edges),
        "edges_jaccard_ge_0_5_pct": _pct(sum(r["jaccard"] >= 0.5 for r in rels), n_edges),
        "edges_per_enriched_building": round(n_edges / len(enriched), 4) if enriched else None,
        "components": len(comps),
        "comp_1_1": case_comps.get("1:1", 0),
        "comp_1_n": case_comps.get("1:n", 0),
        "comp_n_1": case_comps.get("n:1", 0),
        "comp_n_m": case_comps.get("n:m", 0),
        "buildings_in_n_m": sum(b for b, _ in nm),
        "largest_n_m_buildings": largest[0],
        "largest_n_m_osm": largest[1],
        "match_type_edges": dict(sorted(Counter(r["match_type"] for r in rels).items())),
    }
    row["_seconds"] = round(time.perf_counter() - t0, 1)
    return row, rels, enriched, attribute_source(edges), comps


def stability(enr_a, src_a, enr_b, src_b):
    both = enr_a & enr_b
    same = sum(1 for pid in both if src_a[pid]["osm"] == src_b[pid]["osm"])
    return {"gained": len(enr_b - enr_a), "lost": len(enr_a - enr_b),
            "both": len(both), "same_source": same,
            "same_source_pct": _pct(same, len(both))}


# ── threshold diagnostics (why this tau and not a lower / higher one) ────────────

RATIO_BINS = [0.0, 0.01] + [round(0.05 * k, 2) for k in range(1, 20)] + [1.0]
RATIO_BANDS = [(0.0, 0.10), (0.10, 0.25), (0.25, 0.45), (0.45, 0.90), (0.90, 1.01)]


def osm_owner(all_edges):
    """OSM id -> the pykci building it shares the largest intersection with
    (over all positive-overlap pairs). A building whose attribute source is
    'owned' by another building is taking a neighbour's OSM outline."""
    owner = {}
    for e in all_edges:
        o = owner.get(e["osm"])
        if o is None or e["inter_area"] > o["inter_area"]:
            owner[e["osm"]] = e
    return {k: v["pykci"] for k, v in owner.items()}


def ratio_distribution(all_edges):
    """Overlap-ratio distribution of every candidate pair (tau = 0)."""
    r = np.array([e["overlap_ratio"] for e in all_edges])
    a = np.array([e["inter_area"] for e in all_edges])
    n = len(r)
    cnt, _ = np.histogram(np.minimum(r, 1.0), bins=RATIO_BINS)
    bins = [{"lo": lo, "hi": hi, "pairs": int(k), "pct": _pct(int(k), n)}
            for lo, hi, k in zip(RATIO_BINS[:-1], RATIO_BINS[1:], cnt)]
    bands = []
    for lo, hi in RATIO_BANDS:
        m = (r >= lo) & (r < hi)
        bands.append({"lo": lo, "hi": min(hi, 1.0), "pairs": int(m.sum()), "pct": _pct(int(m.sum()), n),
                      "median_shared_area_m2": round(float(np.median(a[m])), 2) if m.any() else None})
    dom = np.array([e["overlap_ratio"] for e in attribute_source(all_edges).values()])
    return {"pairs": n, "bins_0_05": bins, "bands": bands,
            "dominant_edge_ratio_median": round(float(np.median(dom)), 4) if len(dom) else None,
            "dominant_edges_below_ref_pct": _pct(int((dom < REFERENCE_TAU).sum()), len(dom))}


def step_diagnostics(taus, enriched, best, owner):
    """For each consecutive grid step a -> b: the buildings that lose their
    enrichment, split by whether the match they lose came from their own OSM
    outline or from a neighbour's (the OSM feature overlaps another building more)."""
    out = []
    for a, b in zip(taus, taus[1:]):
        dropped = [best[a][p] for p in enriched[a] - enriched[b]]
        neigh = [e for e in dropped if owner[e["osm"]] != e["pykci"]]
        own = [e for e in dropped if owner[e["osm"]] == e["pykci"]]
        out.append({"from": a, "to": b, "dropped": len(dropped),
                    "neighbour_source": len(neigh), "neighbour_pct": _pct(len(neigh), len(dropped)),
                    "own_source": len(own), "own_pct": _pct(len(own), len(dropped)),
                    "own_median_jaccard": round(float(np.median([e["jaccard"] for e in own])), 4)
                    if own else None})
    return out


def _case_by_building(comps):
    out = {}
    for o_ids, p_ids in comps:
        case = classify_case(len(o_ids), len(p_ids))
        for p in p_ids:
            out[p] = case
    return out


def extra_below_ref(lo, enriched, best, owner, comps_lo, comps_ref):
    """What tau = lo adds over the reference tau."""
    gained = [best[lo][p] for p in enriched[lo] - enriched[REFERENCE_TAU]]
    neigh = [e for e in gained if owner[e["osm"]] != e["pykci"]]
    c_lo, c_ref = _case_by_building(comps_lo), _case_by_building(comps_ref)
    regrouped = Counter(c_lo[p] for p, case in c_ref.items() if case == "1:1" and c_lo.get(p) != "1:1")
    return {
        "tau": lo, "extra_buildings": len(gained),
        "neighbour_source": len(neigh), "neighbour_pct": _pct(len(neigh), len(gained)),
        "median_shared_area_m2": round(float(np.median([e["inter_area"] for e in gained])), 2)
        if gained else None,
        "median_jaccard": round(float(np.median([e["jaccard"] for e in gained])), 4) if gained else None,
        "ref_1to1_regrouped": sum(regrouped.values()), "ref_1to1_regrouped_by_case": dict(regrouped),
    }


def edge_level_diff(rels_ref, graph_edges):
    """Compare the sweep's tau=0.30 edges with the stored ENRICHED_BY edges."""
    mine = {(r["pykci"], r["osm"]): r for r in rels_ref}
    theirs = {(g["pykci"], g["osm"]): g for g in graph_edges}
    only_sweep = sorted(set(mine) - set(theirs))
    only_graph = sorted(set(theirs) - set(mine))
    common = set(mine) & set(theirs)
    mt_diff = [k for k in common if mine[k]["match_type"] != theirs[k]["match_type"]]
    prim_diff = [k for k in common if bool(mine[k]["is_primary"]) != bool(theirs[k]["is_primary"])]
    dr = max((abs(mine[k]["overlap_ratio"] - (theirs[k]["ratio"] or 0.0)) for k in common), default=0.0)
    dj = max((abs(mine[k]["jaccard"] - (theirs[k]["jaccard"] or 0.0)) for k in common), default=0.0)
    da = max((abs(mine[k]["inter_area"] - (theirs[k]["inter"] or 0.0)) for k in common), default=0.0)
    pj_mine = sorted(r["jaccard"] for r in rels_ref if r["is_primary"])
    pj_graph = sorted(g["jaccard"] for g in graph_edges if g["is_primary"])
    # Tolerance, not ==: re-fetched footprints can union their GroundSurface rings in a
    # different order than at ingest, which moves areas by a few ulp (seen: <= 1e-11).
    pj_diff = (max((abs(x - y) for x, y in zip(pj_mine, pj_graph)), default=0.0)
               if len(pj_mine) == len(pj_graph) else None)
    summary = {
        "common_edges": len(common),
        "only_in_sweep": len(only_sweep), "only_in_graph": len(only_graph),
        "match_type_mismatch": len(mt_diff), "is_primary_mismatch": len(prim_diff),
        "max_abs_diff_overlap_ratio": dr, "max_abs_diff_jaccard": dj,
        "max_abs_diff_intersection_area": da,
        "primary_jaccard_multiset_identical": pj_diff is not None and pj_diff <= 1e-9,
        "primary_jaccard_multiset_max_abs_diff": pj_diff,
        "examples": {"only_in_sweep": only_sweep[:10], "only_in_graph": only_graph[:10],
                     "match_type_mismatch": mt_diff[:10], "is_primary_mismatch": prim_diff[:10]},
    }
    return summary, prim_diff


def explain_primary_flips(all_edges, flips):
    """is_primary can legitimately differ from the stored edge on an EXACT tie:
    in a 1:n component build_enrich_writes picks the primary building with
    max() over a Python set (order depends on the per-process hash seed), and
    elsewhere the largest-intersection edge with max() in edge order. Count how
    many flipped components contain such a tie."""
    edges = [e for e in all_edges if e["overlap_ratio"] >= REFERENCE_TAU]
    comps = components(edges)
    comp_of = {oid: ci for ci, (o_ids, _) in enumerate(comps) for oid in o_ids}
    by_comp = defaultdict(list)
    for e in edges:
        by_comp[comp_of[e["osm"]]].append(e)
    flip_comps = {comp_of[osm_id] for _pid, osm_id in flips}
    cases, tied = Counter(), 0
    for ci in flip_comps:
        o_ids, p_ids = comps[ci]
        case = classify_case(len(o_ids), len(p_ids))
        cases[case] += 1
        ce = by_comp[ci]
        if case == "1:n":
            best = max(e["jaccard"] for e in ce)
            tied += sum(e["jaccard"] == best for e in ce) > 1
        else:
            per = defaultdict(list)
            for e in ce:
                per[e["pykci"]].append(e["inter_area"])
            tied += any(v.count(max(v)) > 1 for v in per.values())
    return {"components_with_flips": len(flip_comps), "by_case": dict(cases),
            "explained_by_exact_tie": tied}


# ── outputs ─────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "tau", "edges", "enriched_buildings", "enriched_buildings_pct", "matched_osm",
    "matched_osm_pct", "primary_edges", "primary_jaccard_mean", "primary_jaccard_median",
    "primary_jaccard_p10", "edges_jaccard_lt_0_2_pct", "edges_jaccard_ge_0_5_pct",
    "edges_per_enriched_building", "components", "comp_1_1", "comp_1_n", "comp_n_1",
    "comp_n_m", "buildings_in_n_m", "largest_n_m_buildings", "largest_n_m_osm",
    "vs_ref_gained", "vs_ref_lost", "vs_ref_same_source_pct",
]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _f(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return f"{v:,}"


def markdown(rep):
    m, rows = rep["meta"], rep["sweep"]
    lo, hi = MAIN_RANGE
    main = [r for r in rows if lo - 1e-9 <= r["tau"] <= hi + 1e-9]
    nb = m["graph_counts_before"]["Building"]
    L = [
        f"# OSM matching-threshold sweep: {m['dataset']} (whole city)",
        "",
        f"*Generated by `eval/osm_tau_sweep.py` on {m['run_date']} · git `{m['env']['git_commit'][:7]}` · "
        f"pbf `{m['pbf']}` · buffer {m['buffer_m']} m · {m['env']['gdal_ogr2ogr']} · "
        f"GEOS {m['env']['geos']} · read-only (graph counts identical before/after: "
        f"{m['graph_unchanged']}).*",
        "",
        f"Grid: tau = 0.01, then 0.05 to 0.95 in steps of 0.05 (20 values). "
        f"Population: {nb:,} pykci buildings; {m['osm']['building_key_features']:,} OSM "
        f"features under the Building match key (building polygons + POI polygons). "
        f"Validation gate at tau = {REFERENCE_TAU:.2f}: **{'PASSED' if rep['validation']['passed'] else 'FAILED'}** "
        f"(the sweep reproduces the released graph's {rep['validation']['graph']['edges']:,} "
        f"`ENRICHED_BY` edges).",
        "",
    ]
    el = rep["validation"].get("edge_level")
    if el:
        fx = el.get("is_primary_flips_explained", {})
        L += [
            f"Edge-level check at tau = {REFERENCE_TAU:.2f}: {el['common_edges']:,} edges in common, "
            f"{el['only_in_sweep']} only in the sweep, {el['only_in_graph']} only in the graph, "
            f"{el['match_type_mismatch']} `match_type` mismatches; `overlap_ratio`, `jaccard` and "
            f"`intersection_area` max abs. difference {el['max_abs_diff_overlap_ratio']}, "
            f"{el['max_abs_diff_jaccard']}, {el['max_abs_diff_intersection_area']}. "
            f"{el['is_primary_mismatch']} `is_primary` flags differ, in "
            f"{fx.get('components_with_flips', 0)} components ({fx.get('by_case', {})}), "
            f"{fx.get('explained_by_exact_tie', 0)} of which contain an exact tie that "
            "`build_enrich_writes` breaks by Python set / edge order (the ingest itself is "
            "not deterministic there; the count varies between runs). The primary-edge count and "
            "the multiset of primary Jaccard values are "
            + (f"identical (max abs. difference {el.get('primary_jaccard_multiset_max_abs_diff')}, "
               "floating-point noise), so the precision proxy is unaffected."
               if el.get("primary_jaccard_multiset_identical") else
               "NOT identical within 1e-9; inspect before using the precision proxy."),
            "",
        ]
    L += [
        f"## Main table (tau {lo:.2f} to {hi:.2f})",
        "",
        "| tau | enriched bldgs | % of bldgs | matched OSM | % of OSM | edges | edges/bldg "
        "| primary J mean | median | p10 | J<0.2 edges | J>=0.5 edges | n:m comps | bldgs in n:m "
        f"| gained vs {REFERENCE_TAU:.2f} | lost vs {REFERENCE_TAU:.2f} | same attr. source |",
        "| --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for r in main:
        L.append(
            f"| {r['tau']:.2f} | {_f(r['enriched_buildings'])} | {_f(r['enriched_buildings_pct'], 2)} "
            f"| {_f(r['matched_osm'])} | {_f(r['matched_osm_pct'], 2)} | {_f(r['edges'])} "
            f"| {_f(r['edges_per_enriched_building'], 3)} | {_f(r['primary_jaccard_mean'], 3)} "
            f"| {_f(r['primary_jaccard_median'], 3)} | {_f(r['primary_jaccard_p10'], 3)} "
            f"| {_f(r['edges_jaccard_lt_0_2_pct'], 1)}% | {_f(r['edges_jaccard_ge_0_5_pct'], 1)}% "
            f"| {_f(r['comp_n_m'])} | {_f(r['buildings_in_n_m'])} | {_f(r['vs_ref_gained'])} "
            f"| {_f(r['vs_ref_lost'])} | {_f(r['vs_ref_same_source_pct'], 3)}% |")
    L += [
        "",
        f"*Same attr. source* = among buildings enriched at both tau and {REFERENCE_TAU:.2f}, the share whose "
        "`osm_*` scalars come from the same OSM feature (the one sharing the largest intersection area).",
        "",
        f"Structural ceiling: {rep['ceiling']['buildings_without_any_osm_overlap']:,} of {nb:,} buildings "
        f"({_f(rep['ceiling']['pct'], 2)}%) share no positive-area overlap with any OSM feature under the "
        f"Building key, so no tau can enrich them; the tau-free maximum is "
        f"{rep['ceiling']['max_enrichable']:,} buildings.",
        "",
        "Full grid (all 20 values), component cases, largest n:m component and run metadata: "
        f"`osm_tau_sweep_{m['city']}.csv` / `.json`.",
        "",
    ]
    d = rep.get("diagnostics")
    if d:
        fp, rd, ex = d["footprint_source"], d["ratio_distribution"], d["extra_below_ref"]
        L += [
            "## Threshold diagnostics",
            "",
            f"Footprints: {fp['with_ground_surface']:,} of {fp['buildings']:,} buildings "
            f"({_f(fp['ground_surface_pct'], 2)}%) are matched with their GroundSurface outline; the rest "
            "fall back to their axis-aligned bounding-box rectangle.",
            "",
            f"Overlap-ratio distribution of all {rd['pairs']:,} candidate pairs (tau = 0):",
            "",
            "| overlap ratio | pairs | share | median shared area (m2) |",
            "| --- | --: | --: | --: |",
        ]
        for bnd in rd["bands"]:
            L.append(f"| {bnd['lo']:.2f} to {bnd['hi']:.2f} | {bnd['pairs']:,} | {_f(bnd['pct'], 2)}% "
                     f"| {_f(bnd['median_shared_area_m2'], 2)} |")
        L += [
            "",
            f"Per building, the dominant (largest-intersection) pair has median ratio "
            f"{_f(rd['dominant_edge_ratio_median'], 3)}; {_f(rd['dominant_edges_below_ref_pct'], 2)}% "
            f"of dominant pairs lie below {REFERENCE_TAU:.2f}.",
            "",
            "Buildings that lose their enrichment at each step, by where the lost match came from "
            "(*neighbour* = the OSM feature overlaps another building more):",
            "",
            "| step | buildings dropped | neighbour's outline | own outline |",
            "| --- | --: | --: | --: |",
        ]
        for s in d["steps"]:
            if MAIN_RANGE[0] - 1e-9 <= s["from"] and s["to"] <= MAIN_RANGE[1] + 1e-9:
                L.append(f"| {s['from']:.2f} -> {s['to']:.2f} | {s['dropped']:,} "
                         f"| {_f(s['neighbour_pct'], 1)}% | {_f(s['own_pct'], 1)}% |")
        L += [
            "",
            f"tau = {ex['tau']:.2f} instead of {REFERENCE_TAU:.2f}: {ex['extra_buildings']:,} extra buildings "
            f"enriched, {_f(ex['neighbour_pct'], 1)}% of them from a neighbour's outline (median shared "
            f"area {_f(ex['median_shared_area_m2'], 1)} m2, median Jaccard {_f(ex['median_jaccard'], 3)}); "
            f"{ex['ref_1to1_regrouped']:,} buildings that are clean 1:1 matches at {REFERENCE_TAU:.2f} "
            f"are pulled into larger groups ({ex['ref_1to1_regrouped_by_case']}).",
            "",
        ]
    if rep.get("findings"):
        L += ["## Findings", ""] + [f"- {x}" for x in rep["findings"]] + [""]
    return "\n".join(L)


def try_plot(path, rows):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    taus = [r["tau"] for r in rows]
    fig, ax1 = plt.subplots(figsize=(6.4, 3.6))
    ax1.plot(taus, [r["enriched_buildings"] for r in rows], marker="o", color="#2f6db3",
             label="enriched buildings")
    ax1.set_xlabel("overlap threshold tau")
    ax1.set_ylabel("enriched buildings")
    ax2 = ax1.twinx()
    ax2.plot(taus, [r["primary_jaccard_mean"] for r in rows], marker="s", color="#c0612b",
             label="mean primary Jaccard")
    ax2.set_ylabel("mean primary Jaccard")
    ax1.axvline(REFERENCE_TAU, color="#888888", lw=0.8, ls="--")
    fig.legend(loc="lower left", bbox_to_anchor=(0.12, 0.15), frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return True


def findings(rep):
    """Short, data-derived findings for the Markdown summary."""
    by = {r["tau"]: r for r in rep["sweep"]}
    out = []
    lo, hi = MAIN_RANGE
    js = [by[t]["primary_jaccard_mean"] for t in by if lo - 1e-9 <= t <= hi + 1e-9]
    out.append(f"Mean primary Jaccard over tau {lo:.2f}-{hi:.2f}: {min(js):.3f} to {max(js):.3f} "
               f"(range {max(js) - min(js):.3f}).")
    a, b = by[0.10], by[REFERENCE_TAU]
    out.append(f"tau = {REFERENCE_TAU:.2f} keeps {b['enriched_buildings']:,} of the "
               f"{a['enriched_buildings']:,} buildings enriched at tau = 0.10 "
               f"({100.0 * b['enriched_buildings'] / a['enriched_buildings']:.2f}%).")
    p = rep["pairwise_stability"]["0.10->0.50"]
    out.append(f"Attribute source (largest-intersection OSM feature) between tau = 0.10 and 0.50: "
               f"unchanged for {p['same_source']:,} of {p['both']:,} buildings enriched at both "
               f"({_f(p['same_source_pct'], 2)}%); {p['lost']:,} buildings lose enrichment.")
    out.append(f"n:m components: {by[0.10]['comp_n_m']:,} at tau = 0.10 ({by[0.10]['buildings_in_n_m']:,} "
               f"buildings, largest {by[0.10]['largest_n_m_buildings']} buildings x "
               f"{by[0.10]['largest_n_m_osm']} OSM) vs {by[REFERENCE_TAU]['comp_n_m']:,} at "
               f"{REFERENCE_TAU:.2f} ({by[REFERENCE_TAU]['buildings_in_n_m']:,} buildings).")
    taus = sorted(t for t in by if lo - 1e-9 <= t <= hi + 1e-9)
    steps = [(a, b, by[a]["enriched_buildings"] - by[b]["enriched_buildings"],
              by[a]["comp_n_m"] - by[b]["comp_n_m"]) for a, b in zip(taus, taus[1:])]
    out.append("Per 0.05 step (enriched buildings lost / n:m components removed): "
               + "; ".join(f"{a:.2f}->{b:.2f}: {d:,} / {m:,}" for a, b, d, m in steps) + ".")
    c = rep["ceiling"]["max_enrichable"]
    out.append(f"tau = {REFERENCE_TAU:.2f} enriches {100.0 * b['enriched_buildings'] / c:.2f}% of the "
               f"{c:,} buildings that overlap any OSM feature at all (the tau-free ceiling).")
    d = rep.get("diagnostics")
    if d:
        cross = next((s for s in d["steps"] if s["dropped"] and s["own_pct"] > 50.0), None)
        out.append("Dropped matches turn from mostly neighbour outlines to mostly own outlines at step "
                   + (f"{cross['from']:.2f} -> {cross['to']:.2f} ({_f(cross['own_pct'], 1)}% own)."
                      if cross else "none in the grid (always mostly neighbour outlines)."))
        band = next(x for x in d["ratio_distribution"]["bands"] if x["lo"] == 0.25)
        out.append(f"Only {_f(band['pct'], 2)}% of candidate pairs have an overlap ratio between 0.25 and 0.45.")
    return out


def summarize(out_dir):
    """Cross-city table from the per-city JSON reports (no DB, no pbf)."""
    out_dir = Path(out_dir)
    reps = []
    for p in sorted(out_dir.glob("osm_tau_sweep_*.json")):
        rep = json.loads(p.read_text(encoding="utf-8"))
        if "sweep" in rep and "diagnostics" in rep:
            reps.append(rep)
    if not reps:
        raise SystemExit(f"No per-city reports with diagnostics in {out_dir}.")
    reps.sort(key=lambda r: -r["meta"]["graph_counts_before"]["Building"])

    def row(rep):
        by = {r["tau"]: r for r in rep["sweep"]}
        d = rep["diagnostics"]
        cross = next((s for s in d["steps"] if s["dropped"] and s["own_pct"] > 50.0), None)
        band = next(x for x in d["ratio_distribution"]["bands"] if x["lo"] == 0.25)
        ex = d["extra_below_ref"]
        keep = 100.0 * by[REFERENCE_TAU]["enriched_buildings"] / by[0.10]["enriched_buildings"]
        return (f"| {rep['meta']['city']} | {rep['meta']['graph_counts_before']['Building']:,} "
                f"| {_f(d['footprint_source']['ground_surface_pct'], 1)}% "
                f"| {_f(by[0.10]['enriched_buildings_pct'], 2)} / {_f(by[REFERENCE_TAU]['enriched_buildings_pct'], 2)}"
                f" / {_f(by[0.50]['enriched_buildings_pct'], 2)} "
                f"| {_f(by[0.10]['primary_jaccard_mean'], 3)} / {_f(by[REFERENCE_TAU]['primary_jaccard_mean'], 3)}"
                f" / {_f(by[0.70]['primary_jaccard_mean'], 3)} "
                f"| {keep:.2f}% | {_f(rep['pairwise_stability']['0.10->0.50']['same_source_pct'], 2)}% "
                f"| {_f(band['pct'], 2)}% | {ex['extra_buildings']:,} ({_f(ex['neighbour_pct'], 1)}%) "
                f"| {(cross['from'] if cross else float('nan')):.2f} "
                f"| {'passed' if rep['validation']['passed'] else 'FAILED'} |")

    L = [
        "# OSM matching-threshold sweep: cross-city summary",
        "",
        "*Generated by `eval/osm_tau_sweep.py --summarize` from the per-city reports "
        "`osm_tau_sweep_<city>.json` (each a read-only sweep of the released city graph, "
        f"grid tau = 0.01, 0.05 to 0.95 step 0.05; reference tau = {REFERENCE_TAU:.2f}).*",
        "",
        "| city | buildings | GroundSurface footprints | enriched % at 0.10 / 0.30 / 0.50 "
        "| mean primary J at 0.10 / 0.30 / 0.70 | 0.30 keeps of 0.10 | same source 0.10 vs 0.50 "
        "| pairs with ratio 0.25 to 0.45 | extra at 0.10 (from neighbour) | own-majority from tau | gate |",
        "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --- |",
    ] + [row(r) for r in reps] + [
        "",
        "*Extra at 0.10* = buildings enriched at tau = 0.10 but not at 0.30, with the share whose match "
        "comes from a neighbouring building's OSM outline. *Own-majority from tau* = first grid step at "
        "which most of the buildings dropped by raising tau were matched to their own outline (below it, "
        "raising tau mostly removes neighbour overlaps).",
        "",
    ]
    path = out_dir / "osm_tau_sweep_cities.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print(f"Wrote {path}")


# ── main ────────────────────────────────────────────────────────────────────────

def main():
    if "--summarize" in sys.argv:
        ap = argparse.ArgumentParser(description="Cross-city summary of osm_tau_sweep reports.")
        ap.add_argument("--summarize", action="store_true")
        ap.add_argument("--out-dir", default=str(ROOT / "output" / "stats"))
        summarize(ap.parse_args().out_dir)
        return
    ap = argparse.ArgumentParser(description="Read-only city-scale sweep of the OSM match threshold tau.")
    ap.add_argument("--summarize", action="store_true",
                    help="Only write output/stats/osm_tau_sweep_cities.md from existing city reports")
    ap.add_argument("--pbf", required=True, help="The .osm.pbf the fusion ingested")
    ap.add_argument("--dataset", help="Target Dataset name/id (if >1 present)")
    ap.add_argument("--city", help="Output name suffix (default: from the Dataset name)")
    ap.add_argument("--buffer", type=float, default=10.0,
                    help="Clip buffer in metres; MUST match the ingestion run (default 10)")
    ap.add_argument("--uri", default=NEO4J_URI, help=f"Bolt URI (default {NEO4J_URI})")
    ap.add_argument("--cache-dir", default=str(Path(tempfile.gettempdir()) / "pykci_osm_tau_sweep"),
                    help="Cache for steps 2-4; keep it OUTSIDE the OneDrive-synced repo")
    ap.add_argument("--rebuild-cache", action="store_true", help="Ignore an existing tau=0 cache")
    ap.add_argument("--out-dir", default=str(ROOT / "output" / "stats"))
    args = ap.parse_args()

    if not shutil.which("ogr2ogr"):
        raise SystemExit("ERROR: ogr2ogr (GDAL) not found on PATH.")
    if not os.environ.get("GDAL_DATA"):
        # extract_layer hides ogr2ogr's stderr; without osmconf.ini the OSM driver fails
        # with a bare CalledProcessError. QGIS: GDAL_DATA=<QGIS>/apps/gdal/share/gdal,
        # PROJ_LIB=<QGIS>/share/proj (or run via bench/timed_stage.py --env-qgis).
        print("WARNING: GDAL_DATA is not set; the OSM driver may not find osmconf.ini.")
    pbf = Path(args.pbf)
    if not pbf.exists():
        raise SystemExit(f"ERROR: {pbf} not found.")
    source = f"osm:{pbf.stem}"
    t_start = time.perf_counter()

    print(f"Connecting to Neo4j at {args.uri} (read-only sessions) ...")
    driver = GraphDatabase.driver(args.uri, auth=NEO4J_AUTH,
                                  notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
    driver.verify_connectivity()
    ro = ReadOnlyDriver(driver)

    env = environment(ro)
    print("  hashing pbf ...")
    env["pbf_sha256"] = sha256_file(pbf)
    env["pbf_bytes"] = pbf.stat().st_size

    with ro.session() as sess:
        counts_before = graph_counts(sess)
        print(f"  graph counts before: {counts_before}")
        ds = get_dataset(sess, args.dataset)
        crs = _epsg_from_srs(ds["crs"])
        minx, miny, maxx, maxy, recomputed = resolve_extent(sess, ds)
        # identical to ingest_osm.main(): dataset bbox + buffer, inverse-transformed
        fwd = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        b = args.buffer
        lo = fwd.transform(minx - b, miny - b)
        hi = fwd.transform(maxx + b, maxy + b)
        spat = (min(lo[0], hi[0]), min(lo[1], hi[1]), max(lo[0], hi[0]), max(lo[1], hi[1]))
        match_bbox = (minx - b, miny - b, maxx + b, maxy + b)
        print(f"  Dataset '{ds['name']}'  CRS={crs}  extent recomputed={recomputed}")
        print(f"  WGS84 clip window: {spat}")
        print("Step 1: baseline from the graph (released tau = 0.30) ...")
        baseline = graph_baseline(sess)
        print(f"  baseline: {baseline}")
        footprints = sess.run(FOOTPRINT_Q).single().data()
        footprints["ground_surface_pct"] = _pct(footprints["with_ground_surface"], footprints["buildings"])
        print(f"  footprint source: {footprints}")
    city = args.city or re.sub(r"[^a-z0-9]+", "_", ds["name"].lower()).strip("_")

    pairs, osm_ids, pykci_ids, osm_stats, timings, cpath = load_or_build_pairs(
        args, driver, ro, crs, spat, match_bbox, source, counts_before, env)
    all_edges = [{"osm": o, "pykci": p, "overlap_ratio": r, "jaccard": j, "inter_area": a}
                 for o, p, r, j, a in pairs]
    n_buildings = counts_before["Building"]
    n_osm_key = osm_stats["building_key_features"]

    print(f"Step 5: evaluating {len(TAU_GRID)} tau values on {len(all_edges):,} tau=0 pairs ...")
    rows, enriched, src, kept_comps = [], {}, {}, {}
    ref_rels = None
    for tau in TAU_GRID:
        row, rels, enr, asrc, comps = evaluate_tau(tau, all_edges, n_buildings, n_osm_key)
        rows.append(row)
        enriched[tau], src[tau] = enr, asrc
        if tau in (EXTRA_TAU, REFERENCE_TAU):
            kept_comps[tau] = comps
        if abs(tau - REFERENCE_TAU) < 1e-9:
            ref_rels = rels
        print(f"  tau={tau:.2f}: {row['edges']:>8,} edges  {row['enriched_buildings']:>7,} bldgs  "
              f"{row['matched_osm']:>7,} osm  primJ={row['primary_jaccard_mean']}  "
              f"n:m={row['comp_n_m']}  ({row['_seconds']}s)")

    # ── validation gate ──────────────────────────────────────────────────────
    ref = next(r for r in rows if abs(r["tau"] - REFERENCE_TAU) < 1e-9)
    mine = {"edges": ref["edges"], "enriched_buildings": ref["enriched_buildings"],
            "matched_osm": ref["matched_osm"], "primary_edges": ref["primary_edges"],
            "match_type_edges": ref["match_type_edges"]}
    diffs = {k: {"graph": baseline[k], "sweep": mine[k]} for k in mine if baseline[k] != mine[k]}
    print("Validation: edge-level comparison with the stored ENRICHED_BY edges ...")
    with ro.session() as sess:
        graph_edges = sess.run(EDGES_Q).data()
    edge_diff, prim_flips = edge_level_diff(ref_rels, graph_edges)
    edge_diff["is_primary_flips_explained"] = explain_primary_flips(all_edges, prim_flips)
    validation = {"tau": REFERENCE_TAU, "passed": not diffs, "graph": baseline,
                  "sweep": mine, "aggregate_differences": diffs, "edge_level": edge_diff}
    print(f"  gate {'PASSED' if not diffs else 'FAILED'}; edge-level: "
          f"{ {k: v for k, v in edge_diff.items() if k != 'examples'} }")

    with ro.session() as sess:
        counts_after = graph_counts(sess)
    driver.close()
    unchanged = counts_before == counts_after
    print(f"  graph counts after:  {counts_after}  (unchanged={unchanged})")

    if diffs or not unchanged:
        diag = Path(args.cache_dir) / f"osm_tau_sweep_{city}_gate_diagnostic.json"
        diag.write_text(json.dumps({"validation": validation, "counts_before": counts_before,
                                    "counts_after": counts_after}, indent=2, default=str),
                        encoding="utf-8")
        print(f"STOP: {'validation gate failed' if diffs else 'graph counts changed'}; "
              f"diagnostic written to {diag}; no outputs written.")
        sys.exit(2 if diffs else 3)

    # ── stability vs the reference + pairwise ────────────────────────────────
    for row in rows:
        s = stability(enriched[REFERENCE_TAU], src[REFERENCE_TAU], enriched[row["tau"]], src[row["tau"]])
        row["vs_ref_gained"], row["vs_ref_lost"] = s["gained"], s["lost"]
        row["vs_ref_both"], row["vs_ref_same_source"] = s["both"], s["same_source"]
        row["vs_ref_same_source_pct"] = s["same_source_pct"]
    pairwise = {f"{a:.2f}->{b:.2f}": stability(enriched[a], src[a], enriched[b], src[b])
                for a, b in PAIRWISE}

    enr_any = {e["pykci"] for e in all_edges}
    ceiling = {"tau0_pairs": len(all_edges), "max_enrichable": len(enr_any),
               "buildings_without_any_osm_overlap": n_buildings - len(enr_any),
               "pct": _pct(n_buildings - len(enr_any), n_buildings),
               "pykci_footprints_fetched": len(pykci_ids)}

    print("Threshold diagnostics ...")
    owner = osm_owner(all_edges)
    diagnostics = {
        "footprint_source": footprints,
        "ratio_distribution": ratio_distribution(all_edges),
        "steps": step_diagnostics(TAU_GRID, enriched, src, owner),
        "extra_below_ref": extra_below_ref(EXTRA_TAU, enriched, src, owner,
                                           kept_comps[EXTRA_TAU], kept_comps[REFERENCE_TAU]),
    }

    rep = {
        "meta": {
            "run_date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "dataset": ds["name"], "city": city, "crs": crs, "pbf": str(pbf).replace("\\", "/"),
            "buffer_m": args.buffer, "source": source, "city_bbox_wgs84": spat,
            "match_bbox": match_bbox, "extent_recomputed": recomputed,
            "tau_grid": TAU_GRID, "main_range": MAIN_RANGE, "reference_tau": REFERENCE_TAU,
            "graph_counts_before": counts_before, "graph_counts_after": counts_after,
            "graph_unchanged": unchanged, "env": env, "cache_file": cpath,
            "timings_s": {**timings, "total_s": round(time.perf_counter() - t_start, 1),
                          "per_tau_s": {f"{r['tau']:.2f}": r["_seconds"] for r in rows}},
            "osm": osm_stats,
            "definitions": {
                "enriched_buildings": "distinct pykci Buildings with >=1 ENRICHED_BY edge at tau",
                "matched_osm": "distinct OSM features (Building key) with >=1 edge at tau; "
                               "% of building_key_features",
                "primary_jaccard": "Jaccard of the is_primary edges from build_enrich_writes "
                                   "(the paper's precision proxy is their mean)",
                "attribute_source": "per building, the OSM feature sharing the largest "
                                    "intersection area (build_enrich_writes copies osm_* from it)",
                "largest_n_m": "n:m component with the most nodes (buildings + OSM features)",
                "ceiling": "buildings with no positive-area overlap with any OSM feature under "
                           "the Building key (tau-independent)",
                "neighbour_source": "a building's attribute-source OSM feature shares a larger "
                                    "intersection with ANOTHER building (it is that neighbour's "
                                    "outline); 'own' otherwise. A heuristic, most reliable for thin "
                                    "overlaps at low ratios",
                "footprint_source": "Buildings with a GroundSurface boundary; the others are matched "
                                    "with their axis-aligned bbox rectangle (fetch_pykci_footprints fallback)",
            },
        },
        "validation": validation,
        "sweep": [{k: v for k, v in r.items() if k != "_seconds"} for r in rows],
        "pairwise_stability": pairwise,
        "ceiling": ceiling,
        "diagnostics": diagnostics,
    }
    rep["findings"] = findings(rep)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"osm_tau_sweep_{city}"
    Path(f"{base}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=list),
                                    encoding="utf-8")
    write_csv(f"{base}.csv", rows)
    Path(f"{base}.md").write_text(markdown(rep), encoding="utf-8")
    plotted = try_plot(f"{base}.png", rows)
    print(f"Wrote {base}.json / .csv / .md" + (" / .png" if plotted else " (no PNG: matplotlib unavailable)"))
    print("\n".join(rep["findings"]))


if __name__ == "__main__":
    main()
