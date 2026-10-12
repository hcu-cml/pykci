#!/usr/bin/env python3
"""Incorporate a CityGML 2.0 LoD3 building-facade dataset into an existing pykci graph.

This script is *facade-focused* and *provenance-aware*. It does two things:

  1. FIX (offline, no database).  A LoD3 export may carry schema / modelling defects
     that the pykci ingester and downstream 3D-Tiles/viewer code would silently trip
     over.  We detect and repair three classes of defect that occur in real ALKIS
     LoD3 exports (see LOD3_FACADE_INTEGRATION.md for the diagnosis), *without
     touching the original file* — the corrected copy is written next to it as
     ``<name>_fixed.gml``:

        (a) element-order violation of the ``AbstractBuildingType`` XSD sequence
            (a ``bldg:lodNSolid`` emitted *after* ``bldg:boundedBy``);
        (b) LoD-tag inconsistency — a building whose surfaces carry LoD3 features
            (``bldg:opening`` windows/doors, ``gml:interior`` wall holes) but whose
            geometry is still tagged ``lod2Solid`` / ``lod2MultiSurface``;
        (c) a ``CityModel`` ``gml:boundedBy`` envelope that does not enclose the
            actual geometry (recomputed from the data — never hard-coded).

  2. ENRICH (online).  Only the LoD3 *facade* elements are read — semantic boundary
     surfaces (Wall/Roof/Ground), their exterior + interior (window/door hole) rings,
     and their ``bldg:opening`` Windows/Doors.  Everything thematic (address, function,
     roofType, measuredHeight, gen:* attributes, external references) is deliberately
     ignored: those are authoritative ALKIS attributes that already live on the
     LoD2 ``Building`` and must not be duplicated or shadowed by this reconstructed
     source.

     The facade is attached to the *existing* authoritative ``Building`` anchor as an
     ADDITIVE, provenance-tagged overlay (trust class ``reconstructed``).  It never
     overwrites the authoritative LoD2 geometry: the LoD3 surfaces are new nodes,
     reached through an explicit, confidence-carrying ``HAS_LOD3_FACADE`` edge, and
     kept in their own spatial layer.  This mirrors the OSM ``ENRICHED_BY`` reference
     implementation (see the Provenance & Multi-Source Design Principle in GRAPH.md).

Examples
--------
    # Fix + enrich (Neo4j must be running); one Dataset present -> auto-detected
    py enrich_lod3_facades.py "input/citygml/hamburg/hamburg_citygml2_lod3_facades_holes_export.gml"

    # Just produce the repaired *_fixed.gml (no database needed)
    py enrich_lod3_facades.py path/to/lod3.gml --fix-only

    # Target a named Dataset, skip the XSD re-validation of the fixed file
    py enrich_lod3_facades.py path/to/lod3.gml --dataset hamburg_citygml2_lod2_2025 --no-validate
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree

# --------------------------------------------------------------------------- #
# Namespaces / qualified names
# --------------------------------------------------------------------------- #
CITYGML = "http://www.opengis.net/citygml/2.0"
BLDG = "http://www.opengis.net/citygml/building/2.0"
GML = "http://www.opengis.net/gml"
XLINK = "http://www.w3.org/1999/xlink"
XAL = "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"

GML_ID = f"{{{GML}}}id"
XLINK_HREF = f"{{{XLINK}}}href"
NS = {"cg": CITYGML, "bldg": BLDG, "gml": GML, "xlink": XLINK}

# --------------------------------------------------------------------------- #
# Provenance constants (see GRAPH.md -> Provenance & Multi-Source Design Principle).
# Everything this script writes lives under these namespaces so a reset touches
# ONLY our own additions and never the authoritative CityGML/ALKIS graph.
# --------------------------------------------------------------------------- #
SOURCE_TYPE = "reconstructed"          # trust class of the LoD3 facade models
METHOD = "lod3-facade-reconstruction"  # how the data was produced
METHOD_VERSION = "1.0"
ID_PREFIX = "lod3:"                    # source-prefixed id scheme -> clean, idempotent reset
MARKER_LABEL = "Lod3Facade"           # extra label on every node we create -> surgical reset
SPATIAL_LAYER = "lod3_features"       # own R-tree layer -> authoritative 'features' untouched
FACADE_EDGE = "HAS_LOD3_FACADE"       # explicit, confidence-carrying fusion edge

# Neo4j (same instance the rest of pykci uses)
NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")
BATCH_SIZE = 1000

# Facade vocabulary (CityGML building module)
BOUNDARY_TYPES = {
    "GroundSurface", "WallSurface", "RoofSurface", "ClosureSurface",
    "FloorSurface", "InteriorWallSurface", "CeilingSurface",
    "OuterFloorSurface", "OuterCeilingSurface",
}
OPENING_TYPES = ("Window", "Door")  # search order matches ingest_citygml.py

# Building sentinel scalars set on the anchor (namespaced -> never clobber ALKIS keys)
SENTINEL_KEYS = [
    "lod3_enriched", "lod3_surface_count", "lod3_opening_count", "lod3_window_count",
    "lod3_door_count", "lod3_hole_count", "lod3_source", "lod3_confidence",
    "lod3_method", "lod3_method_version", "lod3_source_type",
]

# Canonical child order of AbstractBuildingType (CityGML 2.0 building.xsd).  Used to
# repair defect (a).  Derived from the XSD at runtime when available; this is the
# fallback.  boundedBy sits BETWEEN the lod2* and lod3* geometry, which is exactly
# why a lod2Solid emitted after boundedBy is invalid and a lod3Solid there is legal.
_GEOM_ORDER_FALLBACK = [
    "lod1Solid", "lod1MultiSurface", "lod1TerrainIntersection", "lod1MultiCurve",
    "lod2Solid", "lod2MultiSurface", "lod2MultiCurve", "lod2TerrainIntersection",
    "outerBuildingInstallation", "interiorBuildingInstallation",
    "boundedBy",
    "lod3Solid", "lod3MultiSurface", "lod3MultiCurve", "lod3TerrainIntersection",
    "lod4Solid", "lod4MultiSurface", "lod4MultiCurve", "lod4TerrainIntersection",
    "interiorRoom", "consistsOfBuildingPart", "address",
]


# --------------------------------------------------------------------------- #
# Small XML helpers
# --------------------------------------------------------------------------- #
def _local(el) -> str:
    """Local (namespace-stripped) tag name of an element."""
    tag = el.tag
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def _parse_poslist(text, dim):
    """gml:posList text -> list of coordinate tuples (verbatim floats, for extents only)."""
    toks = text.split()
    pts = []
    for i in range(0, len(toks) - dim + 1, dim):
        try:
            pts.append(tuple(float(toks[i + k]) for k in range(dim)))
        except ValueError:
            continue
    return pts


def _iter_abstract_buildings(root):
    """Yield every bldg:Building and (recursively) bldg:BuildingPart element."""
    for el in root.iter():
        if _local(el) in ("Building", "BuildingPart"):
            yield el


# --------------------------------------------------------------------------- #
# 1. FIX — detect and repair the schema / modelling defects
# --------------------------------------------------------------------------- #
def _derive_geom_order(xsd_dir):
    """Read the AbstractBuildingType child sequence straight from building.xsd.

    Keeps the fixer honest: the *authoritative* element order comes from the schema,
    not from a literal in our code (falls back to _GEOM_ORDER_FALLBACK if unreadable)."""
    if not xsd_dir:
        return {n: i for i, n in enumerate(_GEOM_ORDER_FALLBACK)}
    xsd = Path(xsd_dir) / "building" / "2.0" / "building.xsd"
    try:
        tree = etree.parse(str(xsd))
        xs = "http://www.w3.org/2001/XMLSchema"
        ct = tree.find(f".//{{{xs}}}complexType[@name='AbstractBuildingType']")
        names = [e.get("name") for e in ct.iter(f"{{{xs}}}element") if e.get("name")]
        if names:
            return {n: i for i, n in enumerate(names)}
    except Exception:
        pass
    return {n: i for i, n in enumerate(_GEOM_ORDER_FALLBACK)}


def _is_lod3_building(b_el) -> bool:
    """A building is LoD3 if its facade carries LoD3-only features: openings or holes."""
    for el in b_el.iter():
        ln = _local(el)
        if ln == "opening":          # bldg:opening -> Window/Door
            return True
        if ln == "interior" and el.tag == f"{{{GML}}}interior":  # a wall/door/window hole
            return True
    return False


def diagnose_and_fix(tree):
    """Repair the three known defect classes in place. Returns (issues, fixes) log lists."""
    root = tree.getroot()
    xsd_dir = None  # order derived from fallback here; validate() re-checks against real XSD
    geom_rank = _derive_geom_order(xsd_dir)
    issues, fixes = [], []

    for b_el in _iter_abstract_buildings(root):
        bid = b_el.get(GML_ID, "<no-id>")
        lod3 = _is_lod3_building(b_el)

        # (b) LoD-tag inconsistency: retag lod2* geometry to lod3* on LoD3 buildings.
        if lod3:
            # building shell solid
            for child in list(b_el):
                ln = _local(child)
                if ln in ("lod2Solid", "lod1Solid") and child.tag != f"{{{BLDG}}}lod3Solid":
                    child.tag = f"{{{BLDG}}}lod3Solid"
                    issues.append(f"{bid}: shell solid tagged {ln} but building is LoD3")
                    fixes.append(f"{bid}: {ln} -> lod3Solid")
            # boundary-surface multi-surfaces
            for ms in b_el.iter():
                if _local(ms) in ("lod2MultiSurface", "lod1MultiSurface"):
                    old = _local(ms)
                    ms.tag = f"{{{BLDG}}}lod3MultiSurface"
                    fixes.append(f"{bid}: boundary {old} -> lod3MultiSurface")

        # (a) element-order violation: canonicalise the geometry block.  Non-geometry
        # children (creationDate, gen:*, function, ...) stay in place; only the
        # geometry-block elements are re-sorted into their schema slots.
        children = list(b_el)
        # Only bldg:-namespace children belong to the AbstractBuildingType geometry block.
        # (gml:boundedBy — the feature envelope — shares the local name "boundedBy" with
        # bldg:boundedBy but is an inherited _Feature element that must stay put.)
        geom_positions = [i for i, c in enumerate(children)
                          if _local(c) in geom_rank and c.tag == f"{{{BLDG}}}{_local(c)}"]
        if geom_positions:
            geom_children = [children[i] for i in geom_positions]
            ordered = sorted(
                enumerate(geom_children),
                key=lambda p: (geom_rank[_local(p[1])], p[0]),
            )
            new_order = [children[i] for i in range(len(children))]
            was_reordered = False
            for slot, (_, gchild) in zip(geom_positions, ordered):
                if new_order[slot] is not gchild:
                    was_reordered = True
                new_order[slot] = gchild
            if was_reordered:
                for c in children:
                    b_el.remove(c)
                for c in new_order:
                    b_el.append(c)
                issues.append(f"{bid}: geometry elements out of AbstractBuildingType order")
                fixes.append(f"{bid}: geometry block reordered to schema sequence")

    # (c) CityModel envelope: recompute from the real geometry extent.
    _fix_city_envelope(root, issues, fixes)
    return issues, fixes


def _fix_city_envelope(root, issues, fixes):
    minc = [float("inf")] * 3
    maxc = [float("-inf")] * 3
    seen = False
    for pl in root.iter(f"{{{GML}}}posList"):
        dim = int(pl.get("srsDimension", "3"))
        for pt in _parse_poslist(pl.text or "", dim):
            seen = True
            for k in range(min(3, len(pt))):
                minc[k] = min(minc[k], pt[k])
                maxc[k] = max(maxc[k], pt[k])
    if not seen:
        return
    env = root.find("gml:boundedBy/gml:Envelope", NS)
    if env is None:
        return
    lc = env.find("gml:lowerCorner", NS)
    uc = env.find("gml:upperCorner", NS)
    if lc is None or uc is None:
        return
    dim = int(env.get("srsDimension", "3"))
    d = min(dim, 3)
    new_lc = " ".join(f"{minc[k]:.3f}" for k in range(d))
    new_uc = " ".join(f"{maxc[k]:.3f}" for k in range(d))

    def _contains(corner_txt, lo):
        try:
            vals = [float(x) for x in (corner_txt or "").split()]
        except ValueError:
            return False
        if len(vals) < 2:
            return False
        # does the stated envelope actually bracket the data?
        return (vals[0] <= minc[0] + 1 and vals[1] <= minc[1] + 1) if lo else \
               (vals[0] >= maxc[0] - 1 and vals[1] >= maxc[1] - 1)

    stale = not (_contains(lc.text, True) and _contains(uc.text, False))
    if stale:
        issues.append("CityModel envelope does not enclose the geometry")
        fixes.append(f"CityModel envelope recomputed to [{new_lc}] .. [{new_uc}]")
        lc.text = new_lc
        uc.text = new_uc


# --------------------------------------------------------------------------- #
# Optional: re-validate the fixed file against the local CityGML 2.0 XSDs
# --------------------------------------------------------------------------- #
def validate_xsd(gml_path, xsd_dir):
    """Best-effort XSD validation. Returns (ok:bool|None, n_errors:int, note:str).

    ok=None means validation could not run (no schema / no network for GML 3.1.1)."""
    profile = Path(xsd_dir) / "profiles" / "base" / "2.0" / "CityGML.xsd"
    local_xal = (Path(xsd_dir) / "xAL" / "xAL.xsd").resolve()
    if not profile.exists():
        return None, 0, f"schema profile not found at {profile}"

    class _Resolver(etree.Resolver):
        def resolve(self, url, pubid, context):
            if "xAL.xsd" in url and "oasis" in url:      # OASIS xAL stub lacks AddressDetails
                return self.resolve_filename(str(local_xal), context)
            return None

    try:
        import socket
        socket.setdefaulttimeout(30)
        parser = etree.XMLParser(no_network=False)
        parser.resolvers.add(_Resolver())
        schema = etree.XMLSchema(etree.parse(str(profile), parser))
        ok = schema.validate(etree.parse(str(gml_path)))
        return bool(ok), len(schema.error_log), "" if ok else "see error_log"
    except Exception as exc:  # network down, lxml, etc. — never fatal
        return None, 0, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 2. ENRICH — parse the LoD3 facade into graph-ready batch rows
# --------------------------------------------------------------------------- #
def _pid(gml_id):
    """Source-prefixed node id (idempotent MERGE key; clean reset)."""
    return ID_PREFIX + gml_id


class _Batch:
    """Accumulates node/edge rows keyed by write target."""

    def __init__(self):
        self.d = defaultdict(list)

    def add(self, key, row):
        self.d[key].append(row)


def _collect_ring(ring_el, batch, source, ring_type, ring_index=None):
    """gml:LinearRing -> GeometryRing row + return its (prefixed) id. pos_list verbatim."""
    rid = ring_el.get(GML_ID) or f"ring_{id(ring_el)}"
    pos = ring_el.find("gml:posList", NS)
    if pos is None:
        return None, 0
    pos_list = (pos.text or "").strip()          # NEVER reformatted (lossless invariant)
    dim = int(pos.get("srsDimension", "3"))
    batch.add("rings", {
        "id": _pid(rid), "pos_list": pos_list, "srs_dimension": dim,
        "ring_type": ring_type, "source": source,
    })
    npts = len(_parse_poslist(pos_list, dim))
    return _pid(rid), npts


def _collect_polygon(poly_el, batch, source):
    """gml:Polygon -> GeometryPolygon + exterior/interior GeometryRing rows.
    Returns (polygon_id, n_interior_rings, [exterior_points])."""
    pid = poly_el.get(GML_ID) or f"poly_{id(poly_el)}"
    batch.add("polys", {"id": _pid(pid), "source": source})

    ext = poly_el.find("gml:exterior/gml:LinearRing", NS)
    ext_pts = []
    if ext is not None:
        rid, _ = _collect_ring(ext, batch, source, "exterior")
        if rid:
            batch.add("poly_ext", {"src": _pid(pid), "tgt": rid})
            pos = ext.find("gml:posList", NS)
            if pos is not None:
                ext_pts = _parse_poslist(pos.text or "", int(pos.get("srsDimension", "3")))

    n_holes = 0
    for k, inter in enumerate(poly_el.findall("gml:interior/gml:LinearRing", NS)):
        rid, _ = _collect_ring(inter, batch, source, "interior")
        if rid:
            batch.add("poly_int", {"src": _pid(pid), "tgt": rid, "ring_index": k})
            n_holes += 1
    return _pid(pid), n_holes, ext_pts


def _bbox_wkt(points):
    """2D closed POLYGON WKT from a point cloud (for the R-tree). None if degenerate."""
    xs = [p[0] for p in points if len(p) >= 2]
    ys = [p[1] for p in points if len(p) >= 2]
    if len(xs) < 3:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x0 == x1 or y0 == y1:  # a vertical wall projects to a line in 2D -> pad slightly
        x1 += 1e-6
        y1 += 1e-6
    return (f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, "
            f"{x0} {y1}, {x0} {y0}))")


def _collect_opening(op_el, surf_id, order, batch, source):
    """bldg:opening -> Opening + its lod3MultiSurface geometry. Returns opening_type."""
    concrete = next((c for c in op_el if _local(c) in OPENING_TYPES), None)
    if concrete is None:
        return None
    op_type = _local(concrete)
    oid = concrete.get(GML_ID) or f"opening_{id(concrete)}"
    batch.add("openings", {"id": _pid(oid), "opening_type": op_type,
                           "source": source, "source_type": SOURCE_TYPE})
    batch.add("surf_opening", {"src": surf_id, "tgt": _pid(oid), "order": order})

    # first present lodN MultiSurface (LoD3 for these openings)
    for lod in ("3", "4", "2"):
        ms = concrete.find(f"bldg:lod{lod}MultiSurface/gml:MultiSurface", NS)
        if ms is None:
            continue
        msid = ms.get(GML_ID) or f"ms_{id(ms)}"
        batch.add("multisurfaces", {"id": _pid(msid), "lod": lod, "source": source})
        batch.add("opening_ms", {"src": _pid(oid), "tgt": _pid(msid), "lod": lod})
        for m_order, poly in enumerate(ms.findall("gml:surfaceMember/gml:Polygon", NS)):
            poly_id, _, _ = _collect_polygon(poly, batch, source)
            batch.add("ms_member", {"src": _pid(msid), "tgt": poly_id,
                                    "order": m_order, "via_xlink": False, "orientation": "+"})
        break
    return op_type


def building_ids(tree):
    """Every AbstractBuilding gml:id in the file (candidate anchors)."""
    return [b.get(GML_ID) for b in _iter_abstract_buildings(tree.getroot()) if b.get(GML_ID)]


def parse_facades(tree, source, allowed=None):
    """Walk the (fixed) tree and build facade batch rows for anchors in `allowed`.

    `allowed=None` parses every building (used for --fix-only counts). When a set is
    given, facades of buildings absent from the graph are skipped entirely — this is
    an *enrichment* pass, so we never materialise geometry that has no anchor to
    enrich (the caller reports those ids as not-found instead).

    Returns (batch, per_building) where per_building maps building gml:id -> stats."""
    root = tree.getroot()
    batch = _Batch()
    per_building = {}

    for b_el in _iter_abstract_buildings(root):
        # BuildingParts attach to their own id; both are valid anchors in the graph.
        bid = b_el.get(GML_ID)
        if not bid or (allowed is not None and bid not in allowed):
            continue
        stats = Counter()
        surf_ids = []

        for order, bb in enumerate(b_el.findall("bldg:boundedBy", NS)):
            surf = next((c for c in bb if _local(c) in BOUNDARY_TYPES), None)
            if surf is None:
                continue
            stype = _local(surf)
            sid = surf.get(GML_ID) or f"bsurf_{bid}_{order}_{stype.lower()}"
            spid = _pid(sid)

            # detect LoD from the surface's own lodN MultiSurface (post-fix: "3")
            lod = "3"
            ms_el = None
            for cand in ("3", "4", "2"):
                ms_el = surf.find(f"bldg:lod{cand}MultiSurface/gml:MultiSurface", NS)
                if ms_el is not None:
                    lod = cand
                    break

            srow = {"id": spid, "surface_type": stype, "lod": lod, "source": source,
                    "source_type": SOURCE_TYPE}
            if ms_el is not None and ms_el.get(GML_ID):
                srow["multi_surface_id"] = ms_el.get(GML_ID)

            # polygons -> HAS_POLYGON (flattened, matching ingest_citygml.py)
            all_pts = []
            if ms_el is not None:
                for p_order, poly in enumerate(ms_el.findall("gml:surfaceMember/gml:Polygon", NS)):
                    poly_id, n_holes, ext_pts = _collect_polygon(poly, batch, source)
                    all_pts.extend(ext_pts)
                    stats["holes"] += n_holes
                    batch.add("surf_poly", {"src": spid, "tgt": poly_id, "order": p_order,
                                            "via_xlink": False, "orientation": "+"})
            wkt = _bbox_wkt(all_pts)
            if wkt:
                srow["bbox_wkt"] = wkt
            batch.add("surfaces", srow)
            surf_ids.append((spid, order))

            # openings (Windows / Doors)
            for op_order, op in enumerate(surf.findall("bldg:opening", NS)):
                op_type = _collect_opening(op, spid, op_order, batch, source)
                if op_type == "Window":
                    stats["windows"] += 1
                elif op_type == "Door":
                    stats["doors"] += 1

            stats[f"surf_{stype}"] += 1
            stats["surfaces"] += 1

        # explicit, confidence-carrying attachment edge to the authoritative anchor
        for spid, order in surf_ids:
            batch.add("facade_edge", {"bid": bid, "sid": spid, "order": order})

        per_building[bid] = dict(stats)

    return batch, per_building


# --------------------------------------------------------------------------- #
# Neo4j write layer
# --------------------------------------------------------------------------- #
def _flush(sess, query, rows):
    for i in range(0, len(rows), BATCH_SIZE):
        sess.run(query, rows=rows[i:i + BATCH_SIZE])


def ensure_indexes(sess):
    for label in ("BoundarySurface", "Opening", "GeometryPolygon", "GeometryRing",
                  "GeometryMultiSurface", MARKER_LABEL, "Entity", "Building"):
        idx = f"{label.lower()}_id_idx"
        sess.run(f"CREATE INDEX {idx} IF NOT EXISTS FOR (n:{label}) ON (n.id)")


def reset_lod3(sess, source):
    """Idempotency: strip ONLY this source's additions before re-running (never the
    authoritative graph). Mirrors reset_osm()."""
    try:
        sess.run("CALL spatial.removeLayer($l)", l=SPATIAL_LAYER)
    except Exception:
        pass
    removes = ", ".join(f"b.{k}" for k in SENTINEL_KEYS)
    sess.run(f"MATCH (b:Building) WHERE b.lod3_source = $src REMOVE {removes}", src=source)
    sess.run(f"MATCH (n:{MARKER_LABEL}) WHERE n.source = $src DETACH DELETE n", src=source)


def resolve_dataset(sess, name):
    rows = [dict(r) for r in sess.run("MATCH (d:Dataset) RETURN d.id AS id, d.name AS name")]
    if not rows:
        sys.exit("No Dataset in the graph — run ingest_citygml.py on the LoD2 data first.")
    if name:
        for r in rows:
            if name in (r["id"], r["name"]):
                return r
        sys.exit(f"Dataset '{name}' not found. Present: {[r['name'] for r in rows]}")
    if len(rows) > 1:
        sys.exit(f"Multiple datasets present, pass --dataset. Present: {[r['name'] for r in rows]}")
    return rows[0]


def find_present(sess, ids):
    """Which candidate building ids already exist as :Building anchors."""
    present = set()
    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ids[i:i + BATCH_SIZE]
        present.update(r["id"] for r in sess.run(
            "UNWIND $ids AS id MATCH (b:Building {id: id}) RETURN b.id AS id", ids=chunk))
    return present


def write_facades(sess, batch, per_building, source, confidence):
    """Write facade nodes/edges + sentinels. All per_building anchors are present."""
    E = ":Entity"
    M = f":{MARKER_LABEL}"

    # ---- nodes (MERGE on id; add Entity + Lod3Facade; conventions per ingest_citygml.py)
    _flush(sess, f"UNWIND $rows AS row MERGE (n:BoundarySurface {{id: row.id}}) "
                 f"SET n += row, n{E}{M}", batch.d["surfaces"])
    _flush(sess, f"UNWIND $rows AS row MERGE (n:Opening {{id: row.id}}) "
                 f"SET n += row, n{E}{M}", batch.d["openings"])
    _flush(sess, f"UNWIND $rows AS row MERGE (n:GeometryMultiSurface {{id: row.id}}) "
                 f"SET n += row, n{E}{M}", batch.d["multisurfaces"])
    # GeometryPolygon: props limited to id+source (matches ingest_citygml.py)
    _flush(sess, f"UNWIND $rows AS row MERGE (n:GeometryPolygon {{id: row.id}}) "
                 f"SET n.source = row.source, n{E}{M}", batch.d["polys"])
    # GeometryRing: no Entity label by convention (never a generic edge endpoint)
    _flush(sess, f"UNWIND $rows AS row MERGE (n:GeometryRing {{id: row.id}}) "
                 f"SET n += row, n{M}", batch.d["rings"])

    # ---- geometry edges (identical to native CityGML geometry chain)
    _flush(sess, "UNWIND $rows AS row MATCH (s:BoundarySurface {id: row.src}), "
                 "(p:GeometryPolygon {id: row.tgt}) MERGE (s)-[r:HAS_POLYGON]->(p) "
                 "SET r.order = row.order, r.via_xlink = row.via_xlink, "
                 "r.orientation = row.orientation", batch.d["surf_poly"])
    _flush(sess, "UNWIND $rows AS row MATCH (s:BoundarySurface {id: row.src}), "
                 "(o:Opening {id: row.tgt}) MERGE (s)-[r:HAS_OPENING]->(o) "
                 "SET r.order = row.order", batch.d["surf_opening"])
    _flush(sess, "UNWIND $rows AS row MATCH (o:Opening {id: row.src}), "
                 "(m:GeometryMultiSurface {id: row.tgt}) MERGE (o)-[r:HAS_LOD_SURFACE]->(m) "
                 "SET r.lod = row.lod", batch.d["opening_ms"])
    _flush(sess, "UNWIND $rows AS row MATCH (m:GeometryMultiSurface {id: row.src}), "
                 "(p:GeometryPolygon {id: row.tgt}) MERGE (m)-[r:HAS_SURFACE_MEMBER]->(p) "
                 "SET r.order = row.order, r.via_xlink = row.via_xlink, "
                 "r.orientation = row.orientation", batch.d["ms_member"])
    _flush(sess, "UNWIND $rows AS row MATCH (p:GeometryPolygon {id: row.src}), "
                 "(r2:GeometryRing {id: row.tgt}) MERGE (p)-[:HAS_EXTERIOR_RING]->(r2)",
           batch.d["poly_ext"])
    _flush(sess, "UNWIND $rows AS row MATCH (p:GeometryPolygon {id: row.src}), "
                 "(r2:GeometryRing {id: row.tgt}) MERGE (p)-[r:HAS_INTERIOR_RING]->(r2) "
                 "SET r.ring_index = row.ring_index", batch.d["poly_int"])

    # ---- the explicit, provenance + confidence carrying fusion edge to the anchor.
    # MATCH (not MERGE) the Building: we enrich only anchors that already exist.
    facade_props = {
        "source": source, "source_type": SOURCE_TYPE, "method": METHOD,
        "method_version": METHOD_VERSION, "confidence": confidence, "lod": "3",
    }
    for row in batch.d["facade_edge"]:
        row.update(facade_props)
    _flush(sess, "UNWIND $rows AS row MATCH (b:Building {id: row.bid}), "
                 "(s:BoundarySurface {id: row.sid}) "
                 f"MERGE (b)-[r:{FACADE_EDGE} {{order: row.order}}]->(s) "
                 "SET r.source = row.source, r.source_type = row.source_type, "
                 "r.method = row.method, r.method_version = row.method_version, "
                 "r.confidence = row.confidence, r.lod = row.lod", batch.d["facade_edge"])

    # ---- sentinel scalars on the anchors (namespaced; never overwrite ALKIS keys)
    sent_rows = []
    for bid, st in per_building.items():
        sent_rows.append({"id": bid, "props": {
            "lod3_enriched": True,
            "lod3_surface_count": st.get("surfaces", 0),
            "lod3_opening_count": st.get("windows", 0) + st.get("doors", 0),
            "lod3_window_count": st.get("windows", 0),
            "lod3_door_count": st.get("doors", 0),
            "lod3_hole_count": st.get("holes", 0),
            "lod3_source": source, "lod3_confidence": confidence,
            "lod3_method": METHOD, "lod3_method_version": METHOD_VERSION,
            "lod3_source_type": SOURCE_TYPE,
        }})
    _flush(sess, "UNWIND $rows AS row MATCH (b:Building {id: row.id}) SET b += row.props",
           sent_rows)


def index_spatial(sess):
    """Register LoD3 surfaces in their OWN R-tree layer (best effort)."""
    try:
        existing = {r["name"] for r in sess.run("CALL spatial.layers() YIELD name RETURN name")}
        if SPATIAL_LAYER not in existing:
            sess.run("CALL spatial.addWKTLayer($l, 'bbox_wkt')", l=SPATIAL_LAYER)
        return sess.run(
            f"MATCH (n:{MARKER_LABEL}:BoundarySurface) WHERE n.bbox_wkt IS NOT NULL "
            "WITH collect(n) AS ns CALL spatial.addNodes($l, ns) YIELD count RETURN count AS c",
            l=SPATIAL_LAYER).single()["c"]
    except Exception as exc:
        print(f"  [spatial] skipped ({type(exc).__name__}: {exc})")
        return 0


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def write_summary(source, stem, dataset, issues, fixes, validation, batch,
                  per_building, present, missing, spatial_count, confidence):
    n_windows = sum(s.get("windows", 0) for s in per_building.values())
    n_doors = sum(s.get("doors", 0) for s in per_building.values())
    n_holes = sum(s.get("holes", 0) for s in per_building.values())
    summary = {
        "source": source, "source_type": SOURCE_TYPE, "method": METHOD,
        "method_version": METHOD_VERSION, "confidence": confidence,
        "dataset": dataset["name"],
        "fix": {
            "issue_count": len(issues),
            "issues_by_type": dict(Counter(i.split(": ", 1)[-1] for i in issues)),
            "fixes_by_type": dict(Counter(f.split(": ", 1)[-1] for f in fixes)),
            "sample_fixes": fixes[:5],
            "xsd_validation_after_fix": validation,
        },
        "facade_elements": {
            "buildings_in_lod3_file": len(present) + len(missing),
            "boundary_surfaces_written": len(batch.d["surfaces"]),
            "openings_written": len(batch.d["openings"]),
            "windows": n_windows, "doors": n_doors,
            "wall_holes": n_holes,
            "geometry_polygons_written": len(batch.d["polys"]),
            "geometry_rings_written": len(batch.d["rings"]),
        },
        "enrichment": {
            "buildings_matched": len(present),
            "buildings_not_found": len(missing),
            "coverage_rate": round(len(present) / (len(present) + len(missing)), 3)
                              if (present or missing) else 0.0,
            "not_found_ids": missing[:50],
            "spatial_indexed_surfaces": spatial_count,
        },
        "note": ("LoD3 facades are attached to authoritative Building anchors via "
                 f"{FACADE_EDGE} (trust class '{SOURCE_TYPE}'). Authoritative LoD2 "
                 "geometry and ALKIS attributes are untouched; absence of lod3_enriched "
                 "means no LoD3 coverage (never a negative)."),
    }
    out = Path("output") / f"{stem}_lod3_summary.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("# LoD3 facade enrichment summary\n\n```json\n"
                   + json.dumps(summary, indent=2, ensure_ascii=False) + "\n```\n",
                   encoding="utf-8")
    return summary, out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Fix a CityGML 2.0 LoD3 facade export and enrich the pykci graph with it.")
    ap.add_argument("gml", help="Path to the LoD3 CityGML file")
    ap.add_argument("--dataset", help="Target Dataset name/id (auto if only one present)")
    ap.add_argument("--confidence", type=float, default=0.9,
                    help="Confidence for the reconstructed facades (edge + sentinel; default 0.9)")
    ap.add_argument("--xsd-dir", default="citygml-model/xsd_citygml2",
                    help="CityGML 2.0 XSD dir for the post-fix re-validation")
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip the XSD re-validation of the fixed file")
    ap.add_argument("--fix-only", action="store_true",
                    help="Only write <name>_fixed.gml; do not touch Neo4j")
    args = ap.parse_args()

    src_path = Path(args.gml)
    if not src_path.exists():
        sys.exit(f"File not found: {src_path}")

    # --- parse once, diagnose + fix in memory ---
    tree = etree.parse(str(src_path))
    issues, fixes = diagnose_and_fix(tree)

    # --- write the corrected copy alongside the original (only if anything changed) ---
    if issues:
        fixed_path = src_path.with_name(f"{src_path.stem}_fixed{src_path.suffix}")
        tree.write(str(fixed_path), xml_declaration=True, encoding="UTF-8")
        effective = fixed_path
        print(f"Found {len(issues)} issue(s); wrote fixed file: {fixed_path}")
        for f in fixes:
            print(f"  fix: {f}")
    else:
        effective = src_path
        print("No schema/modelling issues detected; using the original file.")

    # --- optional re-validation ---
    validation = {"ran": False}
    if not args.no_validate and issues:
        ok, n_err, note = validate_xsd(effective, args.xsd_dir)
        validation = {"ran": ok is not None, "valid": ok, "errors": n_err, "note": note}
        print(f"XSD re-validation of fixed file: "
              + ("VALID" if ok else "INVALID" if ok is False else f"skipped ({note})"))

    source = f"lod3:{src_path.stem}"
    candidates = building_ids(tree)

    if args.fix_only:
        batch, per_building = parse_facades(tree, source)  # all buildings, for counts
        print(f"Facade inventory: {len(per_building)} building(s), "
              f"{len(batch.d['surfaces'])} boundary surfaces, "
              f"{len(batch.d['openings'])} openings, {len(batch.d['rings'])} rings.")
        print("--fix-only: done (database not touched).")
        return

    # --- enrich the graph (only buildings that already have an anchor) ---
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        NEO4J_URI, auth=NEO4J_AUTH,
        notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
    driver.verify_connectivity()
    with driver.session(database="neo4j") as sess:
        dataset = resolve_dataset(sess, args.dataset)
        ensure_indexes(sess)
        present = find_present(sess, candidates)
        missing = [i for i in candidates if i not in present]
        batch, per_building = parse_facades(tree, source, allowed=present)
        print(f"Parsed facades for {len(per_building)} anchored building(s): "
              f"{len(batch.d['surfaces'])} boundary surfaces, "
              f"{len(batch.d['openings'])} openings, {len(batch.d['rings'])} rings "
              f"({len(missing)} building(s) not in graph — skipped).")
        reset_lod3(sess, source)                       # idempotent re-run
        write_facades(sess, batch, per_building, source, args.confidence)
        spatial_count = index_spatial(sess)
    driver.close()

    summary, out = write_summary(source, src_path.stem, dataset, issues, fixes, validation,
                                 batch, per_building, present, missing, spatial_count,
                                 args.confidence)
    print(f"\nEnriched {len(present)}/{len(per_building)} buildings "
          f"(not found: {len(missing)}). Summary: {out}")
    print(json.dumps(summary["enrichment"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
