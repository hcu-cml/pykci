"""
Integrate LoD3 facade openings (windows/doors) from the SVI2LoD3 result folders
into the Neo4j graph, matched by CityGML building ID.

Source: a Results folder with per-building subfolders (e.g. gpt-B1, qwen-B1, ...),
each containing a ready-made `LOD3-agent.gml` — a full CityGML 2.0 building whose
WallSurfaces already carry `bldg:opening` → Window/Door with real 3D geometry.

The WallSurface gml:ids in these files match the boundary-surface ids already in
the graph (both build on the same LoD2 geometry), so openings are attached to the
EXISTING surfaces via the canonical model that `export_citygml.py` reads:

    (BoundarySurface)-[:HAS_OPENING {order}]->(Opening {opening_type})
      -[:HAS_LOD_SURFACE {lod:'3'}]->(GeometryMultiSurface)
      -[:HAS_SURFACE_MEMBER {order}]->(GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(GeometryRing {pos_list})   (+ HAS_INTERIOR_RING)

Walls keep their original LoD2 geometry (matches hamburg_lod3_facades.gml, where
walls stay lod2 and only the openings are lod3). Idempotent (MERGE).

Only ONE model is integrated per run (--model gpt|qwen); if the same building has
several result folders for that model, the one with the most openings wins.

Usage:
    python enrich_lod3_openings.py [--results-dir PATH] [--model gpt|qwen]
                                      [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from lxml import etree
from neo4j import GraphDatabase

NEO4J_URL      = os.getenv("NEO4J_URL",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

# LOD3-agent.gml uses CityGML 2.0
BLDG_NS = "http://www.opengis.net/citygml/building/2.0"
GML_NS  = "http://www.opengis.net/gml"

DEFAULT_RESULTS_DIR = Path(
    "/home/luarzou/Documents/lod3pro/citydb-SVI2LoD3-ai/"
    "Results-exp_old_paper_3DGeo"
)

SURFACE_TAGS = {f"{{{BLDG_NS}}}WallSurface",
                f"{{{BLDG_NS}}}RoofSurface",
                f"{{{BLDG_NS}}}GroundSurface",
                f"{{{BLDG_NS}}}ClosureSurface"}


# ---------------------------------------------------------------------------
# Parse one LOD3-agent.gml → building id + list of openings with geometry
# ---------------------------------------------------------------------------

def _ring(el):
    """Return (ring_id, pos_list) for a gml:LinearRing element, or None."""
    lr = el.find(f"{{{GML_NS}}}LinearRing")
    if lr is None:
        return None
    pos = lr.find(f"{{{GML_NS}}}posList")
    if pos is None or not (pos.text and pos.text.strip()):
        return None
    return lr.get(f"{{{GML_NS}}}id", ""), pos.text.strip()


def _polygons_of(multisurface_prop) -> tuple[str, list[dict]]:
    """From a bldg:lod3MultiSurface property element, return (ms_id, [polys])."""
    ms = multisurface_prop.find(f"{{{GML_NS}}}MultiSurface")
    if ms is None:
        return "", []
    ms_id = ms.get(f"{{{GML_NS}}}id", "")
    polys = []
    for order, sm in enumerate(ms.findall(f"{{{GML_NS}}}surfaceMember")):
        poly = sm.find(f"{{{GML_NS}}}Polygon")
        if poly is None:
            continue
        ext_el = poly.find(f"{{{GML_NS}}}exterior")
        ext = _ring(ext_el) if ext_el is not None else None
        if ext is None:
            continue
        interiors = []
        for i_idx, int_el in enumerate(poly.findall(f"{{{GML_NS}}}interior")):
            r = _ring(int_el)
            if r:
                interiors.append({"id": r[0], "pos": r[1], "idx": i_idx})
        polys.append({
            "id":       poly.get(f"{{{GML_NS}}}id", ""),
            "order":    order,
            "ext_id":   ext[0],
            "ext_pos":  ext[1],
            "interiors": interiors,
        })
    return ms_id, polys


def parse_lod3_agent(gml_path: Path) -> dict[str, Any]:
    tree = etree.parse(str(gml_path), parser=etree.XMLParser(huge_tree=True))
    root = tree.getroot()

    bldg = root.find(f".//{{{BLDG_NS}}}Building")
    if bldg is None:
        raise ValueError("no bldg:Building")
    building_id = bldg.get(f"{{{GML_NS}}}id", "")

    openings: list[dict] = []
    # bldg:opening is a direct child of a boundary surface element
    for op_prop in bldg.iter(f"{{{BLDG_NS}}}opening"):
        surf = op_prop.getparent()
        if surf is None or surf.tag not in SURFACE_TAGS:
            continue
        surface_id = surf.get(f"{{{GML_NS}}}id", "")
        if not surface_id:
            continue
        for feat in op_prop:
            tag = etree.QName(feat).localname
            if tag not in ("Window", "Door"):
                continue
            ms_prop = feat.find(f"{{{BLDG_NS}}}lod3MultiSurface")
            if ms_prop is None:
                continue
            ms_id, polys = _polygons_of(ms_prop)
            if not polys:
                continue
            desc_el = feat.find(f"{{{GML_NS}}}description")
            openings.append({
                "surface_id":      surface_id,
                "id":              feat.get(f"{{{GML_NS}}}id", ""),
                "opening_type":    tag,
                "gml_description": (desc_el.text.strip()
                                    if desc_el is not None and desc_el.text else ""),
                "ms_id":           ms_id,
                "polygons":        polys,
            })
    n_win  = sum(1 for o in openings if o["opening_type"] == "Window")
    n_door = sum(1 for o in openings if o["opening_type"] == "Door")
    return {"building_id": building_id, "openings": openings,
            "n_windows": n_win, "n_doors": n_door}


# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------

WRITE_QUERY = """
MATCH (b:Building {id: $building_id})
UNWIND $openings AS op
  MATCH (s:BoundarySurface {id: op.surface_id})<-[:HAS_BOUNDARY]-(b)
  MERGE (o:Opening {id: op.id})
    SET o:Entity, o.opening_type = op.opening_type,
        o.gml_description = op.gml_description, o.source_model = $model
  MERGE (s)-[ho:HAS_OPENING]->(o)
    SET ho.order = op.order
  MERGE (ms:GeometryMultiSurface {id: op.ms_id})
    SET ms:Entity
  MERGE (o)-[hls:HAS_LOD_SURFACE]->(ms)
    SET hls.lod = '3'
  WITH o, ms, op
  UNWIND op.polygons AS pg
    MERGE (poly:GeometryPolygon {id: pg.id})
      SET poly:Entity
    MERGE (ms)-[sm:HAS_SURFACE_MEMBER]->(poly)
      SET sm.order = pg.order
    MERGE (ext:GeometryRing {id: pg.ext_id})
      SET ext.pos_list = pg.ext_pos, ext.srs_dimension = 3
    MERGE (poly)-[:HAS_EXTERIOR_RING]->(ext)
    FOREACH (ir IN pg.interiors |
      MERGE (itr:GeometryRing {id: ir.id})
        SET itr.pos_list = ir.pos, itr.srs_dimension = 3
      MERGE (poly)-[hir:HAS_INTERIOR_RING]->(itr)
        SET hir.ring_index = ir.idx
    )
