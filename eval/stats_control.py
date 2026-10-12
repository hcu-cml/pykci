#!/usr/bin/env python3
"""Per-city statistical + anomaly control for pykci, run BEFORE and AFTER ingestion.

Motivation
----------
Some source datasets contain anomalies that silently corrupt the graph. The
canonical example is swissBUILDINGS3D (Zurich), which stamps the placeholder
``gml:id="ID_"`` on many buildings; because ``ingest_citygml.py`` does
``MERGE (b:Building {id: row.id})`` with no uniqueness safeguard, every such
building collapses into ONE node, hiding hundreds of real buildings inside a
single super-node and distorting every per-building statistic. This tool exists
to catch that class of problem, before it reaches Neo4j and again after.

Two phases, one document per city
---------------------------------
* ``precheck``  - stream the INPUT files (CityGML, its roofmats variant, its
  LoD3 variant, the OSM ``.osm.pbf``) with no database, compute a comprehensive
  statistical profile, and flag anomalies (duplicate/placeholder/missing gml:id,
  degenerate or missing geometry, non-finite / out-of-CRS coordinates, mixed
  srsName, height/vertex/spatial outliers, roofmat + LoD3 coverage, OSM layer
  counts). Writes ``eval/results/stats_control_precheck_<city>_<ts>.json``.

* ``postcheck`` - profile the RESULTING Neo4j graph for that dataset: full label
  and relationship inventory, nodes-per-building distribution (the paper stat),
  gml:id collapse detection (fan-out outliers), orphan / missing-source /
  not-indexed nodes, OSM kind + orphan-rate + huge-relation stats, roofmat and
  LoD3 coverage. Writes ``eval/results/stats_control_postcheck_<city>_<ts>.json``.

* ``report``    - fuse the latest precheck + postcheck for a city into one
  human-readable control document (``output/stats/stats_control_<city>.md``),
  ranked by severity, with a "Decisions needed" section and a paper-ready
  summary-statistics block. Cross-checks input vs graph (e.g. building loss =
  distinct source gml:ids minus Building nodes = collapse magnitude).

Both the statistics and the anomaly list double as reproducible reporting
material for the paper (analogous to eval/osm_ingestion_census.py).

Usage
-----
    py eval/stats_control.py precheck  --city zurich \
        --citygml input/citygml/zuerich/tiles [--roofmats F] [--lod3 F] [--osm input/osm/switzerland-260704.osm.pbf]
    py eval/stats_control.py postcheck --city zurich [--dataset zurich] [--deep]
    py eval/stats_control.py report    --city zurich
    py eval/stats_control.py --render eval/results/stats_control_postcheck_zurich_<ts>.json

Design notes
------------
* CityGML parsing is version-agnostic (local-name ``{*}Building`` streaming via
  lxml.iterparse, matching tools/extract_citygml_building_footprints.py) so 1.0 /
  2.0 / 3.0 all work; memory is bounded by clearing each building after use.
* OSM precheck uses ``ogrinfo`` for layer feature counts (no bbox needed pre-
  ingest); the authoritative lossless OSM gate remains
  eval/osm_ingestion_census.py, which this tool references rather than duplicates.
* Graph checks are split into fast (indexed counts + distributions, default) and
  deep (subtree traversal, orphan anti-join, R-tree membership; ``--deep``) so it
  stays usable on whole-city graphs with millions of OSM nodes.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from lxml import etree
except ImportError:
    etree = None

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "eval" / "results"
STATS_DIR = ROOT / "output" / "stats"

NEO4J_URL = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")

# Severity ordering (high number = more severe) for sorting the anomaly list.
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

# gml:id values that are structurally placeholders (collapse risk on MERGE).
# Matches empty, "ID"/"ID_", or a bare non-hex prefix with no distinguishing suffix.
PLACEHOLDER_ID_RE = re.compile(r"^(id_?|gml_?|building_?|feature_?|_+)$", re.IGNORECASE)

# OSM layers ogrinfo reports for a .osm.pbf (mirrors ingest_osm.py's target triple
# plus the relation layers, for completeness in the scope table).
OSM_LAYERS = ["points", "lines", "multipolygons", "multilinestrings", "other_relations"]

# measuredHeight values at or below this are treated as missing-value sentinels
# (e.g. PLATEAU stamps -9999): kept verbatim in the data, excluded from every
# height statistic, and counted separately.
HEIGHT_SENTINEL = -999.0


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _city_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "dataset"


# --------------------------------------------------------------------------- #
# CRS resolution (compact copy of ingest_citygml._epsg_from_srs, kept in sync)
# --------------------------------------------------------------------------- #

def resolve_epsg(srs: str | None) -> str | None:
    """srsName string -> 'EPSG:NNNN' (best effort), else the input, else None."""
    if not srs:
        return None
    s = srs.upper().replace(" ", "")
    m = re.search(r"EPSG[:/]+(?:\d+/)?(\d{4,6})", s)
    if m:
        return f"EPSG:{m.group(1)}"
    m = re.search(r"UTM(\d{2})", s)
    if m:
        return f"EPSG:258{m.group(1)}"
    if "CH1903+" in s or "LV95" in s:
        return "EPSG:2056"
    if "CH1903" in s or "LV03" in s:
        return "EPSG:21781"
    return srs


# Rough geographic-CRS test so we can range-check coordinates. Geographic EPSG
# codes seen in this project: 4326 (WGS84), 6697 (JGD2011, Tokyo).
GEOGRAPHIC_EPSG = {"EPSG:4326", "EPSG:4979", "EPSG:6697", "EPSG:4258"}


# --------------------------------------------------------------------------- #
# Small statistics helpers
# --------------------------------------------------------------------------- #

def _dist(values: list[float]) -> dict:
    """min/max/mean/median/p90/p99/stdev over a numeric list (None-safe)."""
    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not xs:
        return {"n": 0}
    xs_sorted = sorted(xs)

    def pct(p: float) -> float:
        if len(xs_sorted) == 1:
            return xs_sorted[0]
        k = (len(xs_sorted) - 1) * p
        lo = math.floor(k)
        hi = math.ceil(k)
        if lo == hi:
            return xs_sorted[int(k)]
        return xs_sorted[lo] * (hi - k) + xs_sorted[hi] * (k - lo)

    return {
        "n": len(xs),
        "min": min(xs),
        "max": max(xs),
        "mean": round(statistics.fmean(xs), 4),
        "median": round(statistics.median(xs), 4),
        "p90": round(pct(0.90), 4),
        "p99": round(pct(0.99), 4),
        "stdev": round(statistics.pstdev(xs), 4) if len(xs) > 1 else 0.0,
    }


def _mad(values: list[float]) -> tuple[float, float]:
    """Return (median, median-absolute-deviation) for robust outlier detection."""
    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not xs:
        return 0.0, 0.0
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs]) or 0.0
    return med, mad


def _anomaly(code, severity, scope, count, message, examples=None,
             recommendation="", needs_decision=False) -> dict:
    return {
        "code": code,
        "severity": severity,
        "scope": scope,          # "input" | "graph" | "cross"
        "count": count,
        "message": message,
        "examples": (examples or [])[:15],
        "recommendation": recommendation,
        "needs_decision": needs_decision,
    }


# --------------------------------------------------------------------------- #
# CityGML precheck (streaming, version-agnostic)
# --------------------------------------------------------------------------- #

def _iter_gml_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out.extend(sorted(pp.rglob("*.gml")))
        elif pp.is_file():
            out.append(pp)
        else:
            out.extend(Path(x) for x in sorted(glob.glob(p)))
    return out


def _sniff_srs(path: Path) -> str | None:
    """Grab the first srsName from a file's header cheaply (no full parse)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(20000)
    except OSError:
        return None
    m = re.search(r'srsName="([^"]+)"', head)
    return m.group(1) if m else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _building_coords(b_el):
    """Yield (n_verts, n_rings, min_ring_len, unclosed, nonfinite, bbox3d) for a
    building subtree. bbox3d = [xmin,ymin,zmin,xmax,ymax,zmax] or None."""
    n_verts = 0
    n_rings = 0
    min_ring_len = None
    unclosed = 0
    nonfinite = False
    xs_min = ys_min = zs_min = math.inf
    xs_max = ys_max = zs_max = -math.inf

    for ring in b_el.iter("{*}LinearRing"):
        n_rings += 1
        # posList (preferred) or a sequence of pos children.
        coords: list[float] = []
        dim = 3
        pl = ring.find("{*}posList")
        if pl is not None and pl.text:
            sd = pl.get("srsDimension")
            if sd and sd.isdigit():
                dim = int(sd)
            coords = _floats(pl.text)
        else:
            for pos in ring.iter("{*}pos"):
                if pos.text:
                    coords.extend(_floats(pos.text))
        if not coords:
            if min_ring_len is None or 0 < (min_ring_len or 0):
                min_ring_len = 0
            continue
        if dim < 2:
            dim = 3
        ntup = len(coords) // dim
        if min_ring_len is None or ntup < min_ring_len:
            min_ring_len = ntup
        n_verts += ntup
        # closure test (first tuple == last tuple)
        if ntup >= 2:
            first = coords[:dim]
            last = coords[(ntup - 1) * dim: ntup * dim]
            if any(abs(a - b) > 1e-6 for a, b in zip(first, last)):
                unclosed += 1
        for i in range(0, ntup * dim, dim):
            x = coords[i]
            y = coords[i + 1] if dim > 1 else 0.0
            z = coords[i + 2] if dim > 2 else 0.0
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                nonfinite = True
                continue
            xs_min = min(xs_min, x); xs_max = max(xs_max, x)
            ys_min = min(ys_min, y); ys_max = max(ys_max, y)
            zs_min = min(zs_min, z); zs_max = max(zs_max, z)

    bbox3d = None
    if xs_min != math.inf:
        bbox3d = [xs_min, ys_min, zs_min, xs_max, ys_max, zs_max]
    return n_verts, n_rings, (min_ring_len or 0), unclosed, nonfinite, bbox3d


