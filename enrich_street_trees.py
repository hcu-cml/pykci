"""
enrich_street_trees.py

Integrate a Hamburg street-tree CityGML (Straßenbaumkataster, veg:SolitaryVegetationObject
with lod*ImplicitRepresentation) into the Neo4j semantic city graph — reproducing the
two-step workflow used to add the trees alongside the buildings:

  1. INGEST   — parse + load the CityGML via the project's ingest_citygml.ingest().
                Creates SolitaryVegetationObject/CityObject nodes, implicit geometry
                (transformation matrix + reference point) and resolves the shared
                xlink:href prototype geometry.

  2. LOCATE + INDEX (post-pass) — street trees carry NO gml:boundedBy, so the standard
     ingest leaves them without center_x/center_y/location/bbox_wkt and out of the
     spatial 'features' R-tree. Their true world position lives in the implicit
     reference_point on the linked GeometryMultiSurface. This pass derives
     center_x/center_y/ground_z/location + a small (default 1 m) bbox_wkt footprint
     from that reference_point and registers every tree in the 'features' R-tree, so
     trees become spatially queryable alongside the buildings.

Both steps are idempotent (MERGE / "only where location IS NULL").

Usage:
    python enrich_street_trees.py [path/to/trees.gml]
    python enrich_street_trees.py trees.gml --footprint 1.5
    python enrich_street_trees.py --skip-ingest          # re-run only the post-pass
    python enrich_street_trees.py --layer features --batch 5000

Neo4j connection (env overrides): NEO4J_URL / NEO4J_USER / NEO4J_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

# Reuse the project's CityGML ingester (handles Vegetation + implicit geometry).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_citygml import ingest  # noqa: E402

NEO4J_URL      = os.getenv("NEO4J_URL",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

DEFAULT_GML = Path(
    "/home/luarzou/Documents/llmCityGML/"
    "Strassenbaumkataster_Sommer_Hamburg_2024_03_15/"
    "Strassenbaumkataster_20210901_Sommerbaeume.gml"
)


# ---------------------------------------------------------------------------
# Step 1 — ingest the CityGML
# ---------------------------------------------------------------------------
def ingest_gml(driver, gml_path: Path) -> dict:
    print(f"[1/2] Ingesting {gml_path.name} …")
    t0 = time.perf_counter()
    # nearby_edges=False: proximity/community edges are a building enrichment and
    # expensive; not relevant for trees.
    result = ingest(gml_path, driver, nearby_edges=False)
    print(f"      ingest done in {time.perf_counter() - t0:.1f}s "
          f"(features_total={result.get('features_total')})")
    return result


# ---------------------------------------------------------------------------
# Step 2 — locate + spatially index the trees
# ---------------------------------------------------------------------------
def ensure_features_layer(session, layer: str) -> None:
    """Create the WKT spatial layer if it doesn't exist yet (idempotent)."""
    existing = [r["name"] for r in session.run(
        "CALL spatial.layers() YIELD name RETURN name")]
    if layer not in existing:
        session.run("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", layer=layer)
        print(f"      created spatial layer '{layer}'")
    else:
        print(f"      spatial layer '{layer}' already exists")


_SET_LOCATION = """
UNWIND $rows AS row
MATCH (t:SolitaryVegetationObject {id: row.id})
SET t.center_x = row.x, t.center_y = row.y, t.ground_z = row.z,
    t.location  = point({x: row.x, y: row.y}),
    t.bbox_min_x = row.x0, t.bbox_min_y = row.y0, t.bbox_min_z = row.z,
    t.bbox_max_x = row.x1, t.bbox_max_y = row.y1, t.bbox_max_z = row.z,
    t.bbox_wkt   = row.wkt
"""


def locate_trees(session, source: str | None, half: float, batch: int) -> int:
    """
    Derive center_x/center_y/ground_z/location and a (2*half m) bbox_wkt footprint for
    every SolitaryVegetationObject from its implicit-geometry reference_point. The WKT
    is built in Python (no fragile Cypher-string-in-string quoting). Only touches trees
    that aren't located yet (idempotent). Returns count processed.
    """
    src_filter = "AND t.source = $source" if source else ""
    rows = session.run(f"""
        MATCH (t:SolitaryVegetationObject)-[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
        WHERE ms.reference_point IS NOT NULL AND t.location IS NULL {src_filter}
        RETURN t.id AS id, ms.reference_point AS rp
    """, source=source)

    updates, seen = [], set()
    for r in rows:
        tid = r["id"]
        if tid in seen:               # a tree can have lod1+lod2 implicit reps -> dedup
            continue
        p = (r["rp"] or "").split()
        if len(p) < 3:
            continue
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        x0, x1, y0, y1 = x - half, x + half, y - half, y + half
        wkt = (f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, "
               f"{x0} {y1}, {x0} {y0}))")
        updates.append({"id": tid, "x": x, "y": y, "z": z,
                        "x0": x0, "x1": x1, "y0": y0, "y1": y1, "wkt": wkt})
        seen.add(tid)

    for i in range(0, len(updates), batch):
        session.run(_SET_LOCATION, rows=updates[i:i + batch])
    print(f"      located: {len(updates)} trees")
    return len(updates)


