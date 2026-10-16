"""
enrich_street_trees.py

Integrate a Hamburg street-tree CityGML (Straßenbaumkataster, veg:SolitaryVegetationObject
with lod*ImplicitRepresentation) into the Neo4j semantic city graph:

  1. INGEST      — parse + load the CityGML via the project's ingest_citygml.ingest():
                   SolitaryVegetationObject/CityObject nodes with their generic attributes,
                   one implicit GeometryMultiSurface per tree (transformation_matrix,
                   reference_point, prototype_href), the Dataset node and PART_OF.
                   nearby_edges=False and skip_spatial_index=True: both are building passes
                   over the WHOLE graph (the R-tree pass re-adds every Building); the trees
                   are indexed in step 4.

  2. PROTOTYPES  — the trees share a few prototype geometries (gml:TriangulatedSurface),
                   written inline once and referenced by xlink:href everywhere else.
                   ingest_citygml only reads MultiSurface prototypes, and resolves xlinks by
                   copying every prototype polygon edge onto each instance (Hamburg: 224k
                   trees x ~615 triangles = ~138 M relationships). This step stores each
                   prototype ONCE, with its triangles stored like ingest_citygml's TIN relief:
                       (:GeometryPrototype {geometry_type: 'TriangulatedSurface'})
                         -[:HAS_SURFACE_MEMBER {order}]->(:GeometryPolygon)
                         -[:HAS_EXTERIOR_RING]->(:GeometryRing)
                   and links every tree's implicit geometry to it with a single edge:
                       (:GeometryMultiSurface {is_implicit})-[:HAS_PROTOTYPE]->(:GeometryPrototype)
                   The prototypes' CityGML Appearance (one ParameterizedTexture + X3DMaterial
                   per prototype) is kept with them: image URI / mime type / theme / material
                   on the prototype, verbatim texture coordinates on each ring, so the textured
                   trees can be rebuilt from the graph alone (build_tree_tiles.py).
                   GeometryPrototype is deliberately not a GeometryMultiSurface: a re-run of
                   ingest_citygml would otherwise resolve the xlinks against it and copy the
                   triangles onto every instance after all.

  3. LOCATE      — street trees carry NO gml:boundedBy, so the ingest leaves them without a
                   position. From the reference point and the transformed prototype:
                   center_x/center_y (the trunk), ground_z, location, bbox_* (crown extent and
                   tree height) and bbox_wkt (crown footprint; at least --footprint metres).

  4. INDEX       — register the located trees in the 'features' R-tree (removeNodes ->
                   addNodes per batch, so a re-run does not duplicate entries).

  5. TIDY        — the file has no municipality key, so the ingest names a District and a
                   City after the file. Those are removed again; the tree Dataset COVERS the
                   district(s) of the city model instead and records the licence/attribution
                   comments of the file.

Every step is idempotent (MERGE / SET); step 3 only touches trees without a location
unless --relocate is given.

Usage:
    python enrich_street_trees.py path/to/trees.gml
    python enrich_street_trees.py trees.gml --skip-ingest      # re-run steps 2-5 only
    python enrich_street_trees.py trees.gml --relocate         # recompute every position

Neo4j connection (env overrides): NEO4J_URI (or NEO4J_URL) / NEO4J_USER / NEO4J_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
from lxml import etree
from neo4j import GraphDatabase

# Reuse the project's CityGML ingester (handles Vegetation + implicit geometry).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_citygml import _slug, ingest  # noqa: E402

NEO4J_URL      = os.getenv("NEO4J_URI") or os.getenv("NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

_GML  = "http://www.opengis.net/gml"
_CORE = "http://www.opengis.net/citygml/2.0"
_APP  = "http://www.opengis.net/citygml/appearance/2.0"
_GML_ID = f"{{{_GML}}}id"


def _batches(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# ---------------------------------------------------------------------------
# Step 1 — ingest the CityGML
# ---------------------------------------------------------------------------
def ingest_gml(driver, gml_path: Path) -> dict:
    print(f"[1/5] Ingesting {gml_path.name} …", flush=True)
    t0 = time.perf_counter()
    result = ingest(gml_path, driver, nearby_edges=False, skip_spatial_index=True)
    print(f"      ingest done in {time.perf_counter() - t0:.1f}s "
          f"(features_total={result.get('features_total')})", flush=True)
    return result


# ---------------------------------------------------------------------------
# Step 2 — prototype geometry + appearance
# ---------------------------------------------------------------------------
def read_prototypes(gml_path: Path) -> tuple[dict, str | None]:
    """One streaming pass over the file: every inline gml:TriangulatedSurface of an
    ImplicitGeometry (with the tree that holds it inline), and the Appearance that
    targets the prototypes (ParameterizedTexture per prototype, X3DMaterial per ring)."""
    protos: dict[str, dict] = {}
    ring_proto: dict[str, str] = {}
    theme = None
    tags = [f"{{{_CORE}}}ImplicitGeometry", f"{{{_CORE}}}cityObjectMember",
            f"{{{_APP}}}ParameterizedTexture", f"{{{_APP}}}X3DMaterial", f"{{{_APP}}}theme"]
    for _, el in etree.iterparse(str(gml_path), events=("end",), tag=tags, huge_tree=True):
        tag = etree.QName(el).localname
        if tag == "ImplicitGeometry":
            ts = el.find(f"{{{_CORE}}}relativeGMLGeometry/{{{_GML}}}TriangulatedSurface")
            if ts is None:
                continue
            prop = el.getparent()                         # veg:lod{N}ImplicitRepresentation
            m = re.match(r"lod(\d)", etree.QName(prop).localname)
            tris = []
            for tri in ts.iter(f"{{{_GML}}}Triangle"):
                ring = tri.find(f"{{{_GML}}}exterior/{{{_GML}}}LinearRing")
                if ring is None:
                    continue
                pl = ring.findtext(f"{{{_GML}}}posList")
                if pl is None:
                    pl = " ".join(p.text for p in ring.findall(f"{{{_GML}}}pos") if p.text)
                tris.append([ring.get(_GML_ID), " ".join(pl.split()), None])
            pid = ts.get(_GML_ID)
            protos[pid] = {"owner": prop.getparent().get(_GML_ID),
                           "lod": m.group(1) if m else "2",
                           "srs_name": ts.get("srsName"),
                           "srs_dimension": int(ts.get("srsDimension") or 3),
                           "triangles": tris, "props": {}}
            for rid, _, _ in tris:
                if rid:
                    ring_proto[rid] = pid
        elif tag == "cityObjectMember":
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
        elif tag == "theme":
            theme = (el.text or "").strip() or None
        elif tag == "ParameterizedTexture":
            image = el.findtext(f"{{{_APP}}}imageURI")
            info = {"texture_image": image.strip() if image else None,
                    "texture_mime_type": el.findtext(f"{{{_APP}}}mimeType"),
                    "texture_wrap_mode": el.findtext(f"{{{_APP}}}wrapMode")}
            for tgt in el.findall(f"{{{_APP}}}target"):
                pid = (tgt.get("uri") or "").lstrip("#")
                if pid not in protos:
                    continue
                protos[pid]["props"].update(info)
                uv = {(tc.get("ring") or "").lstrip("#"): " ".join((tc.text or "").split())
                      for tc in tgt.iter(f"{{{_APP}}}textureCoordinates")}
                for tri in protos[pid]["triangles"]:
                    tri[2] = uv.get(tri[0])
            el.clear()
        elif tag == "X3DMaterial":
            props = {f"x3d_{_snake(etree.QName(c).localname)}": (c.text or "").strip()
                     for c in el if etree.QName(c).localname != "target"}
            targets = {(t.text or "").strip().lstrip("#") for t in el.findall(f"{{{_APP}}}target")}
            for pid in {ring_proto[t] for t in targets if t in ring_proto} | (targets & protos.keys()):
                protos[pid]["props"].update(props)
            el.clear()
    return protos, theme


def write_prototypes(s, protos: dict, theme: str | None, source: str, batch: int) -> None:
    s.run("CREATE INDEX geometryprototype_id_idx IF NOT EXISTS "
          "FOR (n:GeometryPrototype) ON (n.id)")
    rows = []
    for pid, p in protos.items():
        pts = np.array([float(v) for _, pl, _ in p["triangles"] for v in pl.split()])
        pts = pts.reshape(-1, p["srs_dimension"])
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        rows.append({"id": pid, "source": source, "geometry_type": "TriangulatedSurface",
                     "lod": p["lod"], "srs_name": p["srs_name"],
                     "srs_dimension": p["srs_dimension"], "triangle_count": len(p["triangles"]),
                     "owner_feature_id": p["owner"],
                     "local_min_x": float(lo[0]), "local_min_y": float(lo[1]),
                     "local_min_z": float(lo[2]), "local_max_x": float(hi[0]),
                     "local_max_y": float(hi[1]), "local_max_z": float(hi[2]),
                     "appearance_theme": theme if p["props"].get("texture_image") else None,
                     **p["props"]})
    s.run("UNWIND $rows AS row MERGE (p:GeometryPrototype {id: row.id}) SET p += row, p:Entity",
          rows=rows)

    polys, rings = [], []
    for pid, p in protos.items():
        for i, (rid, pl, uv) in enumerate(p["triangles"]):
            poly = f"tri_{pid}_{i}"
            polys.append({"proto": pid, "poly": poly, "order": i})
            rings.append({"poly": poly, "id": rid or f"ring_{poly}", "pos_list": pl,
                          "srs_dimension": p["srs_dimension"], "uv": uv})
    for chunk in _batches(polys, batch):
        s.run("""
            UNWIND $rows AS row
            MATCH (p:GeometryPrototype {id: row.proto})
            MERGE (g:GeometryPolygon {id: row.poly}) SET g.source = $src, g:Entity
            MERGE (p)-[r:HAS_SURFACE_MEMBER]->(g) SET r.order = row.order, r.via_xlink = false
        """, rows=chunk, src=source)
    for chunk in _batches(rings, batch):
        s.run("""
            UNWIND $rows AS row
            MATCH (g:GeometryPolygon {id: row.poly})
            MERGE (r:GeometryRing {id: row.id})
            SET r.pos_list = row.pos_list, r.srs_dimension = row.srs_dimension,
                r.ring_type = 'exterior', r.source = $src, r.texture_coordinates = row.uv
            MERGE (g)-[:HAS_EXTERIOR_RING]->(r)
        """, rows=chunk, src=source)
    textured = sum(1 for r in rings if r["uv"])
    print(f"      prototypes: {len(rows)} ({len(polys):,} triangles, "
          f"{textured:,} with texture coordinates)", flush=True)


def link_prototypes(s, protos: dict, source: str, batch: int) -> None:
    """The tree that holds a prototype inline gets prototype_href too (prototype_inline
    marks where the GML defines it); then every implicit geometry → its prototype."""
    s.run("""
        UNWIND $rows AS row
        MATCH (t:CityObject {id: row.fid})-[h:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
        WHERE ms.is_implicit AND coalesce(h.lod, ms.lod) = row.lod
          AND coalesce(ms.prototype_href, row.proto) = row.proto
        SET ms.prototype_href = row.proto, ms.prototype_inline = true
    """, rows=[{"fid": p["owner"], "lod": p["lod"], "proto": pid} for pid, p in protos.items()])
    ms_rows = s.run("""
        MATCH (ms:GeometryMultiSurface {source: $src})
        WHERE ms.prototype_href IS NOT NULL AND NOT (ms)-[:HAS_PROTOTYPE]->()
        RETURN ms.id AS ms, ms.prototype_href AS proto
    """, src=source).data()
    for chunk in _batches(ms_rows, batch):
        s.run("""
            UNWIND $rows AS row
            MATCH (ms:GeometryMultiSurface {id: row.ms})
            MATCH (p:GeometryPrototype {id: row.proto})
            MERGE (ms)-[:HAS_PROTOTYPE]->(p)
        """, rows=chunk)
    print(f"      linked: {len(ms_rows):,} implicit geometries -> HAS_PROTOTYPE", flush=True)


# ---------------------------------------------------------------------------
# Step 3 — locate the trees
# ---------------------------------------------------------------------------
def _hull(xy: np.ndarray) -> np.ndarray:
    """Convex hull (monotone chain) of 2D points; the crown footprint extent of a
    rotated, scaled prototype is the extent of its rotated, scaled hull."""
    pts = sorted(set(map(tuple, np.round(xy, 4))))
    if len(pts) < 3:
        return np.array(pts)

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return np.array(half(pts) + half(pts[::-1]))


def _prototype_shapes(s, source: str) -> dict:
    shapes = {}
    for r in s.run("""
        MATCH (p:GeometryPrototype {source: $src})-[:HAS_SURFACE_MEMBER]->(:GeometryPolygon)
              -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
        RETURN p.id AS id, p.srs_dimension AS dim, collect(r.pos_list) AS pls
    """, src=source):
        pts = np.array([float(v) for pl in r["pls"] for v in pl.split()]).reshape(-1, r["dim"] or 3)
        shapes[r["id"]] = {"hull": _hull(pts[:, :2]),
                           "zmin": float(pts[:, 2].min()), "zmax": float(pts[:, 2].max())}
    return shapes


_SET_LOCATION = """
UNWIND $rows AS row
MATCH (t:CityObject {id: row.id})
SET t.center_x = row.x, t.center_y = row.y, t.ground_z = row.z,
    t.location  = point({x: row.x, y: row.y}),
    t.bbox_min_x = row.x0, t.bbox_min_y = row.y0, t.bbox_min_z = row.z0,
    t.bbox_max_x = row.x1, t.bbox_max_y = row.y1, t.bbox_max_z = row.z1,
    t.bbox_wkt   = row.wkt
"""


def locate_trees(s, source: str | None, footprint: float, batch: int, relocate: bool) -> int:
    """center_x/center_y/ground_z/location from the implicit reference point (the trunk);
    bbox_* from the prototype transformed by the tree's matrix (crown extent, height);
    bbox_wkt = the crown footprint, at least `footprint` metres wide. The WKT is built in
    Python (no fragile Cypher-string-in-string quoting)."""
    shapes = _prototype_shapes(s, source) if source else {}
    src_filter = "AND t.source = $source" if source else ""
    loc_filter = "" if relocate else "AND t.location IS NULL"
    rows = s.run(f"""
        MATCH (t:SolitaryVegetationObject)-[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
        WHERE ms.reference_point IS NOT NULL {loc_filter} {src_filter}
        OPTIONAL MATCH (ms)-[:HAS_PROTOTYPE]->(p:GeometryPrototype)
        RETURN t.id AS id, ms.reference_point AS rp, ms.transformation_matrix AS tm, p.id AS proto
    """, source=source)

    half = footprint / 2.0
    updates, seen, degenerate = [], set(), 0
    for r in rows:
        tid = r["id"]
        if tid in seen:               # a tree can have lod1+lod2 implicit reps -> dedup
            continue
        p = (r["rp"] or "").split()
        if len(p) < 3:
            continue
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        x0, x1, y0, y1, z0, z1 = x - half, x + half, y - half, y + half, z, z
        shape = shapes.get(r["proto"])
        m = [float(v) for v in (r["tm"] or "").split()]
        if shape is not None and len(m) == 16:
            a = np.array(m).reshape(4, 4)
            ext = shape["hull"] @ a[:2, :2].T + a[:2, 3]
            if np.ptp(ext[:, 0]) <= 0 or np.ptp(ext[:, 1]) <= 0:
                degenerate += 1
            x0, x1 = min(x - half, x + ext[:, 0].min()), max(x + half, x + ext[:, 0].max())
            y0, y1 = min(y - half, y + ext[:, 1].min()), max(y + half, y + ext[:, 1].max())
            zs = (a[2, 2] * shape["zmin"] + a[2, 3], a[2, 2] * shape["zmax"] + a[2, 3])
            z0, z1 = z + min(zs), z + max(zs)
        wkt = f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"
        updates.append({"id": tid, "x": x, "y": y, "z": z, "x0": x0, "x1": x1,
                        "y0": y0, "y1": y1, "z0": z0, "z1": z1, "wkt": wkt})
        seen.add(tid)

    for chunk in _batches(updates, batch):
        s.run(_SET_LOCATION, rows=chunk)
    print(f"      located: {len(updates):,} trees"
          + (f" ({degenerate:,} with a zero crown scale: footprint {footprint} m)"
             if degenerate else ""), flush=True)
    return len(updates)


# ---------------------------------------------------------------------------
# Step 4 — spatially index the trees
# ---------------------------------------------------------------------------
def ensure_features_layer(s, layer: str) -> None:
    """Create the WKT spatial layer if it doesn't exist yet (idempotent)."""
    existing = [r["name"] for r in s.run("CALL spatial.layers() YIELD name RETURN name")]
    if layer not in existing:
        s.run("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", layer=layer)
        print(f"      created spatial layer '{layer}'", flush=True)


def index_trees(s, layer: str, source: str | None, batch: int) -> int:
    """Register located trees (bbox_wkt set) in the R-tree layer, batched and serial
    (concurrent R-tree writes corrupt the tree). Each batch is removeNodes -> addNodes, so
    a re-run replaces the entries instead of adding duplicates."""
    src_filter = "AND t.source = $source" if source else ""
    ids = [r["id"] for r in s.run(
        f"MATCH (t:SolitaryVegetationObject) WHERE t.bbox_wkt IS NOT NULL {src_filter} "
        "RETURN t.id AS id", source=source)]
    total, t0 = 0, time.perf_counter()
    for i, chunk in enumerate(_batches(ids, batch)):
        try:
            s.run("""
                MATCH (t:CityObject) WHERE t.id IN $ids
                WITH collect(t) AS batch
                CALL spatial.removeNodes($layer, batch) YIELD count RETURN count
            """, ids=chunk, layer=layer).consume()
        except Exception as exc:          # nodes not in the layer yet (first run)
            if i == 0:
                print(f"      (removeNodes skipped: {str(exc).splitlines()[0][:120]})", flush=True)
        res = s.run("""
            MATCH (t:CityObject) WHERE t.id IN $ids
            WITH collect(t) AS batch
            CALL spatial.addNodes($layer, batch) YIELD count RETURN count
        """, ids=chunk, layer=layer).single()
        total += res["count"] if res else 0
        print(f"      indexed {total:,}/{len(ids):,} ({time.perf_counter() - t0:.0f}s)", flush=True)
    return total


# ---------------------------------------------------------------------------
# Step 5 — district / city / dataset metadata
# ---------------------------------------------------------------------------
def tidy_dataset(s, gml_path: Path, source: str) -> None:
    stem = _slug(gml_path.stem)
    covered = s.run("""
        MATCH (ds:Dataset {source: $src})
        MATCH (other:Dataset)-[:COVERS]->(d:District) WHERE other <> ds AND d.id <> $stem
        MERGE (ds)-[:COVERS]->(d)
        RETURN count(DISTINCT d) AS n
    """, src=source, stem=stem).single()["n"]
    if covered:
        s.run("""
            MATCH (d:District {id: $stem}) WHERE NOT ()-[:LOCATED_IN]->(d)
            OPTIONAL MATCH (d)-[:PART_OF]->(c:City {id: $stem})
            DETACH DELETE d
            WITH c WHERE c IS NOT NULL AND NOT ()-[:PART_OF]->(c)
            DETACH DELETE c
        """, stem=stem)
        print(f"      dataset covers the city model's district(s) ({covered}); "
              f"file-named district/city removed", flush=True)
    with open(gml_path, "rb") as fh:
        head = fh.read(4000).decode("utf-8", "replace")
    meta = {}
    for key, label in (("license", "Nutzungsbedingungen"), ("attribution", "Quellenvermerk")):
        m = re.search(rf"<!--\s*{label}:\s*(.*?)\s*-->", head)
        if m:
            meta[key] = m.group(1)
    if meta:
        s.run("MATCH (ds:Dataset {source: $src}) SET ds += $meta", src=source, meta=meta)


def verify(s, layer: str, source: str | None) -> None:
    src_filter = "AND t.source = $source" if source else ""
    r = s.run(f"""
        MATCH (t:SolitaryVegetationObject) WHERE true {src_filter}
        RETURN count(t) AS n, count(t.location) AS located,
               sum(CASE WHEN EXISTS {{ (t)-[:HAS_LOD_SURFACE]->()-[:HAS_PROTOTYPE]->() }}
                        THEN 1 ELSE 0 END) AS with_proto
    """, source=source).single()
    protos = s.run("MATCH (p:GeometryPrototype) RETURN count(p) AS n").single()["n"]
    fc = s.run("CALL spatial.getFeatureCount($layer) YIELD count RETURN count",
               layer=layer).single()["count"]
    print(f"      trees total={r['n']:,} | located={r['located']:,} | "
          f"with prototype={r['with_proto']:,} | prototypes={protos:,} | "
          f"'{layer}' feature count={fc:,}", flush=True)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gml", help="Path to the street-tree CityGML (e.g. Hamburg "
                    "Strassenbaumkataster_20210901_Sommerbaeume.gml)")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Skip step 1; only run the prototype/locate/index/tidy post-passes")
    ap.add_argument("--relocate", action="store_true",
                    help="Recompute the position of every tree, not only unlocated ones")
    ap.add_argument("--footprint", type=float, default=1.0,
                    help="Minimum tree footprint in metres for the R-tree bbox (default 1.0)")
    ap.add_argument("--layer", default="features", help="Spatial layer name (default 'features')")
    ap.add_argument("--batch", type=int, default=5000, help="Post-pass batch size (default 5000)")
    ap.add_argument("--all-sources", action="store_true",
                    help="Locate/index ALL SolitaryVegetationObject, not just this file's source")
    args = ap.parse_args()

    gml_path = Path(args.gml)
    if not gml_path.exists():
        sys.exit(f"ERROR: GML not found: {gml_path}")
    source = gml_path.name

    driver = GraphDatabase.driver(
        NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications={"UNRECOGNIZED", "DEPRECATION"},
    )
    driver.verify_connectivity()

    if not args.skip_ingest:
        ingest_gml(driver, gml_path)
    else:
        print("[1/5] ingest skipped (--skip-ingest)", flush=True)

    with driver.session() as s:
        print("[2/5] Prototype geometry + appearance …", flush=True)
        t0 = time.perf_counter()
        protos, theme = read_prototypes(gml_path)
        print(f"      read {len(protos)} prototypes in {time.perf_counter() - t0:.0f}s", flush=True)
        write_prototypes(s, protos, theme, source, args.batch)
        link_prototypes(s, protos, source, args.batch)

        scope = None if args.all_sources else source
        print(f"[3/5] Locating trees (source={scope or '<all>'}) …", flush=True)
        locate_trees(s, scope, args.footprint, args.batch, args.relocate)

        print(f"[4/5] Indexing trees in '{args.layer}' …", flush=True)
        ensure_features_layer(s, args.layer)
        index_trees(s, args.layer, scope, args.batch)

        print("[5/5] Dataset metadata …", flush=True)
        tidy_dataset(s, gml_path, source)
        verify(s, args.layer, scope)

    driver.close()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