def _floats(text: str) -> list[float]:
    out = []
    for tok in text.split():
        try:
            out.append(float(tok))
        except ValueError:
            out.append(float("nan"))
    return out


def _first_text(b_el, localname: str) -> str | None:
    for el in b_el.iter():
        if _local(el.tag) == localname and el.text and el.text.strip():
            return el.text.strip()
    return None


def _gen_attr_scan(b_el):
    """Return (n_gen_attrs, roofmat_value_or_None). roofmat = value of the gen
    attribute named PredictedRoofMaterial (any case), matching _attr_key()."""
    n = 0
    roofmat = None
    for el in b_el.iter():
        ln = _local(el.tag)
        if ln.endswith("Attribute") and el.get("name") is not None:
            n += 1
            if el.get("name", "").lower().replace(" ", "") == "predictedroofmaterial":
                for c in el:
                    if _local(c.tag) == "value" and c.text:
                        roofmat = c.text.strip()
    return n, roofmat


def scan_citygml(paths: list[str], progress=True) -> dict:
    """Stream every {*}Building, collect per-building records + aggregate stats."""
    if etree is None:
        raise SystemExit("lxml is required for the CityGML precheck (pip install lxml).")

    files = _iter_gml_files(paths)
    if not files:
        raise SystemExit(f"No .gml files found under: {paths}")

    per_file = {}
    srs_seen = {}
    ids: dict[str, list[str]] = {}          # gml:id -> [source files]
    heights: list[float] = []
    height_sentinels = 0
    vert_counts: list[float] = []
    centroids_x: list[float] = []
    centroids_y: list[float] = []
    functions: dict[str, int] = {}
    roof_types: dict[str, int] = {}
    roofmat_hist: dict[str, int] = {}
    n_missing_id = 0
    n_no_geom = 0
    n_degenerate = 0
    n_nonfinite = 0
    n_unclosed = 0
    n_with_roofmat = 0
    total = 0
    examples = {"missing_id": [], "no_geom": [], "degenerate": [],
                "nonfinite": [], "unclosed": [], "placeholder": []}

    for fi, path in enumerate(files):
        srs = _sniff_srs(path)
        if srs:
            srs_seen.setdefault(srs, 0)
            srs_seen[srs] += 1
        fcount = 0
        try:
            ctx = etree.iterparse(str(path), events=("end",), tag="{*}Building",
                                  huge_tree=True, recover=True)
        except OSError as e:
            per_file[path.name] = {"error": str(e)}
            continue
        for _, b in ctx:
            total += 1
            fcount += 1
            bid = None
            for k, v in b.attrib.items():
                if _local(k) == "id":
                    bid = v
                    break
            src = path.name
            if not bid:
                n_missing_id += 1
                if len(examples["missing_id"]) < 15:
                    examples["missing_id"].append(f"{src}#<no-id>")
            else:
                ids.setdefault(bid, []).append(src)
                if PLACEHOLDER_ID_RE.match(bid.strip()) and len(examples["placeholder"]) < 15:
                    examples["placeholder"].append(f"{src}:{bid}")

            nv, nr, minring, unclosed, nonfinite, bbox3d = _building_coords(b)
            vert_counts.append(nv)
            if nv == 0 or nr == 0:
                n_no_geom += 1
                if len(examples["no_geom"]) < 15:
                    examples["no_geom"].append(f"{src}:{bid}")
            if minring and minring < 4:
                n_degenerate += 1
                if len(examples["degenerate"]) < 15:
                    examples["degenerate"].append(f"{src}:{bid} (ring={minring} pts)")
            if nonfinite:
                n_nonfinite += 1
                if len(examples["nonfinite"]) < 15:
                    examples["nonfinite"].append(f"{src}:{bid}")
            if unclosed:
                n_unclosed += 1
                if len(examples["unclosed"]) < 15:
                    examples["unclosed"].append(f"{src}:{bid} ({unclosed} ring(s))")
            if bbox3d:
                cx = (bbox3d[0] + bbox3d[3]) / 2.0
                cy = (bbox3d[1] + bbox3d[4]) / 2.0
                centroids_x.append(cx)
                centroids_y.append(cy)
                h = bbox3d[5] - bbox3d[2]
            h_attr = _first_text(b, "measuredHeight")
            if h_attr:
                try:
                    hv = float(h_attr)
                    # Missing-value sentinels (e.g. PLATEAU -9999) are kept
                    # verbatim in the data but excluded from statistics.
                    if hv <= HEIGHT_SENTINEL:
                        height_sentinels += 1
                    else:
                        heights.append(hv)
                except ValueError:
                    pass
            fn = _first_text(b, "function")
            if fn:
                functions[fn] = functions.get(fn, 0) + 1
            rt = _first_text(b, "roofType")
            if rt:
                roof_types[rt] = roof_types.get(rt, 0) + 1
            _, roofmat = _gen_attr_scan(b)
            if roofmat:
                n_with_roofmat += 1
                roofmat_hist[roofmat] = roofmat_hist.get(roofmat, 0) + 1

            # bound memory: drop the building and its already-parsed siblings
            b.clear()
            while b.getprevious() is not None:
                del b.getparent()[0]
        per_file[path.name] = {"buildings": fcount, "srs": srs}
        if progress:
            print(f"  [citygml] {path.name}: {fcount} buildings", flush=True)

    # ---- duplicate / placeholder id analysis ---------------------------- #
    dup_ids = {k: v for k, v in ids.items() if len(v) > 1}
    dup_building_total = sum(len(v) for v in dup_ids.values())
    placeholder_ids = {k: v for k, v in ids.items() if PLACEHOLDER_ID_RE.match(k.strip())}
    distinct_ids = len(ids)

    # spatial outliers via robust MAD on centroids
    medx, madx = _mad(centroids_x)
    medy, mady = _mad(centroids_y)
    spatial_outliers = 0
    if madx > 0 and mady > 0:
        for cx, cy in zip(centroids_x, centroids_y):
            if abs(cx - medx) > 12 * madx or abs(cy - medy) > 12 * mady:
                spatial_outliers += 1

    # vertex-count outliers (tessellation bombs / merged multipart)
    vdist = _dist(vert_counts)
    vthresh = max(vdist.get("p99", 0) * 5, 20000) if vdist.get("n") else 0
    vert_outliers = sum(1 for v in vert_counts if v and v > vthresh)

    # coordinate-in-CRS-range sanity (single dominant srs)
    dom_srs = max(srs_seen, key=srs_seen.get) if srs_seen else None
    dom_epsg = resolve_epsg(dom_srs)
    crs_range_issue = None
    if dom_epsg and centroids_x:
        geographic = dom_epsg in GEOGRAPHIC_EPSG
        sample_x = statistics.median(centroids_x)
        sample_y = statistics.median(centroids_y)
        looks_geographic = abs(sample_x) <= 180 and abs(sample_y) <= 90
        if geographic and not looks_geographic:
            crs_range_issue = f"CRS {dom_epsg} is geographic but coords look projected (median {sample_x:.1f}, {sample_y:.1f})"
        elif (not geographic) and looks_geographic:
            crs_range_issue = f"CRS {dom_epsg} is projected but coords look like lon/lat (median {sample_x:.4f}, {sample_y:.4f})"

    return {
        "files": len(files),
        "buildings_total": total,
        "distinct_gml_ids": distinct_ids,
        "per_file": per_file,
        "srs_seen": srs_seen,
        "dominant_srs": dom_srs,
        "dominant_epsg": dom_epsg,
        "duplicate_ids": {
            "colliding_id_count": len(dup_ids),
            "buildings_involved": dup_building_total,
            "top": sorted(((k, len(v)) for k, v in dup_ids.items()),
                          key=lambda kv: kv[1], reverse=True)[:20],
        },
        "placeholder_ids": {
            "id_count": len(placeholder_ids),
            "buildings_involved": sum(len(v) for v in placeholder_ids.values()),
            "values": sorted(placeholder_ids.keys())[:20],
        },
        "missing_id": n_missing_id,
        "no_geometry": n_no_geom,
        "degenerate_rings": n_degenerate,
        "nonfinite_coords": n_nonfinite,
        "unclosed_rings": n_unclosed,
        "height": _dist(heights),
        "height_sentinels": height_sentinels,
        "vertex_count": vdist,
        "vertex_outliers": {"threshold": vthresh, "count": vert_outliers},
        "spatial_outliers": spatial_outliers,
        "crs_range_issue": crs_range_issue,
        "functions_top": sorted(functions.items(), key=lambda kv: kv[1], reverse=True)[:15],
        "roof_types_top": sorted(roof_types.items(), key=lambda kv: kv[1], reverse=True)[:15],
        "roofmat": {
            "buildings_with_prediction": n_with_roofmat,
            "coverage": round(n_with_roofmat / total, 4) if total else 0.0,
            "class_histogram": dict(sorted(roofmat_hist.items(), key=lambda kv: kv[1], reverse=True)),
        },
        "examples": examples,
    }


