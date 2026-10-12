#!/usr/bin/env python3
"""
Ingest ML-predicted roof materials (per building) into the pykci CityGML graph.

This is the ML-predicted layer of the multi-source graph (authoritative CityGML +
crowd-sourced OSM + ML-predicted roof materials + reconstructed LoD3). It attaches
one classifier's per-building roof-material predictions --- each a ranked list of
material classes with the pixel-coverage fraction the model assigned to each --- to
the existing authoritative Building nodes.

Matching is by building ``gml:id`` only (the GeoJSON's ``properties.gml_id`` equals
the CityGML/ALKIS building id); the footprint geometry in the GeoJSON is ignored, so
NO spatial matching is performed. Ingestion is additive and never overwrites an
authoritative attribute: every written key is in the derived ``predicted_roof_material*``
namespace, and every enriched building records the layer's provenance ``source``.

Input GeoJSON feature shape (geometry ignored):
    { "properties": { "gml_id": "DEHHALKAP00013PP",
                      "predicted_roof_materials": ["4", "1"],   # material class codes
                      "material_cov": [0.4323, 0.5677] } }       # pixel coverage, aligned
The code->name dictionary is a ``;``-separated CSV (``roof_materials.csv``):
    0;concrete / 1;metal / 2;glass / 3;roof_tiles / 4;tar_paper

Schema written on each matched Building (see ROOF_MATERIAL_INGESTION.md):
  - predictedroofmaterial              primary (highest-coverage) material NAME
                                       (kept for build_3dtiles.py --color-by roofmat
                                       and export_roofmat_footprints.py compatibility)
  - predicted_roof_material_<i>        material NAME, ranked by coverage descending
                                       (i = 0 .. count-1; _0 is the primary)
  - predicted_roof_material_<i>_coverage
                                       fraction of the building's roof pixels the
                                       classifier assigned to that material (0..1).
                                       This is the per-material confidence signal;
                                       named "coverage" because it is an areal
                                       fraction, not a calibrated posterior.
  - predicted_roof_material_count      number of predicted materials for the building
  - predicted_roof_material_source     provenance tag of this ML layer

Idempotent: prior predicted_roof_material* keys (this layer only, identified by the
source tag) are cleared before writing, so re-running reflects the current input.

Usage:
    py ingest_roof_materials.py input/geojson/hamburg_roofmats_with_citygml_footprints_2025.geojson
    py ingest_roof_materials.py <file.geojson> [--dict roof_materials.csv]
        [--dataset NAME] [--source-tag STR] [--batch 5000]
"""
import argparse
import json
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")

# Bounded set of prediction keys to clear for idempotency: at most one index per
# roof-material class, so <material-class-count> indices is a safe upper bound.
MAX_MATERIALS = 8


def load_material_dict(path: Path) -> dict:
    """Parse the ``code;name`` roof-material dictionary CSV."""
    mapping = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(";")
        if len(parts) >= 2 and parts[0].strip() != "":
            mapping[parts[0].strip()] = parts[1].strip()
    if not mapping:
        raise SystemExit(f"No entries parsed from material dictionary {path}")
    return mapping


def build_props(codes, covs, code2name, source_tag):
    """Build the property dict for one building from aligned code/coverage lists.

    Materials are ranked by coverage descending, so index 0 is the dominant class.
    Unknown codes (not in the dictionary) fall back to ``roofmat_<code>`` so nothing
    is silently dropped.
    """
    pairs = []
    for code, cov in zip(codes, covs):
        name = code2name.get(str(code), f"roofmat_{code}")
        try:
            cov = round(float(cov), 6)
        except (TypeError, ValueError):
            cov = None
        pairs.append((name, cov))
    # rank by coverage descending (None coverages sort last)
    pairs.sort(key=lambda p: (p[1] is not None, p[1]), reverse=True)

    props = {
        "predictedroofmaterial": pairs[0][0],            # primary name (compat key)
        "predicted_roof_material_count": len(pairs),
        "predicted_roof_material_source": source_tag,
    }
    for i, (name, cov) in enumerate(pairs):
        props[f"predicted_roof_material_{i}"] = name
        if cov is not None:
            props[f"predicted_roof_material_{i}_coverage"] = cov
    return props


def reset_prior_layer(session, source_tag, dataset):
    """Remove this ML layer's keys from any building that carries its source tag, so a
    re-run cannot leave stale higher-index keys behind. No-op on a fresh graph."""
    keys = ["predictedroofmaterial", "predicted_roof_material_count",
            "predicted_roof_material_source"]
    for i in range(MAX_MATERIALS):
        keys += [f"predicted_roof_material_{i}", f"predicted_roof_material_{i}_coverage"]
    removes = ", ".join(f"b.{k}" for k in keys)
    where = "b.predicted_roof_material_source IS NOT NULL"
    params = {}
    if dataset:
        where += " AND (b)-[:PART_OF]->(:Dataset {name: $ds})"
        params["ds"] = dataset
    # CALL (b) { ... } IN TRANSACTIONS batches the writes; the driving scan is a
    # bounded Building label scan (never an all-nodes scan).
    res = session.run(
        f"MATCH (b:Building) WHERE {where} "
        f"CALL (b) {{ REMOVE {removes} }} IN TRANSACTIONS OF 10000 ROWS", **params).consume()
    return res.counters.properties_set  # informational


