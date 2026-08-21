"""
Enrich Neo4j Building nodes with predicted roof materials from a GeoJSON file.

Source: hamburg_roof_materials_with_citygml_footprints_2025.geojson
  - gml_id          : matches Building.id in Neo4j
  - predicted_roof_materials : list of material codes [0-4]
  - material_cov    : corresponding coverage ratios (sum ≤ 1.0)

Graph pattern added (mirrors HAS_ROOF_TYPE → RoofType):
  (Building) -[:HAS_ROOF_MATERIAL {coverage: float}]-> (RoofMaterial {code: str, name: str})

Usage:
    python enrich_roof_materials.py
    python enrich_roof_materials.py --geojson path/to/file.geojson
"""

import argparse
import json
import os
import time
from pathlib import Path

from neo4j import GraphDatabase

NEO4J_URL = os.getenv("NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

MATERIAL_NAMES = {
    "0": "concrete",
    "1": "metal",
    "2": "glass",
    "3": "roof_tiles",
    "4": "tar_paper",
}

DEFAULT_GEOJSON = (
    Path(__file__).parent.parent
    / "hamburg_citygml2_lod2_2025_roofmats"
    / "hamburg_roof_materials_with_citygml_footprints_2025.geojson"
)

BATCH_SIZE = 500


def load_geojson(path: Path) -> list[dict]:
    print(f"Loading {path} …")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    features = data["features"]
    print(f"  {len(features):,} features loaded.")
    return features


def build_batches(features: list[dict]) -> list[list[dict]]:
    """Convert GeoJSON features to enrichment records and split into batches."""
    records = []
    skipped = 0
    for feat in features:
        p = feat["properties"]
        gml_id = p.get("gml_id")
        codes = p.get("predicted_roof_materials", [])
        coverages = p.get("material_cov", [])
        if not gml_id or not codes:
            skipped += 1
            continue
        # Pair each code with its coverage (default 1.0 if missing)
        materials = []
        for i, code in enumerate(codes):
            cov = coverages[i] if i < len(coverages) else 1.0
            name = MATERIAL_NAMES.get(str(code), f"unknown_{code}")
            materials.append({"code": str(code), "name": name, "coverage": float(cov)})
        records.append({"gml_id": gml_id, "materials": materials})

    if skipped:
        print(f"  Skipped {skipped} features with missing gml_id or materials.")

    batches = [records[i : i + BATCH_SIZE] for i in range(0, len(records), BATCH_SIZE)]
    return batches


MERGE_QUERY = """
UNWIND $rows AS row
MATCH (b:Building {id: row.gml_id})
WITH b, row
UNWIND row.materials AS mat
MERGE (rm:RoofMaterial {code: mat.code})
  ON CREATE SET rm.name = mat.name
MERGE (b)-[r:HAS_ROOF_MATERIAL {code: mat.code}]->(rm)
  SET r.coverage = mat.coverage
RETURN count(r) AS rels_written
"""


def enrich(driver, batches: list[list[dict]]) -> None:
    total_rels = 0
    total_batches = len(batches)
    t0 = time.perf_counter()

    with driver.session() as session:
        for i, batch in enumerate(batches, 1):
            result = session.run(MERGE_QUERY, rows=batch)
            total_rels += result.single()["rels_written"]
            if i % 50 == 0 or i == total_batches:
                elapsed = time.perf_counter() - t0
                print(
                    f"  Batch {i}/{total_batches}  "
                    f"relationships written: {total_rels:,}  "
                    f"elapsed: {elapsed:.1f}s"
                )

    elapsed = time.perf_counter() - t0
    print(f"\nDone. {total_rels:,} HAS_ROOF_MATERIAL relationships written in {elapsed:.1f}s.")


def print_summary(driver) -> None:
    with driver.session() as session:
        mat_counts = session.run(
            "MATCH (rm:RoofMaterial)<-[r:HAS_ROOF_MATERIAL]-() "
            "RETURN rm.code AS code, rm.name AS name, count(r) AS buildings "
            "ORDER BY buildings DESC"
        ).data()
        print("\nRoofMaterial nodes in graph:")
        for row in mat_counts:
            print(f"  {row['code']} ({row['name']}): {row['buildings']:,} buildings")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich Neo4j with roof material predictions")
    parser.add_argument(
        "--geojson",
        default=str(DEFAULT_GEOJSON),
        help="Path to the GeoJSON file with roof material predictions",
    )
    args = parser.parse_args()

    path = Path(args.geojson)
    if not path.exists():
        print(f"[error] File not found: {path}")
        return

    driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        print("Connected to Neo4j.")
    except Exception as exc:
        print(f"[error] Cannot reach Neo4j at {NEO4J_URL}: {exc}")
        driver.close()
        return

    features = load_geojson(path)
    batches = build_batches(features)
    print(f"  {sum(len(b) for b in batches):,} buildings to enrich in {len(batches)} batches.")

    enrich(driver, batches)
    print_summary(driver)
    driver.close()


if __name__ == "__main__":
    main()