def citygml_anomalies(cg: dict) -> list[dict]:
    out = []
    dup = cg["duplicate_ids"]
    if dup["colliding_id_count"]:
        out.append(_anomaly(
            "DUPLICATE_GML_ID", "critical", "input",
            dup["buildings_involved"],
            f"{dup['colliding_id_count']} gml:id value(s) are shared by "
            f"{dup['buildings_involved']} buildings. MERGE (b:Building {{id}}) "
            f"collapses each group into ONE node, losing "
            f"{dup['buildings_involved'] - dup['colliding_id_count']} buildings.",
            examples=[f"{k} x{n}" for k, n in dup["top"][:10]],
            recommendation="Synthesize a unique id at ingest for these sources "
                           "(e.g. source-file + gml:id + ordinal, or a geometry hash).",
            needs_decision=True))
    ph = cg["placeholder_ids"]
    if ph["id_count"]:
        out.append(_anomaly(
            "PLACEHOLDER_GML_ID", "critical", "input", ph["buildings_involved"],
            f"{ph['id_count']} placeholder gml:id value(s) "
            f"({', '.join(ph['values'][:6])}) on {ph['buildings_involved']} buildings.",
            examples=cg["examples"]["placeholder"],
            recommendation="Same fix as DUPLICATE_GML_ID; placeholder ids are "
                           "the swissBUILDINGS3D failure mode.",
            needs_decision=True))
    if cg["missing_id"]:
        out.append(_anomaly(
            "MISSING_GML_ID", "critical", "input", cg["missing_id"],
            f"{cg['missing_id']} building(s) have no gml:id (ingest skips them).",
            examples=cg["examples"]["missing_id"],
            recommendation="Assign a deterministic fallback id (like the footprint "
                           "tool's uuid fallback) so these buildings are not dropped.",
            needs_decision=True))
    if cg["no_geometry"]:
        out.append(_anomaly(
            "NO_GEOMETRY", "high", "input", cg["no_geometry"],
            f"{cg['no_geometry']} building(s) carry no LinearRing geometry.",
            examples=cg["examples"]["no_geom"],
            recommendation="Confirm whether these are legitimately geometry-less "
                           "(address-only) or a parse/source problem."))
    if cg["nonfinite_coords"]:
        out.append(_anomaly(
            "NONFINITE_COORD", "critical", "input", cg["nonfinite_coords"],
            f"{cg['nonfinite_coords']} building(s) contain NaN/Inf coordinates.",
            examples=cg["examples"]["nonfinite"],
            recommendation="Reject or repair these geometries before ingest.",
            needs_decision=True))
    if cg["degenerate_rings"]:
        out.append(_anomaly(
            "DEGENERATE_RING", "high", "input", cg["degenerate_rings"],
            f"{cg['degenerate_rings']} building(s) have a ring with < 4 points.",
            examples=cg["examples"]["degenerate"],
            recommendation="Degenerate rings triangulate to nothing; review source."))
    if cg["unclosed_rings"]:
        out.append(_anomaly(
            "UNCLOSED_RING", "medium", "input", cg["unclosed_rings"],
            f"{cg['unclosed_rings']} building(s) have a LinearRing whose first and "
            f"last vertex differ.",
            examples=cg["examples"]["unclosed"],
            recommendation="Usually tolerated, but flag for the census; some "
                           "triangulators assume closure."))
    if cg["crs_range_issue"]:
        out.append(_anomaly(
            "CRS_RANGE_MISMATCH", "high", "input", 1, cg["crs_range_issue"],
            recommendation="Verify srsName vs actual coordinates; pass the correct "
                           "--source-crs/--target-crs at ingest.",
            needs_decision=True))
    if len(cg["srs_seen"]) > 1:
        out.append(_anomaly(
            "MIXED_SRS", "medium", "input", len(cg["srs_seen"]),
            f"{len(cg['srs_seen'])} distinct srsName values across the inputs: "
            f"{list(cg['srs_seen'])[:5]}",
            recommendation="Mixed CRS in one dataset misaligns geometry; reproject "
                           "to one metric CRS with --target-crs."))
    vo = cg["vertex_outliers"]
    if vo["count"]:
        out.append(_anomaly(
            "VERTEX_COUNT_OUTLIER", "low", "input", vo["count"],
            f"{vo['count']} building(s) exceed {int(vo['threshold'])} vertices "
            f"(p99={cg['vertex_count'].get('p99')}). Possible merged multipart or "
            f"a collapsed placeholder-id super-building.",
            recommendation="Cross-check against the id-collapse finding."))
    if cg["spatial_outliers"]:
        out.append(_anomaly(
            "SPATIAL_OUTLIER", "medium", "input", cg["spatial_outliers"],
            f"{cg['spatial_outliers']} building(s) sit > 12 MAD from the dataset "
            f"centroid (possible coordinate typo / wrong-tile contamination).",
            recommendation="Inspect; a few far-flung buildings can wreck the bbox "
                           "and every spatial query."))
    h = cg["height"]
    if h.get("n"):
        bad_h = [v for v in [h.get("min"), h.get("max")] if v is not None]
        if (h.get("min", 0) is not None and h["min"] < 0) or (h.get("max", 0) or 0) > 1000:
            out.append(_anomaly(
                "HEIGHT_OUTLIER", "medium", "input", None,
                f"measuredHeight range [{h.get('min')}, {h.get('max')}] m contains "
                f"non-physical values (negative or > 1000 m).",
                recommendation="Clamp/flag; negative or km-scale heights are errors."))
    if cg.get("height_sentinels"):
        out.append(_anomaly(
            "HEIGHT_SENTINEL", "info", "input", cg["height_sentinels"],
            f"{cg['height_sentinels']} building(s) carry a missing-value sentinel "
            f"measuredHeight (<= {HEIGHT_SENTINEL}). Kept verbatim in the data, "
            f"excluded from all height statistics.",
            recommendation="None; intended handling (decision 2026-07-12)."))
    return out