def main():
    ap = argparse.ArgumentParser(description="Ingest ML-predicted roof materials into the pykci graph (by gml:id).")
    ap.add_argument("geojson", help="Per-building roof-material GeoJSON (gml_id + predicted_roof_materials + material_cov)")
    ap.add_argument("--dict", dest="dict_path", default=None,
                    help="code;name roof-material dictionary CSV (default: roof_materials.csv beside the GeoJSON)")
    ap.add_argument("--dataset", default=None, help="Scope reset to this Dataset name (matching is by gml:id regardless)")
    ap.add_argument("--source-tag", default=None,
                    help="Provenance tag stored as predicted_roof_material_source (default: ml:roof_material:<geojson-stem>)")
    ap.add_argument("--batch", type=int, default=5000, help="UNWIND batch size for writes")
    args = ap.parse_args()

    geojson = Path(args.geojson)
    if not geojson.exists():
        raise SystemExit(f"GeoJSON not found: {geojson}")
    dict_path = Path(args.dict_path) if args.dict_path else geojson.parent / "roof_materials.csv"
    if not dict_path.exists():
        raise SystemExit(f"Roof-material dictionary not found: {dict_path}")
    source_tag = args.source_tag or f"ml:roof_material:{geojson.stem}"

    code2name = load_material_dict(dict_path)
    print(f"Roof-material classes: {code2name}")

    print(f"Loading {geojson.name} ...")
    t0 = time.perf_counter()
    with geojson.open(encoding="utf-8") as fh:
        data = json.load(fh)
    feats = data.get("features", [])
    print(f"  {len(feats):,} features loaded in {time.perf_counter()-t0:.1f}s")

    # Build one write-row per feature that has a gml_id + at least one material.
    rows, dup_ids, skipped = [], 0, 0
    seen = set()
    for f in feats:
        p = f.get("properties", {})
        gid = p.get("gml_id")
        mats = p.get("predicted_roof_materials") or []
        covs = p.get("material_cov") or []
        if not gid or not mats:
            skipped += 1
            continue
        if gid in seen:
            dup_ids += 1
            continue
        seen.add(gid)
        rows.append({"id": gid, "props": build_props(mats, covs, code2name, source_tag)})
    print(f"  {len(rows):,} buildings with a prediction "
          f"({skipped} features skipped, {dup_ids} duplicate gml_ids ignored)")

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    with driver.session(database="neo4j") as s:
        # 0. idempotency: clear any prior run of this layer
        reset_prior_layer(s, source_tag, args.dataset)

        # 1. additive write, matched by gml:id (Entity index); count real matches
        matched = 0
        t0 = time.perf_counter()
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            rec = s.run(
                "UNWIND $rows AS row MATCH (b:Building {id: row.id}) "
                "SET b += row.props RETURN count(b) AS n", rows=chunk).single()
            matched += rec["n"] if rec else 0
        print(f"  enriched {matched:,} buildings in {time.perf_counter()-t0:.1f}s")

        # 2. coverage accounting (absence != negative): buildings without a prediction
        total_b = s.run("MATCH (b:Building) RETURN count(b) AS n").single()["n"]
        with_pred = s.run("MATCH (b:Building) WHERE b.predicted_roof_material_source = $src "
                          "RETURN count(b) AS n", src=source_tag).single()["n"]
        # material distribution over primary class
        dist = {r["m"]: r["n"] for r in s.run(
            "MATCH (b:Building) WHERE b.predicted_roof_material_source = $src "
            "RETURN b.predictedroofmaterial AS m, count(*) AS n ORDER BY n DESC", src=source_tag)}
    driver.close()

    unmatched_in_graph = len(rows) - matched
    summary = {
        "geojson": geojson.name,
        "source_tag": source_tag,
        "material_dict": code2name,
        "features_in_geojson": len(feats),
        "buildings_with_prediction_in_input": len(rows),
        "buildings_enriched_in_graph": matched,
        "input_ids_not_found_as_building": unmatched_in_graph,
        "graph_buildings_total": total_b,
        "graph_buildings_with_prediction": with_pred,
        "graph_buildings_without_prediction": total_b - with_pred,
        "coverage_pct": round(100.0 * with_pred / total_b, 2) if total_b else 0.0,
        "primary_material_distribution": dist,
    }
    out = Path("output") / f"{geojson.stem}_roofmat_summary.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("# Roof-material ingestion summary\n\n```json\n"
                   + json.dumps(summary, indent=2, ensure_ascii=False) + "\n```\n", encoding="utf-8")
    print(f"\nEnriched {matched:,}/{len(rows):,} predicted buildings "
          f"({unmatched_in_graph} input ids not found as Building).")
    print(f"Coverage: {with_pred:,}/{total_b:,} graph buildings carry a prediction "
          f"({summary['coverage_pct']}%).")
    print(f"Primary-material distribution: {dist}")
    print(f"Summary: {out}")
    if matched == 0:
        print("WARNING: no buildings matched -- are the gml:ids in the graph? "
              "Is the right dataset ingested?", file=sys.stderr)


if __name__ == "__main__":
    main()