def index_trees(session, layer: str, source: str | None, batch: int) -> int:
    """
    Register located trees (bbox_wkt set) into the 'features' R-tree layer, batched.

    Idempotent: spatial.addNodes is NOT idempotent (re-adding a node inserts a duplicate
    R-tree entry), so each batch is removeNodes -> addNodes. On a first run removeNodes is
    a no-op; on a re-run it clears prior entries so the tree count in the layer stays exact.
    """
    src_filter = "AND t.source = $source" if source else ""
    ids = [r["id"] for r in session.run(
        f"MATCH (t:SolitaryVegetationObject) WHERE t.bbox_wkt IS NOT NULL {src_filter} "
        "RETURN t.id AS id", source=source)]
    total = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        session.run("""
            MATCH (t:SolitaryVegetationObject) WHERE t.id IN $ids
            WITH collect(t) AS batch
            CALL spatial.removeNodes($layer, batch) YIELD count RETURN count
        """, ids=chunk, layer=layer)
        res = session.run("""
            MATCH (t:SolitaryVegetationObject) WHERE t.id IN $ids
            WITH collect(t) AS batch
            CALL spatial.addNodes($layer, batch) YIELD count RETURN count
        """, ids=chunk, layer=layer).single()
        total += res["count"] if res else 0
    print(f"      indexed: {total} trees into '{layer}' (remove+add, idempotent)")
    return total


def verify(session, layer: str, source: str | None) -> None:
    src_filter = "WHERE t.source = $source" if source else ""
    n   = session.run(f"MATCH (t:SolitaryVegetationObject) {src_filter} "
                      "RETURN count(t) AS c", source=source).single()["c"]
    loc = session.run(f"MATCH (t:SolitaryVegetationObject) "
                      f"{'WHERE' if not source else 'WHERE t.source=$source AND'} "
                      "t.location IS NOT NULL RETURN count(t) AS c",
                      source=source).single()["c"]
    fc = session.run("CALL spatial.getFeatureCount($layer) YIELD count RETURN count",
                     layer=layer).single()["count"]
    print(f"      trees total={n:,} | located={loc:,} | '{layer}' feature count={fc:,}")


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gml", nargs="?", default=str(DEFAULT_GML),
                    help="Path to the street-tree CityGML (default: Hamburg Straßenbaumkataster)")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Skip step 1; only run the locate+index post-pass")
    ap.add_argument("--footprint", type=float, default=1.0,
                    help="Tree footprint size in metres for the R-tree bbox (default 1.0)")
    ap.add_argument("--layer", default="features", help="Spatial layer name (default 'features')")
    ap.add_argument("--batch", type=int, default=5000, help="Post-pass batch size (default 5000)")
    ap.add_argument("--all-sources", action="store_true",
                    help="Post-pass over ALL SolitaryVegetationObject, not just this file's source")
    args = ap.parse_args()

    gml_path = Path(args.gml)
    if not args.skip_ingest and not gml_path.exists():
        sys.exit(f"ERROR: GML not found: {gml_path}")
    source = None if args.all_sources else gml_path.name

    driver = GraphDatabase.driver(
        NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications={"UNRECOGNIZED", "DEPRECATION"},
    )
    driver.verify_connectivity()

    if not args.skip_ingest:
        ingest_gml(driver, gml_path)
    else:
        print("[1/2] ingest skipped (--skip-ingest)")

    print(f"[2/2] Locating + indexing trees "
          f"(source={'<all>' if source is None else source}, footprint={args.footprint} m) …")
    half = args.footprint / 2.0
    with driver.session() as s:
        ensure_features_layer(s, args.layer)
        locate_trees(s, source, half, args.batch)
        index_trees(s, args.layer, source, args.batch)
        verify(s, args.layer, source)

    driver.close()
    print("Done.")


if __name__ == "__main__":
    main()