# --------------------------------------------------------------------------- #
# OSM precheck (ogrinfo layer counts; deep verification is osm_ingestion_census)
# --------------------------------------------------------------------------- #

def scan_osm(pbf: str) -> dict:
    import shutil
    p = Path(pbf)
    if not p.exists():
        return {"error": f"not found: {pbf}"}
    ogrinfo = shutil.which("ogrinfo")
    if not ogrinfo:
        return {
            "pbf": p.name,
            "size_bytes": p.stat().st_size,
            "ogrinfo_available": False,
            "note": "ogrinfo not on PATH; skipped OSM layer counts. Install GDAL "
                    "(e.g. QGIS/conda) so ogrinfo is callable, or rely on the "
                    "post-fusion gate eval/osm_ingestion_census.py.",
        }
    counts = {}
    for layer in OSM_LAYERS:
        counts[layer] = _ogrinfo_count(p, layer)
    return {
        "pbf": p.name,
        "size_bytes": p.stat().st_size,
        "ogrinfo_available": True,
        "layer_feature_counts": counts,
        "note": "Lossless per-feature OSM verification is eval/osm_ingestion_census.py "
                "(run it after fusion). This is a pre-ingest sanity count only.",
    }


def _ogrinfo_count(pbf: Path, layer: str) -> int | None:
    try:
        out = subprocess.run(
            ["ogrinfo", "-so", "-al", str(pbf), layer],
            capture_output=True, text=True, timeout=1800,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    m = re.search(r"Feature Count:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def osm_anomalies(osm: dict) -> list[dict]:
    out = []
    if osm.get("error"):
        return out
    if not osm.get("ogrinfo_available", True):
        out.append(_anomaly(
            "OGRINFO_UNAVAILABLE", "info", "input", None,
            "ogrinfo is not on PATH, so OSM layer counts were skipped.",
            recommendation="Add GDAL to PATH (QGIS/conda) for pre-ingest OSM counts; "
                           "post-fusion, eval/osm_ingestion_census.py is authoritative."))
        return out
    counts = osm["layer_feature_counts"]
    if all(v is None for v in counts.values()):
        out.append(_anomaly(
            "OGRINFO_FAILED", "info", "input", None,
            "ogrinfo is on PATH but returned no feature counts for any layer "
            "(likely a GDAL/PROJ environment conflict, not an empty file).",
            recommendation="Fix the GDAL/PROJ PATH (e.g. QGIS's ogrinfo needs its "
                           "own PROJ); OSM completeness is gated post-fusion by "
                           "eval/osm_ingestion_census.py regardless."))
        return out
    if all(v in (0, None) for v in counts.values()):
        out.append(_anomaly(
            "OSM_EMPTY", "high", "input", 0,
            "ogrinfo reported 0 features in every layer of the .osm.pbf.",
            recommendation="Verify the .osm.pbf is the intended extract."))
    elif counts.get("points") in (0, None) and counts.get("multipolygons") in (0, None):
        out.append(_anomaly(
            "OSM_NO_POLYGONS", "medium", "input", None,
            "No multipolygon or point features found; nothing to match against buildings.",
            recommendation="Confirm the extract covers the city bbox."))
    return out


# --------------------------------------------------------------------------- #
# Neo4j postcheck
# --------------------------------------------------------------------------- #

def _driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise SystemExit("neo4j driver required for postcheck (pip install neo4j).")
    d = GraphDatabase.driver(NEO4J_URL, auth=NEO4J_AUTH,
                             notifications_disabled_classifications={"UNRECOGNIZED"})
    d.verify_connectivity()
    return d


def _one(sess, q, **kw):
    r = sess.run(q, **kw).single()
    return dict(r) if r else {}


def scan_graph(dataset: str | None, source_like: str | None,
               deep: bool, orphans: bool = False) -> dict:
    d = _driver()
    rep = {"dataset": dataset, "deep": deep, "orphans_scanned": orphans}
    with d.session(database="neo4j") as s:
        # dataset resolution
        ds_rows = [dict(r) for r in s.run(
            "MATCH (ds:Dataset) RETURN ds.name AS name, ds.crs AS crs, "
            "ds.bbox_min_x AS minx, ds.bbox_min_y AS miny, "
            "ds.bbox_max_x AS maxx, ds.bbox_max_y AS maxy ORDER BY name")]
        rep["datasets"] = ds_rows
        if dataset is None and len(ds_rows) == 1:
            dataset = ds_rows[0]["name"]
            rep["dataset"] = dataset

        bfilter = ""
        if source_like:
            bfilter = " WHERE b.source CONTAINS $sl"

        # ---- label inventory ---------------------------------------------- #
        labels = [r["label"] for r in s.run("CALL db.labels() YIELD label RETURN label ORDER BY label")]
        label_counts = {}
        for lbl in labels:
            label_counts[lbl] = _one(s, f"MATCH (n:`{lbl}`) RETURN count(n) AS c")["c"]
        rep["label_counts"] = label_counts

        rel_types = [r["t"] for r in s.run("CALL db.relationshipTypes() YIELD relationshipType AS t RETURN t ORDER BY t")]
        rel_counts = {}
        for t in rel_types:
            rel_counts[t] = _one(s, f"MATCH ()-[r:`{t}`]->() RETURN count(r) AS c")["c"]
        rep["relationship_counts"] = rel_counts
        rep["totals"] = {
            "nodes": _one(s, "MATCH (n) RETURN count(n) AS c")["c"],
            "relationships": _one(s, "MATCH ()-[r]->() RETURN count(r) AS c")["c"],
        }

        # ---- building fan-out (id-collapse detector, fast) ---------------- #
        fanout = [dict(r) for r in s.run(
            f"MATCH (b:Building){bfilter} "
            "OPTIONAL MATCH (b)-[:HAS_BOUNDARY]->(bs) "
            "WITH b, count(bs) AS nb "
            "OPTIONAL MATCH (b)-[:HAS_BUILDING_PART]->(bp) "
            "RETURN b.id AS id, b.source AS source, nb AS boundaries, count(bp) AS parts",
            sl=source_like)]
        rep["id_synthesized_count"] = _one(
            s, "MATCH (b:Building) WHERE b.id_synthesized = true RETURN count(b) AS c")["c"]
        b_boundaries = [r["boundaries"] for r in fanout]
        b_parts = [r["parts"] for r in fanout]
        rep["building_count"] = len(fanout)
        rep["boundaries_per_building"] = _dist(b_boundaries)
        rep["parts_per_building"] = _dist(b_parts)

        bdist = rep["boundaries_per_building"]
        med_b = bdist.get("median", 0) or 0
        # collapse-suspect: extreme boundary fan-out OR many building parts
        b_thresh = max((med_b or 1) * 50, bdist.get("p99", 0) * 8, 200)
        collapse = [r for r in fanout
                    if (r["boundaries"] or 0) > b_thresh or (r["parts"] or 0) > 20]
        collapse.sort(key=lambda r: (r["parts"] or 0, r["boundaries"] or 0), reverse=True)
        rep["collapse_suspects"] = {
            "boundary_threshold": b_thresh,
            "count": len(collapse),
            "top": [{"id": r["id"], "source": r["source"],
                     "boundaries": r["boundaries"], "parts": r["parts"]}
                    for r in collapse[:20]],
        }

        # per-source distinct-id vs node-count (collapse magnitude, fast)
        src_rows = [dict(r) for r in s.run(
            "MATCH (b:Building) RETURN b.source AS source, count(b) AS nodes "
            "ORDER BY nodes DESC")]
        rep["buildings_per_source_top"] = src_rows[:15]

        # ---- OSM stats ---------------------------------------------------- #
        osm_total = _one(s, "MATCH (o:OsmFeature) RETURN count(o) AS c")["c"]
        rep["osm"] = {"osm_feature_nodes": osm_total}
        if osm_total:
            rep["osm"]["by_kind"] = {r["k"]: r["c"] for r in s.run(
                "MATCH (o:OsmFeature) RETURN o.osm_kind AS k, count(*) AS c ORDER BY c DESC")}
            split = _one(s, "MATCH (o:OsmFeature) "
                            "RETURN sum(CASE WHEN o.matched THEN 1 ELSE 0 END) AS matched, "
                            "sum(CASE WHEN o.matched THEN 0 ELSE 1 END) AS orphan")
            rep["osm"]["matched"] = split.get("matched", 0)
            rep["osm"]["orphan"] = split.get("orphan", 0)
            rep["osm"]["orphan_rate"] = round(
                (split.get("orphan", 0) or 0) / osm_total, 4) if osm_total else 0.0
            rep["osm"]["orphan_reasons"] = {r["r"]: r["c"] for r in s.run(
                "MATCH (o:OsmFeature) WHERE o.matched = false "
                "RETURN o.unmatched_reason AS r, count(*) AS c ORDER BY c DESC")}
            rep["osm"]["enriched_buildings"] = _one(
                s, "MATCH (b:Building)-[:ENRICHED_BY]->() RETURN count(DISTINCT b) AS c")["c"]
            rep["osm"]["pois"] = _one(s, "MATCH ()-[r:HAS_POI]->() RETURN count(r) AS c")["c"]
            # A node and a way can legitimately share a numeric osm_id, so a real
            # duplicate is a repeat of the same (osm_type, osm_id) pair.
            rep["osm"]["dup_osm_id"] = _one(
                s, "MATCH (o:OsmFeature) WITH o.osm_type AS t, o.osm_id AS oid, count(*) AS c "
                   "WHERE oid IS NOT NULL AND c > 1 RETURN count(*) AS c")["c"]

        # ---- roofmat + lod3 coverage ------------------------------------- #
        rm = _one(s, "MATCH (b:Building) "
                     "RETURN count(b) AS total, count(b.predictedroofmaterial) AS pred")
        rep["roofmat"] = {
            "total_buildings": rm.get("total", 0),
            "with_prediction": rm.get("pred", 0),
            "coverage": round((rm.get("pred", 0) or 0) / rm["total"], 4) if rm.get("total") else 0.0,
        }
        if rm.get("pred"):
            rep["roofmat"]["class_histogram"] = {r["m"]: r["c"] for r in s.run(
                "MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL "
                "RETURN b.predictedroofmaterial AS m, count(*) AS c ORDER BY c DESC")}
        rep["lod3"] = {
            "enriched_buildings": _one(
                s, "MATCH (b:Building) WHERE b.lod3_enriched = true RETURN count(b) AS c")["c"],
            "facade_edges": _one(
                s, "MATCH ()-[r:HAS_LOD3_FACADE]->() RETURN count(r) AS c")["c"],
        }

        # ---- height distribution ----------------------------------------- #
        heights = [r["h"] for r in s.run(
            "MATCH (b:Building) WHERE b.measured_height IS NOT NULL "
            "AND toFloat(b.measured_height) > $sent "
            "RETURN toFloat(b.measured_height) AS h", sent=HEIGHT_SENTINEL)]
        rep["measured_height"] = _dist(heights)
        rep["measured_height_sentinels"] = _one(
            s, "MATCH (b:Building) WHERE b.measured_height IS NOT NULL "
               "AND toFloat(b.measured_height) <= $sent RETURN count(b) AS c",
            sent=HEIGHT_SENTINEL)["c"]

        # ---- missing-source (fast; Building/OsmFeature scoped) ----------- #
        rep["missing_source"] = {
            "buildings": _one(s, "MATCH (b:Building) WHERE b.source IS NULL RETURN count(b) AS c")["c"],
            "osm": _one(s, "MATCH (o:OsmFeature) WHERE o.source IS NULL RETURN count(o) AS c")["c"] if osm_total else 0,
        }

        # ---- deep (optional): bounded by building count, still cheap ------ #
        if deep:
            deep_stats = _deep_nodes_per_building(s, source_like)
            rep["nodes_per_building"] = deep_stats.get("nodes", deep_stats)
            rep["geoms_per_building"] = deep_stats.get("geoms")
            rep["attrs_per_building"] = _deep_attrs_per_building(s, source_like)
            rep["buildings_no_geometry"] = _one(
                s, "MATCH (b:Building) WHERE NOT (b)-[:HAS_LOD_SOLID]->() "
                   "AND NOT (b)-[:HAS_BOUNDARY]->() RETURN count(b) AS c")["c"]
        # ---- orphan anti-join: full-graph scan, opt-in (can be minutes on
        #      multi-million-node graphs) ----------------------------------- #
        if orphans:
            rep["orphan_nodes"] = _one(
                s, "MATCH (n) WHERE NOT (n)--() AND NOT n:Dataset RETURN count(n) AS c")["c"]
    d.close()
    return rep


def _deep_nodes_per_building(s, source_like) -> dict:
    """Full subtree node count per building via APOC (min/max/avg/percentiles),
    plus the geometry-polygon count inside the same traversal (one pass).
    Blacklists upward and OSM nodes so we count each building's own CityGML tree."""
    where = " WHERE b.source CONTAINS $sl" if source_like else ""
    try:
        rec = _one(s, f"""
            MATCH (b:Building){where}
            CALL apoc.path.subgraphNodes(b, {{relationshipFilter:'>',
              labelFilter:'-Dataset|-District|-City|-OsmFeature', maxLevel:-1}})
            YIELD node
            WITH b, count(node) AS n,
                 sum(CASE WHEN node:GeometryPolygon THEN 1 ELSE 0 END) AS g
            RETURN min(n) AS n_min, max(n) AS n_max, avg(n) AS n_avg,
                   percentileCont(n,0.5) AS n_p50, percentileCont(n,0.9) AS n_p90,
                   percentileCont(n,0.99) AS n_p99,
                   min(g) AS g_min, max(g) AS g_max, avg(g) AS g_avg,
                   percentileCont(g,0.5) AS g_p50, percentileCont(g,0.9) AS g_p90,
                   percentileCont(g,0.99) AS g_p99, count(b) AS buildings
            """, sl=source_like)
        return {
            "nodes": {"min": rec["n_min"], "max": rec["n_max"], "avg": rec["n_avg"],
                      "p50": rec["n_p50"], "p90": rec["n_p90"], "p99": rec["n_p99"],
                      "buildings": rec["buildings"]},
            "geoms": {"min": rec["g_min"], "max": rec["g_max"], "avg": rec["g_avg"],
                      "p50": rec["g_p50"], "p90": rec["g_p90"], "p99": rec["g_p99"],
                      "buildings": rec["buildings"],
                      "unit": "GeometryPolygon nodes in the building's CityGML subtree"},
        }
    except Exception as e:  # APOC missing or timeout
        return {"error": f"deep subtree scan unavailable: {e}"}


# Building-node property keys that are NOT thematic content: identity/bookkeeping,
# geometry anchors, spatial-plugin bookkeeping, and non-authoritative layers
# (osm_*/addr_* enrichment, lod3_* sentinels, ML prediction). What remains counts
# as a semantic/thematic attribute of the authoritative CityGML feature.
_NON_THEMATIC_EXACT = [
    "id", "name", "source", "source_gml_id", "id_synthesized",
    "bbox_wkt", "location", "gtype", "bbox", "center_x", "center_y",
    "matched", "community", "_gen_attrs_map", "gen_map",
    "predictedroofmaterial", "embedding", "embedding_ollama",
]
_NON_THEMATIC_PREFIXES = ["bbox_", "osm_", "addr_", "lod3_"]


def _deep_attrs_per_building(s, source_like) -> dict:
    """Thematic-attribute count per building: node property keys minus the
    documented non-thematic exclusion list (full distribution)."""
    where = " WHERE b.source CONTAINS $sl" if source_like else ""
    try:
        rec = _one(s, f"""
            MATCH (b:Building){where}
            WITH size([k IN keys(b) WHERE NOT k IN $excl
                       AND none(p IN $pref WHERE k STARTS WITH p)]) AS a
            RETURN min(a) AS min, max(a) AS max, avg(a) AS avg,
                   percentileCont(a,0.5) AS p50, percentileCont(a,0.9) AS p90,
                   percentileCont(a,0.99) AS p99, count(*) AS buildings
            """, sl=source_like, excl=_NON_THEMATIC_EXACT,
            pref=_NON_THEMATIC_PREFIXES)
        rec = dict(rec)
        rec["unit"] = ("thematic property keys on the Building node "
                       "(identity/bbox/spatial-plugin/OSM/LoD3/ML keys excluded)")
        return rec
    except Exception as e:
        return {"error": f"attrs-per-building scan unavailable: {e}"}


def graph_anomalies(g: dict) -> list[dict]:
    out = []
    cs = g.get("collapse_suspects", {})
    if cs.get("count"):
        out.append(_anomaly(
            "ID_COLLAPSE", "critical", "graph", cs["count"],
            f"{cs['count']} Building node(s) have anomalous fan-out (boundaries > "
            f"{int(cs['boundary_threshold'])} or > 20 building parts), the signature "
            f"of many source buildings collapsed onto one gml:id.",
            examples=[f"{r['id']} (src={r['source']}, {r['boundaries']} boundaries, "
                      f"{r['parts']} parts)" for r in cs["top"][:10]],
            recommendation="Re-ingest the affected source with unique-id synthesis; "
                           "these super-nodes distort every per-building statistic.",
            needs_decision=True))
    ms = g.get("missing_source", {})
    if (ms.get("buildings") or 0) + (ms.get("osm") or 0) > 0:
        out.append(_anomaly(
            "MISSING_SOURCE", "medium", "graph",
            (ms.get("buildings") or 0) + (ms.get("osm") or 0),
            f"{ms.get('buildings',0)} Building and {ms.get('osm',0)} OsmFeature "
            f"node(s) lack a `source` (breaks provenance).",
            recommendation="Every node must carry `source` (provenance principle)."))
    osm = g.get("osm", {})
    if osm.get("dup_osm_id"):
        out.append(_anomaly(
            "OSM_DUPLICATE_ID", "high", "graph", osm["dup_osm_id"],
            f"{osm['dup_osm_id']} osm_id value(s) appear on more than one OsmFeature.",
            recommendation="Check the id scheme in build_osm_props; ways/relations "
                           "sharing a raw id should be namespaced."))
    if osm.get("orphan_rate", 0) and osm["orphan_rate"] > 0.9:
        out.append(_anomaly(
            "OSM_HIGH_ORPHAN_RATE", "low", "graph", osm.get("orphan"),
            f"{osm['orphan_rate']*100:.1f}% of OSM features are unmatched orphans "
            f"(expected for city-wide extracts: roads, natural areas, POIs outside "
            f"building footprints). Informational.",
            recommendation="Expected; confirm buffer/threshold if it looks too high."))
    if g.get("orphans_scanned") and g.get("orphan_nodes"):
        out.append(_anomaly(
            "ORPHAN_NODES", "high", "graph", g["orphan_nodes"],
            f"{g['orphan_nodes']} node(s) have no relationships at all.",
            recommendation="Dangling nodes indicate an interrupted or buggy ingest."))
    if g.get("deep") and g.get("buildings_no_geometry"):
        out.append(_anomaly(
            "BUILDING_NO_GEOMETRY", "high", "graph", g["buildings_no_geometry"],
            f"{g['buildings_no_geometry']} Building node(s) have neither a solid "
            f"nor any boundary surface.",
            recommendation="Confirm these are address-only or fix the parse."))
    return out


def cross_checks(pre: dict | None, post: dict | None) -> list[dict]:
    out = []
    if not pre or not post:
        return out
    cg = pre.get("citygml")
    if cg and post.get("building_count") is not None:
        total = cg.get("buildings_total", 0)
        missing = cg.get("missing_id", 0)
        distinct = cg.get("distinct_gml_ids", 0)
        actual = post["building_count"]
        with_id = total - missing           # real buildings that carry an id
        collapsed = with_id - distinct      # buildings merged onto a shared id

        synth = post.get("id_synthesized_count", 0) or 0

        # (1) collapse magnitude: real buildings the id-collisions would hide.
        #     With the tiered id resolution at ingest (content/geometry-hash dedup,
        #     collision-only split with id_synthesized=true), collisions are
        #     RESOLVED, not lost: splits show up as synthesized ids, exact
        #     duplicates dedup on purpose. Only flag loss when collisions exist
        #     and the graph shows no evidence of resolution.
        if collapsed > 0 and synth == 0 and actual <= distinct:
            out.append(_anomaly(
                "BUILDING_LOSS", "critical", "cross", collapsed,
                f"Source has {total} building elements ({with_id} with a gml:id) but "
                f"only {distinct} distinct gml:ids, and the graph shows no synthesized "
                f"ids: up to {collapsed} real buildings are lost to id-collapse on MERGE"
                + (f" (+{missing} more dropped for having no id)" if missing else "")
                + ".",
                recommendation="Re-ingest with the current ingest_citygml.py (tiered "
                               "id resolution: dedup identical content/geometry, split "
                               "distinct features onto synthesized ids).",
                needs_decision=True))
        elif collapsed > 0:
            deduped = with_id - actual  # duplicate occurrences merged on purpose
            out.append(_anomaly(
                "ID_COLLISIONS_RESOLVED", "info", "cross", collapsed,
                f"{collapsed} duplicate-id occurrence(s) in the source were resolved "
                f"at ingest: {synth} split onto synthesized ids "
                f"(source_gml_id preserved), {max(deduped, 0)} deduplicated as "
                f"identical content/geometry.",
                recommendation="None; intended behaviour of the id resolution."))

        # (2) ingest integrity: graph nodes = distinct raw ids + synthesized splits
        #     - intentional dedups. actual < distinct means buildings are absent.
        expected = distinct + synth
        delta = actual - expected
        if actual < distinct:
            out.append(_anomaly(
                "INGEST_INCOMPLETE", "high", "cross", distinct - actual,
                f"Graph has {actual} Building nodes but the source has {distinct} "
                f"distinct gml:ids: {distinct - actual} expected buildings are absent "
                f"from the graph (interrupted ingest, a subset was loaded, or "
                f"identical-content duplicates were deduplicated across sources).",
                recommendation="Confirm the full input was ingested; check the "
                               "dedup count against the duplicate-id report."))
        elif delta > 0:
            out.append(_anomaly(
                "BUILDING_COUNT_MISMATCH", "medium", "cross", delta,
                f"Graph has {actual} Building nodes but the source accounts for "
                f"{expected} ({distinct} distinct gml:ids + {synth} synthesized "
                f"splits): {delta} more than expected (residual buildings from a "
                f"prior ingest, or cross-dataset id reuse).",
                recommendation="Confirm the graph holds only this city's dataset."))
    return out


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def _latest(pattern: str) -> Path | None:
    files = sorted(RESULTS_DIR.glob(pattern))
    return files[-1] if files else None


def build_report(city: str) -> dict:
    # Anchor to the _<timestamp> suffix so city "zurich" does not also match a
    # differently-suffixed run like "zurich_smoke".
    pre_f = _latest(f"stats_control_precheck_{city}_[0-9]*.json")
    post_f = _latest(f"stats_control_postcheck_{city}_[0-9]*.json")
    pre = json.loads(pre_f.read_text(encoding="utf-8")) if pre_f else None
    post = json.loads(post_f.read_text(encoding="utf-8")) if post_f else None

    anomalies = []
    if pre:
        if pre.get("citygml"):
            anomalies += citygml_anomalies(pre["citygml"])
        if pre.get("osm"):
            anomalies += osm_anomalies(pre["osm"])
    if post:
        anomalies += graph_anomalies(post)
    anomalies += cross_checks(pre, post)
    anomalies.sort(key=lambda a: (SEVERITY_RANK.get(a["severity"], 0), a["count"] or 0),
                   reverse=True)

    return {
        "city": city,
        "generated": _ts(),
        "precheck_file": pre_f.name if pre_f else None,
        "postcheck_file": post_f.name if post_f else None,
        "precheck": pre,
        "postcheck": post,
        "anomalies": anomalies,
        "decisions_needed": [a for a in anomalies if a["needs_decision"]],
    }


def _md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_markdown(rep: dict) -> str:
    city = rep["city"]
    L = [f"# Statistical + anomaly control: {city}",
         "",
         f"Generated {rep['generated']} by `eval/stats_control.py`. "
         f"Precheck: `{rep['precheck_file']}`. Postcheck: `{rep['postcheck_file']}`.",
         ""]

    sev = {}
    for a in rep["anomalies"]:
        sev[a["severity"]] = sev.get(a["severity"], 0) + 1
    banner = ", ".join(f"{sev[s]} {s}" for s in ("critical", "high", "medium", "low", "info") if s in sev)
    L += [f"**{len(rep['anomalies'])} anomalies** ({banner or 'none'}); "
          f"**{len(rep['decisions_needed'])} need your decision.**", ""]

    # ---- decisions ------------------------------------------------------- #
    if rep["decisions_needed"]:
        L += ["## Decisions needed", ""]
        L.append(_md_table(
            ["#", "Severity", "Code", "What", "Recommended fix"],
            [[i + 1, a["severity"].upper(), a["code"], a["message"], a["recommendation"]]
             for i, a in enumerate(rep["decisions_needed"])]))
        L.append("")

    # ---- full anomaly list ---------------------------------------------- #
    L += ["## All anomalies", ""]
    if rep["anomalies"]:
        L.append(_md_table(
            ["Severity", "Scope", "Code", "Count", "Detail"],
            [[a["severity"].upper(), a["scope"], a["code"], a["count"] if a["count"] is not None else "-", a["message"]]
             for a in rep["anomalies"]]))
    else:
        L.append("_No anomalies detected._")
    L.append("")

    # ---- input statistics ------------------------------------------------ #
    pre = rep.get("precheck") or {}
    cg = pre.get("citygml")
    if cg:
        L += ["## Input statistics (CityGML)", "",
              _md_table(["Metric", "Value"], [
                  ["Files", cg["files"]],
                  ["Building elements", cg["buildings_total"]],
                  ["Distinct gml:ids", cg["distinct_gml_ids"]],
                  ["Dominant CRS", f"{cg['dominant_srs']} ({cg['dominant_epsg']})"],
                  ["measuredHeight (n/min/med/max)", f"{cg['height'].get('n')}/{cg['height'].get('min')}/{cg['height'].get('median')}/{cg['height'].get('max')}"],
                  ["Vertices/building (med/p99/max)", f"{cg['vertex_count'].get('median')}/{cg['vertex_count'].get('p99')}/{cg['vertex_count'].get('max')}"],
                  ["Roofmat coverage", f"{cg['roofmat']['buildings_with_prediction']} ({cg['roofmat']['coverage']*100:.1f}%)"],
              ]), ""]
        if cg["roofmat"]["class_histogram"]:
            L += ["Predicted roof-material classes: " +
                  ", ".join(f"{k} ({v})" for k, v in cg["roofmat"]["class_histogram"].items()), ""]
    osm = pre.get("osm")
    if osm and not osm.get("error"):
        L += ["## Input statistics (OSM)", ""]
        counts = osm.get("layer_feature_counts") or {}
        if osm.get("ogrinfo_available", True) and any(v is not None for v in counts.values()):
            L += [_md_table(["Layer", "Features"],
                            [[k, v if v is not None else "n/a"] for k, v in counts.items()]), ""]
        elif osm.get("ogrinfo_available", True):
            L += ["_ogrinfo returned no counts (GDAL/PROJ env conflict); see "
                  "OGRINFO_FAILED. OSM completeness is gated by "
                  "eval/osm_ingestion_census.py after fusion._", ""]
        L += [f"_{osm['note']}_", ""]

    # ---- graph statistics ----------------------------------------------- #
    post = rep.get("postcheck") or {}
    if post:
        L += ["## Graph statistics (Neo4j)", "",
              _md_table(["Metric", "Value"], [
                  ["Dataset", post.get("dataset")],
                  ["Total nodes", post.get("totals", {}).get("nodes")],
                  ["Total relationships", post.get("totals", {}).get("relationships")],
                  ["Building nodes", post.get("building_count")],
                  ["Boundaries/building (med/p99/max)", f"{post['boundaries_per_building'].get('median')}/{post['boundaries_per_building'].get('p99')}/{post['boundaries_per_building'].get('max')}"],
                  ["measured_height (n/min/med/max)", f"{post['measured_height'].get('n')}/{post['measured_height'].get('min')}/{post['measured_height'].get('median')}/{post['measured_height'].get('max')}"],
              ])]
        def _dist_line(label, dd):
            if dd and not dd.get("error"):
                L.append("")
                L.append(f"{label}: min {dd.get('min')}, "
                         f"avg {round(dd.get('avg') or 0, 1)}, median {dd.get('p50')}, "
                         f"p90 {dd.get('p90')}, p99 {dd.get('p99')}, max {dd.get('max')}.")
        _dist_line("Nodes per building (CityGML subtree)", post.get("nodes_per_building"))
        _dist_line("Geometry polygons per building", post.get("geoms_per_building"))
        _dist_line("Thematic attributes per building", post.get("attrs_per_building"))
        L.append("")
        # label + rel inventory
        L += ["### Node labels", "",
              _md_table(["Label", "Count"],
                        sorted(post.get("label_counts", {}).items(), key=lambda kv: kv[1], reverse=True)), ""]
        L += ["### Relationship types", "",
              _md_table(["Type", "Count"],
                        sorted(post.get("relationship_counts", {}).items(), key=lambda kv: kv[1], reverse=True)), ""]
        if post.get("osm", {}).get("osm_feature_nodes"):
            o = post["osm"]
            L += ["### OSM enrichment", "",
                  _md_table(["Metric", "Value"], [
                      ["OsmFeature nodes", o["osm_feature_nodes"]],
                      ["By kind", ", ".join(f"{k}:{v}" for k, v in o.get("by_kind", {}).items())],
                      ["Matched / orphan", f"{o.get('matched')} / {o.get('orphan')} ({o.get('orphan_rate',0)*100:.1f}% orphan)"],
                      ["Enriched buildings", o.get("enriched_buildings")],
                      ["POIs attached", o.get("pois")],
                  ]), ""]

    # ---- paper-ready block ---------------------------------------------- #
    L += ["## Paper-ready summary statistics", "",
          "```json",
          json.dumps(_paper_block(rep), indent=2, ensure_ascii=False),
          "```", ""]
    return "\n".join(L)


def _paper_block(rep: dict) -> dict:
    pre = rep.get("precheck") or {}
    cg = pre.get("citygml") or {}
    post = rep.get("postcheck") or {}
    return {
        "city": rep["city"],
        "input_building_elements": cg.get("buildings_total"),
        "input_distinct_ids": cg.get("distinct_gml_ids"),
        "graph_building_nodes": post.get("building_count"),
        "graph_total_nodes": post.get("totals", {}).get("nodes"),
        "graph_total_relationships": post.get("totals", {}).get("relationships"),
        "nodes_per_building": post.get("nodes_per_building"),
        "geoms_per_building": post.get("geoms_per_building"),
        "attrs_per_building": post.get("attrs_per_building"),
        "measured_height_m": post.get("measured_height"),
        "osm_feature_nodes": post.get("osm", {}).get("osm_feature_nodes"),
        "osm_orphan_rate": post.get("osm", {}).get("orphan_rate"),
        "roofmat_coverage": post.get("roofmat", {}).get("coverage"),
        "anomaly_counts_by_severity": {
            s: sum(1 for a in rep["anomalies"] if a["severity"] == s)
            for s in ("critical", "high", "medium", "low", "info")
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  wrote {path}")


def cmd_precheck(args):
    city = _city_slug(args.city)
    rep = {"phase": "precheck", "city": city, "generated": _ts()}
    if args.citygml:
        print("Scanning CityGML input...")
        rep["citygml"] = scan_citygml(args.citygml)
    if args.roofmats:
        print("Scanning roofmats CityGML variant...")
        rep["roofmats"] = scan_citygml([args.roofmats], progress=False)
    if args.lod3:
        print("Scanning LoD3 CityGML variant...")
        rep["lod3"] = scan_citygml([args.lod3], progress=False)
    if args.osm:
        print("Scanning OSM .pbf (ogrinfo)...")
        rep["osm"] = scan_osm(args.osm)

    out = RESULTS_DIR / f"stats_control_precheck_{city}_{_ts()}.json"
    _write_json(out, rep)

    an = []
    if rep.get("citygml"):
        an += citygml_anomalies(rep["citygml"])
    if rep.get("osm"):
        an += osm_anomalies(rep["osm"])
    _print_anomalies(an, "PRECHECK")


def cmd_postcheck(args):
    city = _city_slug(args.city)
    print("Scanning Neo4j graph...")
    g = scan_graph(args.dataset, args.source_like, args.deep, args.orphans)
    out = RESULTS_DIR / f"stats_control_postcheck_{city}_{_ts()}.json"
    _write_json(out, {"phase": "postcheck", "city": city, "generated": _ts(), **g})
    _print_anomalies(graph_anomalies(g), "POSTCHECK")


def cmd_report(args):
    city = _city_slug(args.city)
    rep = build_report(city)
    out_json = RESULTS_DIR / f"stats_control_report_{city}_{_ts()}.json"
    out_md = STATS_DIR / f"stats_control_{city}.md"
    _write_json(out_json, rep)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(rep), encoding="utf-8")
    print(f"  wrote {out_md}")
    _print_anomalies(rep["anomalies"], f"REPORT: {city}")
    if rep["decisions_needed"]:
        print(f"\n>>> {len(rep['decisions_needed'])} decision(s) needed - see "
              f"'Decisions needed' in {out_md}")


def cmd_render(path: str):
    rep = json.loads(Path(path).read_text(encoding="utf-8"))
    if rep.get("phase") == "postcheck" or "label_counts" in rep:
        # wrap a bare scan into a minimal report
        rep = {"city": rep.get("city", "dataset"), "generated": _ts(),
               "precheck_file": None, "postcheck_file": Path(path).name,
               "precheck": None, "postcheck": rep,
               "anomalies": graph_anomalies(rep),
               "decisions_needed": [a for a in graph_anomalies(rep) if a["needs_decision"]]}
    out_md = STATS_DIR / f"stats_control_{_city_slug(rep['city'])}.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(rep), encoding="utf-8")
    print(f"  wrote {out_md}")


def _print_anomalies(anomalies: list[dict], banner: str):
    anomalies = sorted(anomalies, key=lambda a: SEVERITY_RANK.get(a["severity"], 0), reverse=True)
    print(f"\n===== {banner}: {len(anomalies)} anomalies =====")
    for a in anomalies:
        flag = "  [DECISION NEEDED]" if a["needs_decision"] else ""
        print(f"[{a['severity'].upper():8}] {a['code']}: {a['message']}{flag}")
        if a["examples"]:
            print(f"           e.g. {', '.join(map(str, a['examples'][:5]))}")
    if not anomalies:
        print("  (clean)")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--render", metavar="REPORT.json",
                   help="rebuild the markdown control document from a saved JSON")
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("precheck", help="scan input files (no database)")
    pc.add_argument("--city", required=True)
    pc.add_argument("--citygml", nargs="+", help="file(s) and/or directory(ies)")
    pc.add_argument("--roofmats", help="roofmats CityGML variant (predictedroofmaterial)")
    pc.add_argument("--lod3", help="LoD3 CityGML variant")
    pc.add_argument("--osm", help="input .osm.pbf")

    po = sub.add_parser("postcheck", help="scan the ingested Neo4j graph")
    po.add_argument("--city", required=True)
    po.add_argument("--dataset", help="Dataset name (default: the only dataset)")
    po.add_argument("--source-like", help="restrict Building checks to source CONTAINS this")
    po.add_argument("--deep", action="store_true",
                    help="also compute nodes-per-building (subtree, APOC) + no-geometry "
                         "(bounded by building count)")
    po.add_argument("--orphans", action="store_true",
                    help="also run the full-graph orphan anti-join (can be minutes on "
                         "multi-million-node graphs)")

    rp = sub.add_parser("report", help="fuse latest precheck+postcheck into one document")
    rp.add_argument("--city", required=True)

    args = p.parse_args()
    if args.render:
        cmd_render(args.render)
        return
    if args.cmd == "precheck":
        cmd_precheck(args)
    elif args.cmd == "postcheck":
        cmd_postcheck(args)
    elif args.cmd == "report":
        cmd_report(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
