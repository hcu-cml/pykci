#!/usr/bin/env python3
"""
citygml3_tests.py — end-to-end tests of the CityGML 3.0 codec (ingest_citygml.py) and the
3.0 / 2.0 writers (export_citygml.py) against a THROWAWAY Neo4j.

  T1  (with --sample) a real CityGML 3.0 file: ingest, per-type counts vs. the counts of
      the same elements in the file, no sub-feature leakage (CityObject label / PART_OF),
      no pending references
  T2  (with --sample) idempotency: re-ingest → identical graph digest
  T3  (with --sample) export 3.0 → XSD-valid, round-trip census lossless (appearance included)
  T4  (with --sample) export 2.0 → XSD-valid (+ loss report)
  T5  kitchen-sink fixture (every element type): 3.0 lossless + valid, 2.0 valid
  T6  tile merge (eval/fixtures/citygml3/tile_b.gml then tile_a.gml): shared Road /
      Section / TrafficSpace merged, cross-tile references resolved, re-ingest
      identical, merged exports valid
  T7  (with --sample) 2.0-sourced data → 3.0: the T4 2.0 file re-ingested (2.0
      TrafficAreas) and exported as 3.0 with synthesised spaces → valid
  T8  optional real tiles (--tiles a.gml b.gml …): one Dataset, layer 'streetspace',
      3.0 + 2.0 exports valid
  T9  (with --tiles) clipped --bbox export (--box, default a 400 m box around the centre
      of the tiles' Dataset) as 3.0 and 2.0: valid, no written (Auxiliary)TrafficArea/Marking
      entirely outside the box, no empty Road; export time
  T11 dynamizer fixture (eval/fixtures/citygml3/dynamizer.gml): a SensorConnection and a
      GenericTimeseries Dynamizer → Dynamizer sub-features (HAS_DYNAMIZER), idempotent,
      3.0 export valid + lossless, 2.0 export valid with the dynamizers in the loss report
  T10 (with --tiles) --backfill-bbox reproduces the sub-feature bboxes written at ingest
      after they are removed (old-graph situation)

The database is WIPED. The script refuses bolt://…:7687 (the usual port of a real graph)
and any non-empty database unless --wipe is given. T5, T6 and T11 need only the fixtures in
eval/fixtures/citygml3; the others run when --sample / --tiles name real files. The XSD
checks need the OGC schema bundle once: `python tools/validate_citygml.py --setup`.

    python eval/citygml3_tests.py --uri bolt://localhost:7688 --password … --wipe \
        [--out DIR] [--sample city3.gml] [--tiles tile1.gml tile2.gml] [--box WKT]
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
VALIDATOR = ROOT / "tools" / "validate_citygml.py"
FIX = ROOT / "eval" / "fixtures" / "citygml3"

# graph label (or relationship type) -> element local name counted in the sample file
_SAMPLE_COUNTS = {
    "Road": "Road", "Railway": "Railway", "Track": "Track", "Square": "Square",
    "Section": "Section", "Intersection": "Intersection", "TrafficSpace": "TrafficSpace",
    "AuxiliaryTrafficSpace": "AuxiliaryTrafficSpace", "TrafficArea": "TrafficArea",
    "AuxiliaryTrafficArea": "AuxiliaryTrafficArea", "Marking": "Marking",
    "PlantCover": "PlantCover", "SolitaryVegetationObject": "SolitaryVegetationObject",
    "Bridge": "Bridge", "CityFurniture": "CityFurniture", "Building": "Building",
    "WallSurface": "WallSurface", "RoofSurface": "RoofSurface", "GroundSurface": "GroundSurface",
    "HAS_PROTOTYPE": "ImplicitGeometry", "RELATED_TO": "CityObjectRelation",
    "Dynamizer": "Dynamizer",
}
_MASK = re.compile(r"poly_[0-9a-f]{16}|ID_[0-9a-f-]{36}")
_SPATIAL_PROPS = {"bbox", "gtype"}


def sample_expect(path):
    """How many of each element the file holds, as the ingest should count them."""
    from lxml import etree
    seen = {}
    for _, el in etree.iterparse(str(path), events=("start",), huge_tree=True):
        if isinstance(el.tag, str):
            ln = etree.QName(el).localname
            seen[ln] = seen.get(ln, 0) + 1
    return {k: seen[v] for k, v in _SAMPLE_COUNTS.items() if seen.get(v)}


def run(cmd, log, ok_codes=(0,)):
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    log.write(f"$ {' '.join(map(str, cmd))}\n{p.stdout}\n{p.stderr}\n")
    if p.returncode not in ok_codes:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n{p.stderr[-2000:]}")
    return p, time.perf_counter() - t0


def validate(path, version, log):
    p, _ = run([PY, VALIDATOR, path, "--version", str(version)], log, ok_codes=(0, 1))
    res = re.search(r"XSD result: (\S+)", p.stdout)
    refs = re.search(r"(\d[\d,]*) unresolved", p.stdout)
    return (res.group(1) if res else "ERROR"), int(refs.group(1).replace(",", "")) if refs else -1


def census(src, exp, log, appearance=True):
    cmd = [PY, ROOT / "eval" / "roundtrip_census.py", "--gml", src, "--export", exp, "--show", "5"]
    if appearance:
        cmd.append("--include-appearance")
    p, _ = run(cmd, log, ok_codes=(0, 1))
    m = re.search(r"RESULT: (\d+) missing", p.stdout)
    return 0 if "LOSSLESS" in p.stdout else (int(m.group(1)) if m else -1)


def digest(sess):
    lines = []
    for r in sess.run("MATCH (n) WHERE NOT n:SpatialLayer AND size(labels(n)) > 0 "
                      "RETURN labels(n) AS l, properties(n) AS p"):
        p = {k: _MASK.sub("X", str(v)) for k, v in r["p"].items() if k not in _SPATIAL_PROPS}
        lines.append(json.dumps([sorted(r["l"]), p], sort_keys=True))
    for r in sess.run("MATCH (a)-[x]->(b) WHERE NOT type(x) STARTS WITH 'RTREE' AND type(x) <> 'LAYER' "
                      "RETURN type(x) AS t, properties(x) AS p, a.id AS a, b.id AS b"):
        lines.append(json.dumps([r["t"], str(r["a"]), str(r["b"]),
                                 {k: str(v) for k, v in r["p"].items()}], sort_keys=True))
    lines.sort()
    return hashlib.sha1("\n".join(lines).encode()).hexdigest(), len(lines)


def wipe(sess):
    sess.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()


def count(sess, what):
    if what.upper() == what:
        return sess.run(f"MATCH ()-[r:{what}]->() RETURN count(r) AS c").single()["c"]
    if what in ("WallSurface", "RoofSurface", "GroundSurface"):
        return sess.run("MATCH (n:BoundarySurface {surface_type: $t}) RETURN count(n) AS c",
                        t=what).single()["c"]
    return sess.run(f"MATCH (n:{what}) RETURN count(n) AS c").single()["c"]


def sample_tests(s, sample, out, log, results, ingest, export):
    """T1-T4 and T7 on a real CityGML 3.0 file."""
    expect = sample_expect(sample)
    n_areas = expect.get("TrafficArea", 0) + expect.get("AuxiliaryTrafficArea", 0)
    # T1 + T2
    wipe(s)
    _, t = ingest(sample, "--rtree-layer", "citygml3")
    got = {k: count(s, k) for k in expect}
    leaks = s.run("MATCH (n) WHERE (n:TrafficSpace OR n:AuxiliaryTrafficSpace OR "
                  "n:BoundarySurface OR n:Section OR n:Intersection OR n:Marking OR "
                  "n:GeometryPrototype OR n:Appearance) AND (n:CityObject OR (n)-[:PART_OF]->()) "
                  "RETURN count(n) AS c").single()["c"]
    pending = s.run("MATCH (n:PendingRefs) RETURN count(n) AS c").single()["c"]
    results["T1_sample_ingest"] = {"ok": got == expect and leaks == 0 and pending == 0,
                                 "seconds": round(t, 1), "counts": got,
                                 "expected": expect, "subfeature_leaks": leaks,
                                 "pending": pending}
    d1 = digest(s)
    ingest(sample, "--rtree-layer", "citygml3")
    d2 = digest(s)
    rt = s.run("MATCH ()-[r:RTREE_REFERENCE]->(n) RETURN count(r) AS r, count(DISTINCT n) AS n").single()
    results["T2_idempotency"] = {"ok": d1 == d2 and rt["r"] == rt["n"], "digest": d1[0],
                                 "lines": d1[1], "rtree_refs": rt["r"], "rtree_nodes": rt["n"]}
    # T3 / T4
    v3, v2 = out / "sample_v3.gml", out / "sample_v2.gml"
    export(3, v3)
    val3 = validate(v3, 3, log)
    miss3 = census(sample, v3, log)
    results["T3_sample_export_v3"] = {"ok": val3 == ("VALID", 0) and miss3 == 0,
                                    "xsd": val3[0], "unresolved": val3[1], "census_missing": miss3}
    export(2, v2)
    val2 = validate(v2, 2, log)
    loss = json.loads(v2.with_suffix(".loss.json").read_text())["loss"]
    results["T4_sample_export_v2"] = {"ok": val2 == ("VALID", 0), "xsd": val2[0],
                                    "unresolved": val2[1], "loss_total": loss["total"],
                                    "loss": loss["by_category"]}
    # T7 2.0-sourced → 3.0
    wipe(s)
    ingest(v2)
    tas = s.run("MATCH (:Road)-[h:HAS_BOUNDARY]->(t) WHERE h.prop IN ['trafficArea', "
                "'auxiliaryTrafficArea'] RETURN count(t) AS c").single()["c"]
    s23 = out / "sample_2to3.gml"
    export(3, s23)
    sval = validate(s23, 3, log)
    synth = len(re.findall(r'gml:id="[^"]+__space"', s23.read_text()))
    results["T7_v2_sourced_to_v3"] = {"ok": sval == ("VALID", 0) and synth == tas == n_areas,
                                      "v2_traffic_areas": tas, "synthesised_spaces": synth,
                                      "v3": sval}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", required=True)
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--wipe", action="store_true", help="allow wiping a non-empty database")
    ap.add_argument("--out", default=str(ROOT / "output" / "citygml3_tests"))
    ap.add_argument("--sample", default=None,
                    help="a real CityGML 3.0 file for T1-T4 and T7 (skipped without it)")
    ap.add_argument("--tiles", nargs="*", default=[])
    ap.add_argument("--box", default=None,
                    help="WKT polygon for T9 (default: 400 m around the tiles' Dataset centre)")
    args = ap.parse_args()
    if re.search(r":7687\b", args.uri):
        raise SystemExit("refusing to run against port 7687 (live pykci graph); use a throwaway DB")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "citygml3_tests.log", "w")
    conn = ["--uri", args.uri, "--user", args.user, "--password", args.password]
    drv = GraphDatabase.driver(args.uri, auth=(args.user, args.password),
                               notifications_disabled_classifications={"DEPRECATION"})
    results = {}

    def ingest(path, *extra):
        return run([PY, "ingest_citygml.py", str(path), *conn, "--writers", "4", *extra], log)

    def export(version, dest, *extra):
        return run([PY, "export_citygml.py", "--version", str(version), "--out", str(dest), *conn, *extra], log)

    with drv.session() as s:
        n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        if n and not args.wipe:
            raise SystemExit(f"database holds {n} nodes; pass --wipe to allow wiping it")

        if args.sample:
            sample_tests(s, Path(args.sample), out, log, results, ingest, export)
        # T5 kitchen sink
        wipe(s)
        ingest(FIX / "kitchen_sink.gml", "--rtree-layer", "streetspace")
        ks3, ks2 = out / "ks_v3.gml", out / "ks_v2.gml"
        export(3, ks3)
        export(2, ks2)
        k3, k2 = validate(ks3, 3, log), validate(ks2, 2, log)
        kmiss = census(FIX / "kitchen_sink.gml", ks3, log)
        results["T5_kitchen_sink"] = {"ok": k3 == ("VALID", 0) and k2 == ("VALID", 0) and kmiss == 0,
                                      "v3": k3, "v2": k2, "census_missing_v3": kmiss}
        # T11 dynamizers
        wipe(s)
        ingest(FIX / "dynamizer.gml", "--rtree-layer", "streetspace")
        dyn = s.run("MATCH (f:CityFurniture {id: 'fx_charger'})-[:HAS_DYNAMIZER]->(d:Dynamizer) "
                    "RETURN count(d) AS n, collect(d.datastream_id) AS ds, collect(d.dynamic_data_type) AS types, "
                    "sum(coalesce(d.n_values, 0)) AS values, "
                    "size([x IN collect(d) WHERE x:CityObject OR (x)-[:PART_OF]->()]) AS leaks").single()
        dg1 = digest(s)
        ingest(FIX / "dynamizer.gml", "--rtree-layer", "streetspace")
        dg2 = digest(s)
        dy3, dy2 = out / "dyn_v3.gml", out / "dyn_v2.gml"
        export(3, dy3)
        export(2, dy2)
        d3, d2 = validate(dy3, 3, log), validate(dy2, 2, log)
        dmiss = census(FIX / "dynamizer.gml", dy3, log)
        dloss = json.loads(dy2.with_suffix(".loss.json").read_text())["loss"]["by_category"]
        results["T11_dynamizer"] = {
            "ok": dyn["n"] == 2 and dyn["leaks"] == 0 and dyn["values"] == 3 and dg1 == dg2
            and d3 == ("VALID", 0) and d2 == ("VALID", 0) and dmiss == 0
            and dloss.get("dyn:Dynamizer (no 2.0 equivalent)") == 2,
            "dynamizers": dyn["n"], "datastreams": dyn["ds"], "types": dyn["types"], "values": dyn["values"],
            "idempotent": dg1 == dg2, "v3": d3, "v2": d2, "census_missing_v3": dmiss,
            "v2_loss": dloss.get("dyn:Dynamizer (no 2.0 equivalent)")}
        # T6 tile merge
        wipe(s)
        for t_ in ("tile_b.gml", "tile_a.gml"):
            ingest(FIX / t_, "--dataset-name", "streetspace_test", "--rtree-layer", "streetspace")
        chk = s.run(
            "MATCH (r:Road {id: 'road_shared'}) WITH count(r) AS roads, collect(r)[0] AS r "
            "MATCH (sec:Section {id: 'sec_shared'})-[h:HAS_TRAFFIC_SPACE]->() "
            "WITH roads, r, count(h) AS sec_spaces "
            "MATCH (ts:TrafficSpace {id: 'ts_group_shared'})-[:HAS_BOUNDARY]->(a) "
            "WITH roads, r, sec_spaces, count(a) AS ts_areas "
            "OPTIONAL MATCH (x)-[c:PREDECESSOR|SUCCESSOR|RELATED_TO]->() WHERE c.href IS NOT NULL "
            "RETURN roads, size(r.source_files) AS files, sec_spaces, ts_areas, count(c) AS cross").single()
        pend = s.run("MATCH (n:PendingRefs) RETURN count(n) AS c").single()["c"]
        t1 = digest(s)
        for t_ in ("tile_b.gml", "tile_a.gml"):
            ingest(FIX / t_, "--dataset-name", "streetspace_test", "--rtree-layer", "streetspace")
        t2 = digest(s)
        tv3, tv2 = out / "tiles_v3.gml", out / "tiles_v2.gml"
        export(3, tv3)
        export(2, tv2)
        tval3, tval2 = validate(tv3, 3, log), validate(tv2, 2, log)
        merged = dict(chk)
        results["T6_tile_merge"] = {
            "ok": merged == {"roads": 1, "files": 2, "sec_spaces": 6, "ts_areas": 4, "cross": 3}
            and pend == 0 and t1 == t2 and tval3 == ("VALID", 0) and tval2 == ("VALID", 0),
            "merged": merged, "pending": pend, "reingest_identical": t1 == t2,
            "v3": tval3, "v2": tval2}
        # T8 real tiles
        if args.tiles:
            wipe(s)
            secs = 0.0
            for t_ in args.tiles:
                _, dt = ingest(t_, "--dataset-name", "tiles", "--rtree-layer", "streetspace",
                               "--source-type", "reconstructed")
                secs += dt
            top = s.run("MATCH (n)-[:PART_OF]->(:Dataset {id: 'tiles'}) RETURN count(n) AS c").single()["c"]
            pend = s.run("MATCH (n:PendingRefs) RETURN count(n) AS c").single()["c"]
            r3, r2 = out / "tiles_real_v3.gml", out / "tiles_real_v2.gml"
            _, e3 = export(3, r3)
            _, e2 = export(2, r2)
            rv3, rv2 = validate(r3, 3, log), validate(r2, 2, log)
            results["T8_real_tiles"] = {"ok": rv3 == ("VALID", 0) and rv2 == ("VALID", 0),
                                        "tiles": [Path(t_).name for t_ in args.tiles],
                                        "top_level_features": top, "pending_refs_left": pend,
                                        "ingest_seconds": round(secs, 1),
                                        "export_seconds": [round(e3, 1), round(e2, 1)],
                                        "v3": rv3, "v2": rv2}
            # T9 clipped bbox export
            if not args.box:
                c = s.run("MATCH (d:Dataset {id: 'tiles'}) RETURN (d.bbox_min_x + d.bbox_max_x) / 2 AS x, "
                          "(d.bbox_min_y + d.bbox_max_y) / 2 AS y").single()
                x0, y0 = c["x"] - 200, c["y"] - 200
                args.box = (f"POLYGON(({x0} {y0}, {x0 + 400} {y0}, {x0 + 400} {y0 + 400}, "
                            f"{x0} {y0 + 400}, {x0} {y0}))")
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", args.box)]
            box = (min(nums[0::2]), min(nums[1::2]), max(nums[0::2]), max(nums[1::2]))
            t9 = {"ok": True}
            for ver in (3, 2):
                dest = out / f"box_v{ver}.gml"
                _p9, dt = run([PY, "export_citygml.py", "--version", str(ver), "--out", str(dest),
                               "--bbox", args.box, *conn], log)
                val = validate(dest, ver, log)
                from lxml import etree
                doc = etree.parse(str(dest))
                gml = "{http://www.opengis.net/gml/3.2}" if ver == 3 else "{http://www.opengis.net/gml}"
                outside = total = empty = 0
                for el in doc.iter():
                    if not isinstance(el.tag, str):
                        continue
                    ln = etree.QName(el).localname
                    if ln in ("TrafficArea", "AuxiliaryTrafficArea", "Marking"):
                        xs, ys = [], []
                        for pl in el.iter(gml + "posList"):
                            v = pl.text.split()
                            xs += [float(a) for a in v[0::3]]
                            ys += [float(a) for a in v[1::3]]
                        total += 1
                        if xs and (max(xs) < box[0] or min(xs) > box[2] or max(ys) < box[1] or min(ys) > box[3]):
                            outside += 1
                    elif ln == "Road" and not any(etree.QName(x).localname in
                                                  ("TrafficArea", "AuxiliaryTrafficArea", "Marking")
                                                  for x in el.iter() if isinstance(x.tag, str)):
                        empty += 1
                t9[f"v{ver}"] = {"xsd": val, "seconds": round(dt, 1),
                                 "mb": round(dest.stat().st_size / 1e6, 1), "areas": total,
                                 "areas_outside_box": outside, "empty_roads": empty}
                t9["ok"] = t9["ok"] and val == ("VALID", 0) and outside == 0 and empty == 0
            results["T9_clipped_bbox_export"] = t9
            # T10 backfill reproduces the ingest bboxes
            sub = ("(n:Section OR n:Intersection OR n:TrafficSpace OR n:AuxiliaryTrafficSpace OR "
                   "n:Hole OR n:ClearanceSpace OR n:Marking OR n:BridgePart OR n:NestedFeature OR "
                   "n:BoundarySurface OR n:Opening OR n:BuildingPart)")
            snap = {r["id"]: r["b"] for r in s.run(
                f"MATCH (n) WHERE {sub} AND n.bbox_min_x IS NOT NULL RETURN n.id AS id, "
                "[n.bbox_min_x, n.bbox_min_y, n.bbox_min_z, n.bbox_max_x, n.bbox_max_y, n.bbox_max_z] AS b")}
            s.run(f"MATCH (n) WHERE {sub} REMOVE n.bbox_min_x, n.bbox_min_y, n.bbox_min_z, "
                  "n.bbox_max_x, n.bbox_max_y, n.bbox_max_z").consume()
            _, dt = run([PY, "ingest_citygml.py", "--backfill-bbox", "--dataset", "tiles", *conn], log)
            back = {r["id"]: r["b"] for r in s.run(
                f"MATCH (n) WHERE {sub} AND n.bbox_min_x IS NOT NULL RETURN n.id AS id, "
                "[n.bbox_min_x, n.bbox_min_y, n.bbox_min_z, n.bbox_max_x, n.bbox_max_y, n.bbox_max_z] AS b")}
            diffs = sum(1 for k in snap if k in back and any(abs(a - b) > 1e-9 for a, b in zip(snap[k], back[k])))
            again, _ = run([PY, "ingest_citygml.py", "--backfill-bbox", "--dataset", "tiles", *conn], log)
            m = re.search(r"Backfill: ([\d,]+)", again.stdout)
            rerun = int(m.group(1).replace(",", "")) if m else -1
            results["T10_backfill"] = {"ok": len(snap) == len(back) and diffs == 0 and rerun == 0,
                                       "ingest_bboxes": len(snap), "backfilled": len(back),
                                       "value_differences": diffs, "seconds": round(dt, 1),
                                       "rerun_written": rerun}
    drv.close()
    (out / "citygml3_tests.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for k, v in results.items():
        print(f"{'PASS' if v['ok'] else 'FAIL'}  {k}: " +
              json.dumps({x: y for x, y in v.items() if x not in ('ok', 'counts', 'expected', 'loss')}))
    print(f"details: {out / 'citygml3_tests.json'}  log: {out / 'citygml3_tests.log'}")
    sys.exit(0 if all(v["ok"] for v in results.values()) else 1)


if __name__ == "__main__":
    main()