RETURN count(DISTINCT o) AS openings_written
"""

SUMMARY_QUERY = """
MATCH (b:Building {id: $building_id})
SET b.has_lod3_data = true,
    b.lod3_windows  = $n_windows,
    b.lod3_doors    = $n_doors,
    b.lod3_source   = $model
RETURN b.id AS id
"""


def building_exists(driver, bid: str) -> bool:
    with driver.session() as s:
        return s.run("MATCH (b:Building {id:$id}) RETURN b.id LIMIT 1",
                     id=bid).single() is not None


def existing_surface_ids(driver, bid: str, sids: set[str]) -> set[str]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (b:Building {id:$id})-[:HAS_BOUNDARY]->(x:BoundarySurface) "
            "WHERE x.id IN $sids RETURN x.id AS id", id=bid, sids=list(sids))
        return {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Integrate LoD3 openings into Neo4j by CityGML id")
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    ap.add_argument("--model", choices=["gpt", "qwen"], default="gpt")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.results_dir)
    subdirs = sorted(d for d in root.glob(f"{args.model}-*") if d.is_dir())
    if not subdirs:
        print(f"[error] no '{args.model}-*' subfolders in {root}")
        sys.exit(1)

    # Parse each LOD3-agent.gml, group by building id, keep richest per building
    per_building: dict[str, dict] = {}
    dropped_dupes = []
    for d in subdirs:
        gml = d / "LOD3-agent.gml"
        if not gml.exists():
            print(f"  [skip] {d.name} — no LOD3-agent.gml")
            continue
        try:
            data = parse_lod3_agent(gml)
        except Exception as exc:
            print(f"  [skip] {d.name} — parse error: {exc}")
            continue
        data["folder"] = d.name
        bid = data["building_id"]
        n = len(data["openings"])
        if bid in per_building:
            prev = per_building[bid]
            # keep the folder with more openings; tie → lexicographically first
            if (n, prev["folder"]) > (len(prev["openings"]), d.name):
                dropped_dupes.append(f"{prev['folder']} (kept {d.name})")
                per_building[bid] = data
            else:
                dropped_dupes.append(f"{d.name} (kept {prev['folder']})")
        else:
            per_building[bid] = data

    print(f"Model '{args.model}': {len(subdirs)} folders → "
          f"{len(per_building)} unique buildings")
    if dropped_dupes:
        print(f"  duplicate results dropped: {', '.join(dropped_dupes)}")

    driver = None if args.dry_run else GraphDatabase.driver(
        NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD))

    stats = {"enriched": 0, "openings": 0, "windows": 0, "doors": 0,
             "skip_no_bldg": 0, "skip_no_surface": 0}
    t0 = time.perf_counter()

    for bid, data in sorted(per_building.items()):
        ops = data["openings"]
        folder = data["folder"]

        if not args.dry_run and not building_exists(driver, bid):
            print(f"  [skip] {bid} ({folder}) — building not in graph")
            stats["skip_no_bldg"] += 1
            continue

        # Verify the target wall surfaces exist on this building
        want_sids = {o["surface_id"] for o in ops}
        if args.dry_run:
            have = want_sids
        else:
            have = existing_surface_ids(driver, bid, want_sids)
        missing = want_sids - have
        ops_ok = [o for o in ops if o["surface_id"] in have]
        if missing:
            print(f"  [warn] {bid} ({folder}) — {len(missing)} surface(s) not on "
                  f"building, {len(ops)-len(ops_ok)} opening(s) skipped: "
                  f"{sorted(missing)}")
            stats["skip_no_surface"] += len(ops) - len(ops_ok)
        if not ops_ok:
            continue

        nw = sum(1 for o in ops_ok if o["opening_type"] == "Window")
        nd = sum(1 for o in ops_ok if o["opening_type"] == "Door")
        print(f"  [{folder:>10}] {bid}  →  {nw} windows, {nd} doors  "
              f"on {len(have)} wall(s)")

        if not args.dry_run:
            with driver.session() as s:
                s.run(WRITE_QUERY, building_id=bid, openings=ops_ok,
                      model=args.model)
                s.run(SUMMARY_QUERY, building_id=bid,
                      n_windows=nw, n_doors=nd, model=args.model)

        stats["enriched"] += 1
        stats["openings"] += len(ops_ok)
        stats["windows"]  += nw
        stats["doors"]    += nd

    if driver:
        driver.close()

    print("\n" + "=" * 60)
    print(f"Model integrated       : {args.model}")
    print(f"Buildings enriched     : {stats['enriched']}")
    print(f"Total windows          : {stats['windows']}")
    print(f"Total doors            : {stats['doors']}")
    print(f"Total openings written : {stats['openings']}")
    print(f"Skipped (no building)  : {stats['skip_no_bldg']}")
    print(f"Openings w/o surface   : {stats['skip_no_surface']}")
    print(f"Elapsed                : {time.perf_counter()-t0:.1f}s")
    if args.dry_run:
        print("[DRY RUN — nothing written]")


if __name__ == "__main__":
    main()
