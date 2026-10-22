"""
export_citygml.py — reconstruct CityGML 2.0 from the Neo4j graph.
Optimized for large graphs: batched queries + streaming XML via lxml.xmlfile.

Lossless round-trip of all data written by ingest_citygml.py:
  Buildings
  ─────────
  - Dataset CRS / srsName / srsDimension / bounding-box envelope
  - Building gml:id, creationDate, externalReference
  - All gen:stringAttribute values with original CamelCase names (via _gen_attrs_map)
  - bldg:function, bldg:roofType, bldg:measuredHeight (with uom), storeysAboveGround
  - bldg:lod{N}Solid → gml:Solid (original gml:id) with ordered xlink:href surface members
  - bldg:boundedBy surfaces in original document order (HAS_BOUNDARY.order)
  - bldg:lod{N}TerrainIntersection curves
  - Optional bbox_wkt filter via Neo4j Spatial R-tree

  All other CityGML 2.0 modules (CityObject nodes)
  ──────────────────────────────────────────────────
  - Transportation: Road, Railway, Track, Square, TransportationComplex
  - Vegetation: SolitaryVegetationObject, PlantCover
  - WaterBody (with wtr:boundedBy surfaces)
  - LandUse
  - CityFurniture
  - Relief: ReliefFeature (geometry limited; see note below)
  - Bridge (with brid:boundedBy surfaces)
  - Tunnel (with tun:boundedBy surfaces)
  - CityObjectGroup

  For each CityObject:
  - gml:id, creationDate, bbox envelope
  - All gen:*Attribute values with original CamelCase names
  - Module-specific scalar attributes (class, function, usage, species, height, …)
  - lod*Solid / lod*MultiSurface / lod*Geometry geometry with inline polygon rings
  - Module boundary surfaces (wtr/brid/tun) with inline polygon rings

  Note on ReliefFeature geometry: TIN triangle geometry is reconstructed via
  GeometryMultiSurface → GeometryPolygon chains. MassPoint/Breakline line strings
  are not reconnected to their parent feature (no relationship stored during ingest);
  those are omitted from the ReliefFeature export.

Output: output/citygml/<dataset-stem>_export.gml
"""

import argparse
import json
import sys
import uuid
from pathlib import Path


from lxml import etree
from neo4j import GraphDatabase

from pykci_runtime import neo4j_auth, neo4j_uri, progress

NEO4J_URI = neo4j_uri()
NEO4J_USER, NEO4J_PASSWORD = neo4j_auth()

# ---------------------------------------------------------------------------
# Namespace declarations
# ---------------------------------------------------------------------------

NSMAP = {
    None:    "http://www.opengis.net/citygml/2.0",
    "xAL":   "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0",
    "gml":   "http://www.opengis.net/gml",
    "wtr":   "http://www.opengis.net/citygml/waterbody/2.0",
    "app":   "http://www.opengis.net/citygml/appearance/2.0",
    "veg":   "http://www.opengis.net/citygml/vegetation/2.0",
    "dem":   "http://www.opengis.net/citygml/relief/2.0",
    "tran":  "http://www.opengis.net/citygml/transportation/2.0",
    "bldg":  "http://www.opengis.net/citygml/building/2.0",
    "grp":   "http://www.opengis.net/citygml/cityobjectgroup/2.0",
    "tun":   "http://www.opengis.net/citygml/tunnel/2.0",
    "frn":   "http://www.opengis.net/citygml/cityfurniture/2.0",
    "gen":   "http://www.opengis.net/citygml/generics/2.0",
    "brid":  "http://www.opengis.net/citygml/bridge/2.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "luse":  "http://www.opengis.net/citygml/landuse/2.0",
    "xsi":   "http://www.w3.org/2001/XMLSchema-instance",
}

GML   = "{http://www.opengis.net/gml}"
BLDG  = "{http://www.opengis.net/citygml/building/2.0}"
GEN   = "{http://www.opengis.net/citygml/generics/2.0}"
CG    = "{http://www.opengis.net/citygml/2.0}"
XLINK = "{http://www.w3.org/1999/xlink}"
XSI   = "{http://www.w3.org/2001/XMLSchema-instance}"

SCHEMA_LOCATION = (
    "http://www.opengis.net/citygml/building/2.0 "
    "http://schemas.opengis.net/citygml/building/2.0/building.xsd "
    "http://www.opengis.net/citygml/cityobjectgroup/2.0 "
    "http://schemas.opengis.net/citygml/cityobjectgroup/2.0/cityObjectGroup.xsd "
    "http://www.opengis.net/citygml/tunnel/2.0 "
    "http://schemas.opengis.net/citygml/tunnel/2.0/tunnel.xsd "
    "http://www.opengis.net/citygml/waterbody/2.0 "
    "http://schemas.opengis.net/citygml/waterbody/2.0/waterBody.xsd "
    "http://www.opengis.net/citygml/appearance/2.0 "
    "http://schemas.opengis.net/citygml/appearance/2.0/appearance.xsd "
    "http://www.opengis.net/citygml/cityfurniture/2.0 "
    "http://schemas.opengis.net/citygml/cityfurniture/2.0/cityFurniture.xsd "
    "http://www.opengis.net/citygml/bridge/2.0 "
    "http://schemas.opengis.net/citygml/bridge/2.0/bridge.xsd "
    "http://www.opengis.net/citygml/generics/2.0 "
    "http://schemas.opengis.net/citygml/generics/2.0/generics.xsd "
    "http://www.opengis.net/citygml/vegetation/2.0 "
    "http://schemas.opengis.net/citygml/vegetation/2.0/vegetation.xsd "
    "http://www.opengis.net/citygml/relief/2.0 "
    "http://schemas.opengis.net/citygml/relief/2.0/relief.xsd "
    "http://www.opengis.net/citygml/transportation/2.0 "
    "http://schemas.opengis.net/citygml/transportation/2.0/transportation.xsd "
    "http://www.opengis.net/citygml/landuse/2.0 "
    "http://schemas.opengis.net/citygml/landuse/2.0/landUse.xsd"
)

BLDG_SURFACE_TAG = {
    "WallSurface":          f"{BLDG}WallSurface",
    "RoofSurface":          f"{BLDG}RoofSurface",
    "GroundSurface":        f"{BLDG}GroundSurface",
    "ClosureSurface":       f"{BLDG}ClosureSurface",
    "FloorSurface":         f"{BLDG}FloorSurface",
    "InteriorWallSurface":  f"{BLDG}InteriorWallSurface",
    "CeilingSurface":       f"{BLDG}CeilingSurface",
    "OuterFloorSurface":    f"{BLDG}OuterFloorSurface",
    "OuterCeilingSurface":  f"{BLDG}OuterCeilingSurface",
}

EXPORT_BATCH_SIZE = 100

# Active dataset CRS, set at export() start so deep writers (e.g. per-surface
# envelopes) can emit srsName/srsDimension without threading it through.
_EXPORT_CRS: dict = {}
# GeometryPrototype ids already written inline in the current document.
_EMITTED_PROTOTYPES: set = set()

# ---------------------------------------------------------------------------
# Module registry for CityObject export
# ---------------------------------------------------------------------------
# feature_type → (ns_uri, [(prop_key, element_local_name)],
#                 bsurf_ns_uri_or_None, geom_style)
#
# geom_style controls which XML element wraps the geometry:
#   "solid_or_surface" → lod{N}Solid / lod{N}MultiSurface
#   "surface_only"     → lod{N}MultiSurface only
#   "geometry"         → lod{N}Geometry (wraps Solid or MultiSurface)
#   "relief"           → lod{N}MultiSurface (TIN reconstructed as polygons)

_TRAN_NS = "http://www.opengis.net/citygml/transportation/2.0"
_VEG_NS  = "http://www.opengis.net/citygml/vegetation/2.0"
_WTR_NS  = "http://www.opengis.net/citygml/waterbody/2.0"
_LUSE_NS = "http://www.opengis.net/citygml/landuse/2.0"
_FRN_NS  = "http://www.opengis.net/citygml/cityfurniture/2.0"
_DEM_NS  = "http://www.opengis.net/citygml/relief/2.0"
_BRID_NS = "http://www.opengis.net/citygml/bridge/2.0"
_TUN_NS  = "http://www.opengis.net/citygml/tunnel/2.0"
_GRP_NS  = "http://www.opengis.net/citygml/cityobjectgroup/2.0"

_S = [("class", "class"), ("function", "function"), ("usage", "usage")]

CO_MODULE_MAP: dict = {
    "Road":                    (_TRAN_NS, _S + [("surface_material", "surfaceMaterial")],                   None,    "surface_only"),
    "Railway":                 (_TRAN_NS, _S + [("surface_material", "surfaceMaterial")],                   None,    "surface_only"),
    "Track":                   (_TRAN_NS, _S + [("surface_material", "surfaceMaterial")],                   None,    "surface_only"),
    "Square":                  (_TRAN_NS, _S + [("surface_material", "surfaceMaterial")],                   None,    "surface_only"),
    "TransportationComplex":   (_TRAN_NS, _S + [("surface_material", "surfaceMaterial")],                   None,    "surface_only"),
    "SolitaryVegetationObject":(_VEG_NS,  _S + [("species", "species"), ("height", "height"),
                                                 ("trunk_diameter", "trunkDiameter"),
                                                 ("crown_diameter", "crownDiameter"),
                                                 ("average_height", "averageHeight")],                      None,    "geometry"),
    "PlantCover":              (_VEG_NS,  _S + [("average_height", "averageHeight")],                       None,    "geometry"),
    "WaterBody":               (_WTR_NS,  _S + [("water_level", "waterLevel")],                             _WTR_NS, "solid_or_surface"),
    "LandUse":                 (_LUSE_NS, _S,                                                               None,    "surface_only"),
    "CityFurniture":           (_FRN_NS,  _S,                                                               None,    "geometry"),
    "ReliefFeature":           (_DEM_NS,  [("lod", "lod")],                                                 None,    "relief"),
    "Bridge":                  (_BRID_NS, _S + [("year_of_construction", "yearOfConstruction"),
                                                 ("year_of_demolition",  "yearOfDemolition"),
                                                 ("is_movable",          "isMovable")],                     _BRID_NS, "solid_or_surface"),
    "Tunnel":                  (_TUN_NS,  _S + [("year_of_construction", "yearOfConstruction"),
                                                 ("year_of_demolition",  "yearOfDemolition")],              _TUN_NS,  "solid_or_surface"),
    "CityObjectGroup":         (_GRP_NS,  _S,                                                               None,     "geometry"),
    "GenericCityObject":       (GEN[1:-1], _S,                                                              None,     "geometry"),
}


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _new_id():
    return f"ID_{uuid.uuid4()}"


def _sub(parent, tag, text=None, **attribs):
    el = etree.SubElement(parent, tag, **attribs)
    if text is not None:
        el.text = str(text)
    return el


def _gen_string_attrs(parent, props, gen_attrs_map):
    """Emit gen:*Attribute elements, preserving the original type and uom.

    gen_attrs_map values are either a plain name (legacy) or
    {"name": ..., "type": "string|int|double|date|uri|measure"[, "set": [...]]}.
    A "set" entry is the root→leaf gen:genericAttributeSet wrapper path captured
    at ingest ([{"name":…, "i": sibling-index[, "codeSpace":…]}, …]); wrappers are
    recreated here so nested attribute sets (e.g. Helsinki's per-building data-
    integration set) round-trip instead of being flattened away.
    """
    # wrapper-path -> already-created gen:genericAttributeSet element
    set_elements = {}

    def _set_container(set_path):
        if not set_path:
            return parent
        key = tuple((e.get("name", ""), e.get("i", 0), e.get("codeSpace"))
                    for e in set_path)
        el = set_elements.get(key)
        if el is None:
            container = _set_container(set_path[:-1])
            leaf = set_path[-1]
            el = _sub(container, f"{GEN}genericAttributeSet")
            if leaf.get("name"):
                el.set("name", leaf["name"])
            if leaf.get("codeSpace"):
                el.set("codeSpace", leaf["codeSpace"])
            set_elements[key] = el
        return el

    for key, meta in gen_attrs_map.items():
        if key not in props:
            continue
        if isinstance(meta, dict):
            original_name = meta.get("name", key)
            gtype         = meta.get("type", "string")
            container     = _set_container(meta.get("set") or [])
        else:
            original_name, gtype = meta, "string"
            container = parent
        sa  = _sub(container, f"{GEN}{gtype}Attribute", name=original_name)
        val = _sub(sa, f"{GEN}value", props[key])
        if gtype == "measure":
            uom = props.get(f"{key}_uom")
            if uom:
                val.set("uom", uom)


def _emit_common_extras(feat_el, props):
    """Re-emit the verbatim-preserved AbstractCityObject/_Feature/_GML content
    captured by ingest's _capture_common_extras: cg:terminationDate (scalar),
    gml:metaDataProperty, gml:location, cg:generalizesTo, cg:externalReference and
    ADE / gml:genericADEElement. Element order within a feature is not significant
    for the lossless gate (census compares element/attribute/text/coordinate
    multisets, not sequence)."""
    if props.get("termination_date"):
        _sub(feat_el, f"{CG}terminationDate", props["termination_date"])
    for key in ("metadata_xml", "generalizes_to_xml", "external_reference_xml", "ade_xml"):
        raw = props.get(key)
        if not raw:
            continue
        try:
            frags = json.loads(raw)
        except ValueError:
            continue
        for frag in frags:
            try:
                feat_el.append(etree.fromstring(frag))
            except etree.XMLSyntaxError:
                pass
    loc = props.get("location_xml")
    if loc:
        try:
            feat_el.append(etree.fromstring(loc))
        except etree.XMLSyntaxError:
            pass


def _write_polygon(parent_el, poly_data):
    """Write <gml:surfaceMember><gml:Polygon>… into parent_el."""
    sm = _sub(parent_el, f"{GML}surfaceMember")
    _write_polygon_into(sm, poly_data)


def _write_polygon_into(parent_el, poly_data):
    """Write a bare gml:Polygon (exterior/interior rings) directly into parent_el."""
    pp      = poly_data["props"]
    poly_el = _sub(parent_el, f"{GML}Polygon")
    poly_el.set(f"{GML}id", pp["id"])

    ext_ring = poly_data.get("ext_ring")
    if ext_ring:
        ext_el  = _sub(poly_el, f"{GML}exterior")
        ring_el = _sub(ext_el, f"{GML}LinearRing")
        ring_el.set(f"{GML}id", ext_ring["id"])
        _sub(ring_el, f"{GML}posList",
             ext_ring["pos_list"],
             srsDimension=str(ext_ring.get("srs_dimension", 3)))

    for itr_entry in poly_data.get("int_rings", []):
        itr     = itr_entry["ring"]
        int_el  = _sub(poly_el, f"{GML}interior")
        ring_el = _sub(int_el, f"{GML}LinearRing")
        ring_el.set(f"{GML}id", itr["id"])
        _sub(ring_el, f"{GML}posList",
             itr["pos_list"],
             srsDimension=str(itr.get("srs_dimension", 3)))


def _load_gen_map(props):
    raw = props.get("_gen_attrs_map")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _write_member(container_el, m):
    """Write one gml:surfaceMember from a member descriptor
    ``{order, via_xlink, orientation, target_id, is_composite, poly_data}``.
    via_xlink members (recovered from edges that were xlink:href in the source)
    are re-emitted as xlink:href; gml:OrientableSurface wrappers re-emit their
    orientation; inline members write the full gml:Polygon."""
    orientation = m.get("orientation")
    href = bool(m.get("via_xlink")) or m.get("poly_data") is None
    if orientation:
        sm   = _sub(container_el, f"{GML}surfaceMember")
        os_  = _sub(sm, f"{GML}OrientableSurface")
        os_.set("orientation", orientation)
        base = _sub(os_, f"{GML}baseSurface")
        if href:
            base.set(f"{XLINK}href", f"#{m['target_id']}")
        else:
            _write_polygon_into(base, m["poly_data"])
    elif href:
        _sub(container_el, f"{GML}surfaceMember").set(f"{XLINK}href", f"#{m['target_id']}")
    else:
        _write_polygon(container_el, m["poly_data"])


def _write_members(container_el, members):
    for m in members:
        _write_member(container_el, m)


def _write_multisurface(parent_el, prop_tag, ms_id, members, composite_id=None):
    """Write <prop_tag><gml:MultiSurface>... from an ordered member list. When
    composite_id is set, members are wrapped in a single gml:CompositeSurface
    (the inline grouping a lodNSolid may reference by xlink)."""
    prop_el = _sub(parent_el, prop_tag)
    ms      = _sub(prop_el, f"{GML}MultiSurface")
    ms.set(f"{GML}id", ms_id or _new_id())
    if composite_id:
        cs = _sub(_sub(ms, f"{GML}surfaceMember"), f"{GML}CompositeSurface")
        cs.set(f"{GML}id", composite_id)
        _write_members(cs, members)
    else:
        _write_members(ms, members)
    return prop_el


def _members_by_container(rows):
    """Group member-edge rows into ordered member-descriptor lists, keyed by
    container id. Each row has: cid, tid, is_composite, morder, via_xlink,
    orientation, ext, int_rings_raw. Inline (non-xlink polygon) members carry
    their ring geometry; xlink and composite members carry only target_id."""
    by_cid = {}
    for row in rows:
        if row["tid"] is None:
            continue
        by_cid.setdefault(row["cid"], []).append(_member_from_row(row))
    for cid in by_cid:
        by_cid[cid].sort(key=lambda x: x["order"])
    return by_cid


_MEMBER_RETURN = (
    "t.id AS tid, ('GeometryCompositeSurface' IN labels(t)) AS is_composite, "
    "coalesce(m.order,0) AS morder, coalesce(m.via_xlink,false) AS via_xlink, "
    "m.orientation AS orientation, ext, "
    "collect({ring: itr, idx: coalesce(hir.ring_index,0)}) AS int_rings_raw"
)


def query_solid_members(sess, solid_ids):
    """Ordered surfaceMembers of each GeometrySolid (id → [member descriptors])."""
    if not solid_ids:
        return {}
    rows = sess.run(
        f"""
        MATCH (sol:GeometrySolid) WHERE sol.id IN $ids
        MATCH (sol)-[m:HAS_SURFACE_MEMBER]->(t)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN sol.id AS cid, {_MEMBER_RETURN}
        ORDER BY cid, morder
        """,
        ids=solid_ids,
    )
    return _members_by_container(rows)


def _write_surface_openings(surf_el, openings):
    """Write bldg:opening → Door/Window elements for a boundary surface."""
    for op in openings:
        op_props = op["props"]
        op_el    = _sub(surf_el, f"{BLDG}opening")
        kind     = op_props.get("opening_type", "Window")
        feat     = _sub(op_el, f"{BLDG}{kind}")
        feat.set(f"{GML}id", op_props["id"])
        _write_feature_scalars(feat, op_props)
        ms = op.get("ms") or {}
        if op.get("members"):
            _write_multisurface(
                feat, f"{BLDG}lod{op.get('lod', '3')}MultiSurface",
                ms.get("id"), op["members"],
                composite_id=ms.get("composite_surface_id"))


def _write_bldg_surface(parent_el, surf_data):
    """Write a bldg:boundedBy → <SurfaceType> element (with name/desc, gen attrs,
    its lodN MultiSurface, and any openings). Reused for building shell and
    interior room surfaces."""
    sp        = surf_data["props"]
    surf_type = sp.get("surface_type", "WallSurface")
    lod_n     = sp.get("lod", "2")
    tag       = BLDG_SURFACE_TAG.get(surf_type, f"{BLDG}{surf_type}")

    bb      = _sub(parent_el, f"{BLDG}boundedBy")
    surf_el = _sub(bb, tag)
    surf_el.set(f"{GML}id", sp["id"])

    if sp.get("gml_description"):
        _sub(surf_el, f"{GML}description", sp["gml_description"])
    if sp.get("gml_name"):
        _sub(surf_el, f"{GML}name", sp["gml_name"])
    if sp.get("has_bbox_envelope") and sp.get("bbox_lower_corner") and \
            sp.get("bbox_upper_corner"):
        crs = _EXPORT_CRS.get("crs"); sdim = _EXPORT_CRS.get("srs_dim", 3)
        env = _sub(_sub(surf_el, f"{GML}boundedBy"), f"{GML}Envelope")
        if crs:
            env.set("srsName", str(crs))
        env.set("srsDimension", str(sdim))
        _sub(env, f"{GML}lowerCorner", sp["bbox_lower_corner"], srsDimension=str(sdim))
        _sub(env, f"{GML}upperCorner", sp["bbox_upper_corner"], srsDimension=str(sdim))
    if sp.get("creation_date"):
        _sub(surf_el, f"{CG}creationDate", sp["creation_date"])
    if sp.get("relative_to_terrain"):
        _sub(surf_el, f"{CG}relativeToTerrain", sp["relative_to_terrain"])
    if sp.get("relative_to_water"):
        _sub(surf_el, f"{CG}relativeToWater", sp["relative_to_water"])
    _gen_string_attrs(surf_el, sp, _load_gen_map(sp))
    _emit_common_extras(surf_el, sp)

    if surf_data.get("members"):
        _write_multisurface(surf_el, f"{BLDG}lod{lod_n}MultiSurface",
                            sp.get("multi_surface_id"), surf_data["members"],
                            composite_id=sp.get("composite_surface_id"))

    _write_surface_openings(surf_el, surf_data.get("openings", []))
    return surf_el


def _write_tin(feat_el, ms_entry, crs, srs_dim):
    """Write a ReliefFeature's TIN natively:
    dem:reliefComponent/dem:TINRelief/dem:tin/gml:TriangulatedSurface/
    gml:trianglePatches/gml:Triangle*. Each member polygon is a triangle whose
    exterior ring is emitted directly."""
    DEM = "{http://www.opengis.net/citygml/relief/2.0}"
    msp = ms_entry["ms"]
    rc  = _sub(feat_el, f"{DEM}reliefComponent")
    tr  = _sub(rc, f"{DEM}TINRelief")
    if msp.get("tin_relief_id"):
        tr.set(f"{GML}id", msp["tin_relief_id"])
    if msp.get("tin_bbox_lower") and msp.get("tin_bbox_upper") and crs:
        bb  = _sub(tr, f"{GML}boundedBy")
        env = _sub(bb, f"{GML}Envelope", srsName=str(crs), srsDimension=str(srs_dim))
        _sub(env, f"{GML}lowerCorner", msp["tin_bbox_lower"], srsDimension=str(srs_dim))
        _sub(env, f"{GML}upperCorner", msp["tin_bbox_upper"], srsDimension=str(srs_dim))
    if msp.get("tin_creation_date"):
        _sub(tr, f"{CG}creationDate", msp["tin_creation_date"])
    _sub(tr, f"{DEM}lod", str(ms_entry.get("lod") or "0"))
    ts = _sub(_sub(tr, f"{DEM}tin"), f"{GML}TriangulatedSurface")
    if msp.get("triangulated_surface_id"):
        ts.set(f"{GML}id", msp["triangulated_surface_id"])
    tp = _sub(ts, f"{GML}trianglePatches")
    for m in ms_entry["members"]:
        pd = m.get("poly_data")
        if not pd or not pd.get("ext_ring"):
            continue
        ring   = _sub(_sub(_sub(tp, f"{GML}Triangle"), f"{GML}exterior"), f"{GML}LinearRing")
        extr   = pd["ext_ring"]
        if extr.get("id"):
            ring.set(f"{GML}id", extr["id"])
        _sub(ring, f"{GML}posList", extr["pos_list"],
             srsDimension=str(extr.get("srs_dimension", 3)))


def _write_solid(parent_el, solid):
    """Write a bldg:lod{N}Solid from its ordered HAS_SURFACE_MEMBER edges
    (solid['members']): xlink references re-emit as xlink:href, inline members
    (LoD1) write the full gml:Polygon, OrientableSurface wrappers re-emit their
    orientation."""
    lod_n  = solid.get("lod", "2")
    lod_el = _sub(parent_el, f"{BLDG}lod{lod_n}Solid")
    sol_el = _sub(lod_el, f"{GML}Solid")
    sol_el.set(f"{GML}id", solid["id"])
    cs = _sub(_sub(sol_el, f"{GML}exterior"), f"{GML}CompositeSurface")
    cs.set(f"{GML}id", solid.get("composite_surface_id") or _new_id())
    _write_members(cs, solid.get("members", []))


# ---------------------------------------------------------------------------
# Spatial plugin check
# ---------------------------------------------------------------------------

def _spatial_ok(sess):
    try:
        sess.run("CALL spatial.layers()").consume()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Common polygon query result processor
# ---------------------------------------------------------------------------

def _member_from_row(row):
    """Build one member descriptor from a member-edge row (see _MEMBER_RETURN)."""
    m = {"order": row["morder"], "via_xlink": bool(row["via_xlink"]),
         "orientation": row["orientation"], "target_id": row["tid"],
         "is_composite": bool(row["is_composite"]), "poly_data": None}
    if not m["via_xlink"] and not m["is_composite"] and row["ext"] is not None:
        int_rings = []
        for itr_entry in row["int_rings_raw"]:
            itr = itr_entry.get("ring")
            if itr is None:
                continue
            ring = dict(itr)
            if not any(r["ring"]["id"] == ring["id"] for r in int_rings):
                int_rings.append({"ring": ring, "idx": itr_entry["idx"]})
        int_rings.sort(key=lambda e: e["idx"])
        m["poly_data"] = {"props": {"id": row["tid"]},
                          "ext_ring": dict(row["ext"]), "int_rings": int_rings}
    return m


def _process_surface_rows(rows, id_key):
    """
    Shared result processor for boundary-surface query rows.
    Returns dict: feature_id → [surf_data, ...] sorted by boundary_order, each
    surf_data = {"props", "members": [member descriptors], "openings": []}.
    """
    surf_maps = {}   # fid -> sid -> {props, b_order, _members: {order: m}}
    for row in rows:
        fid  = row[id_key]
        surf = row["surf"]
        if surf is None:
            continue
        surf = dict(surf)
        sid  = surf["id"]
        smap = surf_maps.setdefault(fid, {})
        if sid not in smap:
            smap[sid] = {"props": surf, "b_order": row["boundary_order"], "_members": {}}
        if row["tid"] is None or row["morder"] in smap[sid]["_members"]:
            continue
        smap[sid]["_members"][row["morder"]] = _member_from_row(row)

    result = {}
    for fid, smap in surf_maps.items():
        surfs = sorted(smap.values(), key=lambda s: s["b_order"])
        for sd in surfs:
            sd["members"] = [sd["_members"][o] for o in sorted(sd["_members"])]
            del sd["_members"]
            del sd["b_order"]
        result[fid] = surfs
    return result


# ---------------------------------------------------------------------------
# Building graph queries
# ---------------------------------------------------------------------------

def query_dataset(sess, dataset=None, feature_ids=None):
    """The Dataset to export: by id/name/source when `dataset` is given; else the Dataset
    of the first selected feature; else the only Dataset. Several Datasets without either
    stop with an error (before, 'MATCH (d:Dataset) LIMIT 1' exported an arbitrary one). Returns {} when nothing matches."""
    if dataset:
        row = sess.run(
            "MATCH (d:Dataset) WHERE d.id = $ds OR d.name = $ds OR d.source = $ds "
            "RETURN d LIMIT 1", ds=dataset,
        ).single()
        return dict(row["d"]) if row else {}
    if feature_ids:
        row = sess.run(
            "UNWIND $ids AS fid MATCH (n:Entity {id: fid})-[:PART_OF]->(d:Dataset) "
            "RETURN d LIMIT 1", ids=list(feature_ids),
        ).single()
        if row:
            return dict(row["d"])
    rows = sess.run("MATCH (d:Dataset) RETURN d ORDER BY d.id").data()
    if len(rows) > 1:
        raise SystemExit("ERROR: several Datasets in the graph — pass --dataset one of: "
                         + ", ".join(str(r["d"].get("id")) for r in rows))
    return dict(rows[0]["d"]) if rows else {}


def split_feature_ids(sess, ids):
    """Partition requested ids into (building_ids, city_object_ids, missing),
    keeping the caller's order. BuildingParts are exported through their parent
    Building, so a part id is reported as missing rather than exported alone."""
    rows = sess.run(
        "UNWIND $ids AS fid "
        "OPTIONAL MATCH (b:Building {id: fid}) "
        "OPTIONAL MATCH (c:CityObject {id: fid}) "
        "RETURN fid, b IS NOT NULL AS is_bldg, c IS NOT NULL AS is_co",
        ids=list(ids),
    )
    kind = {r["fid"]: ("b" if r["is_bldg"] else "c" if r["is_co"] else None) for r in rows}
    bldg = [i for i in ids if kind.get(i) == "b"]
    cos = [i for i in ids if kind.get(i) == "c"]
    missing = [i for i in ids if kind.get(i) is None]
    return bldg, cos, missing


def query_selection_envelope(sess, ids, srs_dim):
    """Tight (lower, upper) corner strings around the selected features, or None
    when any of them lacks bbox scalars (caller then keeps the Dataset envelope)."""
    row = sess.run(
        "UNWIND $ids AS fid MATCH (n:Entity {id: fid}) "
        "RETURN count(n) AS n, count(n.bbox_min_x) AS with_bbox, "
        "min(n.bbox_min_x) AS x0, min(n.bbox_min_y) AS y0, min(n.bbox_min_z) AS z0, "
        "max(n.bbox_max_x) AS x1, max(n.bbox_max_y) AS y1, max(n.bbox_max_z) AS z1",
        ids=list(ids),
    ).single()
    if not row or not row["n"] or row["with_bbox"] != row["n"]:
        return None
    if srs_dim == 2 or row["z0"] is None or row["z1"] is None:
        return f"{row['x0']!r} {row['y0']!r}", f"{row['x1']!r} {row['y1']!r}"
    return (f"{row['x0']!r} {row['y0']!r} {row['z0']!r}",
            f"{row['x1']!r} {row['y1']!r} {row['z1']!r}")


def query_all_building_ids(sess, source, bbox_wkt=None, dataset_id=None, layer="features"):
    """Buildings of the Dataset. Selection is by PART_OF the Dataset (a --dataset-name ingest stores Dataset.source = <dataset name> but
    Building.source = <tile file>, so the old `source` filter exported no buildings);
    the `source` match remains only for callers that pass no dataset id."""
    if bbox_wkt:
        try:
            rows = sess.run(
                """
                CALL spatial.intersects($layer, $wkt) YIELD node AS b
                WITH b WHERE b:Building AND
                     (($did IS NOT NULL AND (b)-[:PART_OF]->(:Dataset {id: $did}))
                      OR ($did IS NULL AND b.source = $src))
                RETURN DISTINCT b.id AS id ORDER BY id
                """,
                wkt=bbox_wkt, src=source, did=dataset_id, layer=layer,
            )
            return [r["id"] for r in rows]
        except Exception as exc:
            print(f"WARNING: spatial.intersects unavailable ({exc}), falling back.")

    if dataset_id:
        rows = sess.run(
            "MATCH (b:Building)-[:PART_OF]->(:Dataset {id: $did}) RETURN b.id AS id ORDER BY b.id",
            did=dataset_id,
        )
        return [r["id"] for r in rows]
    rows = sess.run(
        "MATCH (b:Building {source: $src}) RETURN b.id AS id ORDER BY b.id",
        src=source,
    )
    return [r["id"] for r in rows]


def query_buildings_core_batch(sess, bldg_ids, label="Building"):
    """Building/BuildingPart props + ALL its lod*Solid nodes + terrain-intersection
    lines + lod*MultiCurve lines. A feature can carry one solid per LOD at the same
    time (Helsinki ships lod1Solid + lod2Solid on the same building), so solids are
    aggregated before the terrain-intersection expansion to avoid a row cross-
    product. Solid surfaceMembers are fetched separately via query_solid_members()."""
    rows = sess.run(
        f"""
        MATCH (b:{label}) WHERE b.id IN $ids
        OPTIONAL MATCH (b)-[:HAS_LOD_SOLID]->(sol:GeometrySolid)
        WITH b, collect(DISTINCT sol) AS sols
        OPTIONAL MATCH (b)-[:HAS_TERRAIN_INTERSECTION]->(ti:TerrainIntersection)
        OPTIONAL MATCH (ti)-[hl:HAS_LINE]->(ls:GeometryLineString)
        WITH b, sols, ti, collect({{ls: ls, order: coalesce(hl.order, 0)}}) AS lines_raw
        RETURN b, sols, lines_raw, coalesce(ti.lod, '2') AS ti_lod
        """,
        ids=bldg_ids,
    )
    result = {}
    for row in rows:
        b   = dict(row["b"])
        lines_sorted = sorted(
            [x for x in row["lines_raw"] if x["ls"] is not None],
            key=lambda x: x["order"],
        )
        solids = sorted((dict(s) for s in row["sols"] if s is not None),
                        key=lambda s: str(s.get("lod", "9")))
        result[b["id"]] = {
            "props":  b,
            "solids": solids,
            "lines":  [dict(x["ls"]) for x in lines_sorted],
            "ti_lod": row["ti_lod"],
        }
    return result


def _ms_entries_from_rows(rows, key):
    """Group MultiSurface member rows into {key: [{ms, lod, role, members}]}.
    Each MS entry's members are ordered descriptors (built via _member_from_row).
    Rows carry: <key>, ms, lod, role, and the _MEMBER_RETURN columns."""
    result = {}
    idx    = {}
    for row in rows:
        k  = row[key]
        ms = row["ms"]
        if ms is None:
            continue
        ms    = dict(ms)
        ekey  = (k, ms["id"])
        result.setdefault(k, [])
        idx.setdefault(k, {})
        if ekey not in idx[k]:
            idx[k][ekey] = len(result[k])
            result[k].append({"ms": ms, "lod": row["lod"],
                              "role": row["role"], "members": []})
        if row["tid"] is not None:
            result[k][idx[k][ekey]]["members"].append(_member_from_row(row))
    for k in result:
        for e in result[k]:
            e["members"].sort(key=lambda m: m["order"])
    return result


def query_buildings_ms_batch(sess, bldg_ids, label="Building"):
    """MultiSurface geometry attached directly to a Building/BuildingPart
    (lod0FootPrint / lod0RoofEdge / lodN MultiSurface), with role + lod + members."""
    rows = sess.run(
        f"""
        MATCH (b:{label}) WHERE b.id IN $ids
        MATCH (b)-[hls:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
        OPTIONAL MATCH (ms)-[m:HAS_SURFACE_MEMBER]->(t:GeometryPolygon)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN b.id AS cid, ms, hls.lod AS lod, hls.role AS role, {_MEMBER_RETURN}
        ORDER BY cid, role, lod, morder
        """,
        ids=bldg_ids,
    )
    return _ms_entries_from_rows(rows, "cid")


def query_surface_openings_batch(sess, surf_ids):
    """bldg:opening (Door/Window) for BoundarySurface ids, each with its lodN
    MultiSurface members. Returns surf_id → [{"props", "lod", "ms", "members"}]."""
    if not surf_ids:
        return {}
    rows = sess.run(
        f"""
        MATCH (s:BoundarySurface) WHERE s.id IN $ids
        MATCH (s)-[ho:HAS_OPENING]->(o:Opening)
        OPTIONAL MATCH (o)-[hls:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
        OPTIONAL MATCH (ms)-[m:HAS_SURFACE_MEMBER]->(t:GeometryPolygon)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN s.id AS surf_id, o, coalesce(ho.order,0) AS op_order,
               ms, hls.lod AS lod, {_MEMBER_RETURN}
        ORDER BY surf_id, op_order, morder
        """,
        ids=surf_ids,
    )
    result   = {}
    op_index = {}
    for row in rows:
        sid = row["surf_id"]
        o   = row["o"]
        if o is None:
            continue
        o    = dict(o)
        o_id = o["id"]
        result.setdefault(sid, [])
        op_index.setdefault(sid, {})
        if o_id not in op_index[sid]:
            op_index[sid][o_id] = len(result[sid])
            result[sid].append({"props": o, "lod": row["lod"],
                                "ms": dict(row["ms"]) if row["ms"] else {},
                                "members": []})
        if row["tid"] is not None:
            result[sid][op_index[sid][o_id]]["members"].append(_member_from_row(row))
    for sid in result:
        for op in result[sid]:
            op["members"].sort(key=lambda m: m["order"])
    return result


def _query_surfaces(sess, ids, parent_match):
    """Fetch HAS_BOUNDARY surfaces (with HAS_POLYGON members) for parents matched
    by parent_match (e.g. 'p:Building' or 'p'). Returns {parent_id → [surf_data]}."""
    rows = sess.run(
        f"""
        MATCH (p{':' + parent_match if parent_match else ''}) WHERE p.id IN $ids
        MATCH (p)-[hb:HAS_BOUNDARY]->(surf:BoundarySurface)
        OPTIONAL MATCH (surf)-[m:HAS_POLYGON]->(t:GeometryPolygon)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN p.id AS parent_id, surf,
               coalesce(hb.order,0) AS boundary_order, {_MEMBER_RETURN}
        ORDER BY parent_id, boundary_order, morder
        """,
        ids=ids,
    )
    return _process_surface_rows(rows, "parent_id")


def query_buildings_surfaces_batch(sess, bldg_ids, label="Building"):
    return _query_surfaces(sess, bldg_ids, label)


def query_surfaces_by_parent(sess, parent_ids):
    """Generic HAS_BOUNDARY surfaces for any feature parent (Room/Installation)."""
    if not parent_ids:
        return {}
    return _query_surfaces(sess, parent_ids, "")


def query_solids_by_parent(sess, parent_ids):
    """HAS_LOD_SOLID solid node for any feature parent (members fetched separately)."""
    if not parent_ids:
        return {}
    rows = sess.run(
        "MATCH (p)-[:HAS_LOD_SOLID]->(s:GeometrySolid) WHERE p.id IN $ids "
        "RETURN p.id AS pid, s",
        ids=parent_ids,
    )
    return {r["pid"]: dict(r["s"]) for r in rows}


def query_lod_geometry_by_parent(sess, feat_ids):
    """HAS_LOD_GEOMETRY MultiSurface (lod*Geometry) for furniture/installations."""
    if not feat_ids:
        return {}
    rows = sess.run(
        f"""
        MATCH (n)-[g:HAS_LOD_GEOMETRY]->(ms:GeometryMultiSurface) WHERE n.id IN $ids
        OPTIONAL MATCH (ms)-[m:HAS_SURFACE_MEMBER]->(t:GeometryPolygon)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN n.id AS cid, ms, g.lod AS lod, null AS role, {_MEMBER_RETURN}
        ORDER BY cid, morder
        """,
        ids=feat_ids,
    )
    return _ms_entries_from_rows(rows, "cid")


def attach_building_interior(sess, cores_map, label="Building"):
    """Populate core['rooms'], core['outer_installations'],
    core['interior_installations'] and core['multicurves'] for each building/part,
    with each room's surfaces (+openings), solid, furniture and installations."""
    bldg_ids = list(cores_map.keys())
    if not bldg_ids:
        return

    # interiorRoom → Room
    room_rows = sess.run(
        f"MATCH (b:{label})-[r:HAS_INTERIOR_ROOM]->(rm:Room) WHERE b.id IN $ids "
        "RETURN b.id AS bid, rm, coalesce(r.order,0) AS ord ORDER BY bid, ord",
        ids=bldg_ids,
    ).data()
    # outerBuildingInstallation
    outer_rows = sess.run(
        f"MATCH (b:{label})-[r:HAS_OUTER_INSTALLATION]->(i:BuildingInstallation) "
        "WHERE b.id IN $ids RETURN b.id AS bid, i, coalesce(r.order,0) AS ord "
        "ORDER BY bid, ord",
        ids=bldg_ids,
    ).data()
    # interiorBuildingInstallation
    int_inst_rows = sess.run(
        f"MATCH (b:{label})-[r:HAS_INTERIOR_INSTALLATION]->(i:BuildingInstallation) "
        "WHERE b.id IN $ids RETURN b.id AS bid, i, coalesce(r.order,0) AS ord "
        "ORDER BY bid, ord",
        ids=bldg_ids,
    ).data()
    # lod{N}MultiCurve lines, grouped by LOD
    mc_rows = sess.run(
        f"MATCH (b:{label})-[r:HAS_LOD_MULTICURVE]->(ls:GeometryLineString) "
        "WHERE b.id IN $ids "
        "RETURN b.id AS bid, ls, r.lod AS lod, coalesce(r.order,0) AS ord "
        "ORDER BY bid, lod, ord",
        ids=bldg_ids,
    ).data()

    rooms_by_bid = {}
    room_ids = []
    for r in room_rows:
        room = {"props": dict(r["rm"]), "surfaces": [], "furniture": [],
                "installations": [], "solid": None}
        rooms_by_bid.setdefault(r["bid"], []).append(room)
        room_ids.append(room["props"]["id"])

    # furniture and room installations
    furn_rows = sess.run(
        "MATCH (rm:Room)-[r:HAS_INTERIOR_FURNITURE]->(f:BuildingFurniture) "
        "WHERE rm.id IN $ids RETURN rm.id AS rid, f, coalesce(r.order,0) AS ord "
        "ORDER BY rid, ord", ids=room_ids,
    ).data()
    rinst_rows = sess.run(
        "MATCH (rm:Room)-[r:HAS_ROOM_INSTALLATION]->(i:BuildingInstallation) "
        "WHERE rm.id IN $ids RETURN rm.id AS rid, i, coalesce(r.order,0) AS ord "
        "ORDER BY rid, ord", ids=room_ids,
    ).data()

    furn_ids = [dict(fr["f"])["id"] for fr in furn_rows]
    inst_ids = [dict(ir["i"])["id"] for ir in rinst_rows] + \
               [dict(orow["i"])["id"] for orow in outer_rows] + \
               [dict(orow["i"])["id"] for orow in int_inst_rows]

    # Geometry / surfaces / solids for nested features
    room_surfaces = query_surfaces_by_parent(sess, room_ids)
    inst_surfaces = query_surfaces_by_parent(sess, inst_ids)
    room_solids   = query_solids_by_parent(sess, room_ids)
    furn_geom     = query_lod_geometry_by_parent(sess, furn_ids)
    inst_geom     = query_lod_geometry_by_parent(sess, inst_ids)

    # openings across ALL surfaces (shell already handled by caller; here nested)
    nested_surf_ids = [sd["props"]["id"]
                       for sl in list(room_surfaces.values()) + list(inst_surfaces.values())
                       for sd in sl]
    openings_map = query_surface_openings_batch(sess, nested_surf_ids)

    def _attach_openings(surf_list):
        for sd in surf_list:
            sd["openings"] = openings_map.get(sd["props"]["id"], [])

    def _first(entries):
        return entries[0] if entries else None

    # Build installation objects keyed by id
    def _make_inst(node):
        ip = dict(node)
        iid = ip["id"]
        surfs = inst_surfaces.get(iid, [])
        _attach_openings(surfs)
        return {"props": ip, "surfaces": surfs, "geom": _first(inst_geom.get(iid))}

    inst_by_room = {}
    for ir in rinst_rows:
        inst_by_room.setdefault(ir["rid"], []).append(_make_inst(ir["i"]))
    furn_by_room = {}
    for fr in furn_rows:
        fp = dict(fr["f"])
        furn_by_room.setdefault(fr["rid"], []).append(
            {"props": fp, "geom": _first(furn_geom.get(fp["id"]))})

    for bid, rooms in rooms_by_bid.items():
        for room in rooms:
            rid = room["props"]["id"]
            room["surfaces"] = room_surfaces.get(rid, [])
            _attach_openings(room["surfaces"])
            room["solid"]        = room_solids.get(rid)
            room["furniture"]    = furn_by_room.get(rid, [])
            room["installations"] = inst_by_room.get(rid, [])
        cores_map[bid]["rooms"] = rooms

    for orow in outer_rows:
        cores_map[orow["bid"]].setdefault("outer_installations", []).append(
            _make_inst(orow["i"]))

    for orow in int_inst_rows:
        cores_map[orow["bid"]].setdefault("interior_installations", []).append(
            _make_inst(orow["i"]))

    mc_by_bid: dict = {}
    for r in mc_rows:
        mc_by_bid.setdefault(r["bid"], {}).setdefault(r["lod"], []).append(dict(r["ls"]))
    for bid, by_lod in mc_by_bid.items():
        cores_map[bid]["multicurves"] = sorted(by_lod.items())


def query_building_part_ids(sess, parent_ids):
    """Return {parent_id: [(order, part_id), ...]} for direct HAS_BUILDING_PART
    children of the given Building/BuildingPart parents (one level)."""
    if not parent_ids:
        return {}
    rows = sess.run(
        "MATCH (p)-[r:HAS_BUILDING_PART]->(bp:BuildingPart) WHERE p.id IN $ids "
        "RETURN p.id AS pid, bp.id AS partid, coalesce(r.order,0) AS ord "
        "ORDER BY pid, ord",
        ids=parent_ids,
    ).data()
    out: dict = {}
    for r in rows:
        out.setdefault(r["pid"], []).append((r["ord"], r["partid"]))
    return out


def _enrich_building_cores(sess, cores_map, label, crs, srs_dim):
    """Attach geometry/interior/installations to a map of Building or BuildingPart
    cores, then recurse into consistsOfBuildingPart children so each core carries
    a fully populated ``parts`` list. Mutates cores_map in place."""
    if not cores_map:
        return
    ids          = list(cores_map)
    ms_map       = query_buildings_ms_batch(sess, ids, label)
    surfaces_map = query_buildings_surfaces_batch(sess, ids, label)
    attach_building_interior(sess, cores_map, label)

    # Solid surfaceMembers (shell + room solids)
    solid_index: dict = {}
    for core in cores_map.values():
        for solid in core.get("solids") or []:
            solid_index.setdefault(solid["id"], []).append(solid)
        for room in core.get("rooms", []):
            if room.get("solid"):
                solid_index.setdefault(room["solid"]["id"], []).append(room["solid"])
    members_map = query_solid_members(sess, list(solid_index))
    for sid, solids in solid_index.items():
        for s in solids:
            s["members"] = members_map.get(sid, [])

    # Shell-surface openings
    all_surf_ids = [sd["props"]["id"] for sl in surfaces_map.values() for sd in sl]
    openings_map = query_surface_openings_batch(sess, all_surf_ids)
    for bid, core in cores_map.items():
        core["building_ms"] = ms_map.get(bid, [])
        sds = surfaces_map.get(bid, [])
        for sd in sds:
            sd["openings"] = openings_map.get(sd["props"]["id"], [])
        core["surfaces"] = sds

    # Recurse into building parts
    part_id_map  = query_building_part_ids(sess, ids)
    all_part_ids = sorted({pid for lst in part_id_map.values() for (_o, pid) in lst})
    if not all_part_ids:
        return
    part_cores = query_buildings_core_batch(sess, all_part_ids, "BuildingPart")
    _enrich_building_cores(sess, part_cores, "BuildingPart", crs, srs_dim)
    for pid, core in cores_map.items():
        core["parts"] = [part_cores[cid] for (_o, cid) in part_id_map.get(pid, [])
                         if cid in part_cores]


# ---------------------------------------------------------------------------
# CityObject graph queries
# ---------------------------------------------------------------------------

def query_all_city_object_ids(sess, source, dataset_id=None):
    if dataset_id:
        rows = sess.run(
            "MATCH (n:CityObject)-[:PART_OF]->(:Dataset {id: $did}) "
            "RETURN n.id AS id ORDER BY n.feature_type, n.id", did=dataset_id)
        return [r["id"] for r in rows]
    rows = sess.run(
        "MATCH (n:CityObject {source: $src}) RETURN n.id AS id ORDER BY n.feature_type, n.id",
        src=source,
    )
    return [r["id"] for r in rows]


def query_city_objects_core_batch(sess, co_ids):
    """Props + ALL lod*Solid nodes (a feature may carry one solid per LOD;
    surfaceMembers fetched via query_solid_members)."""
    rows = sess.run(
        """
        MATCH (n:CityObject) WHERE n.id IN $ids
        OPTIONAL MATCH (n)-[:HAS_LOD_SOLID]->(sol:GeometrySolid)
        WITH n, collect(DISTINCT sol) AS sols
        RETURN n, sols
        """,
        ids=co_ids,
    )
    result = {}
    for row in rows:
        n = dict(row["n"])
        result[n["id"]] = {
            "props": n,
            "solids": sorted((dict(s) for s in row["sols"] if s is not None),
                             key=lambda s: str(s.get("lod", "9"))),
        }
    return result


def query_city_objects_ms_batch(sess, co_ids):
    """GeometryMultiSurface geometry (with members) for a batch of CityObject IDs.
    Returns dict: co_id → [{"ms", "lod", "role", "members"}, ...]."""
    rows = sess.run(
        f"""
        MATCH (n:CityObject) WHERE n.id IN $ids
        MATCH (n)-[hls:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
        OPTIONAL MATCH (ms)-[m:HAS_SURFACE_MEMBER]->(t:GeometryPolygon)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN n.id AS cid, ms, hls.lod AS lod, null AS role, {_MEMBER_RETURN}
        ORDER BY cid, lod, morder
        """,
        ids=co_ids,
    )
    return _ms_entries_from_rows(rows, "cid")


def query_city_objects_surfaces_batch(sess, co_ids):
    """Module boundary surfaces (wtr/brid/tun) with members.
    Returns dict: co_id → [surf_data, ...] sorted by boundary_order."""
    rows = sess.run(
        f"""
        MATCH (n:CityObject) WHERE n.id IN $ids
        MATCH (n)-[hb:HAS_BOUNDARY]->(surf:BoundarySurface)
        OPTIONAL MATCH (surf)-[m:HAS_POLYGON]->(t:GeometryPolygon)
        OPTIONAL MATCH (t)-[:HAS_EXTERIOR_RING]->(ext:GeometryRing)
        OPTIONAL MATCH (t)-[hir:HAS_INTERIOR_RING]->(itr:GeometryRing)
        RETURN n.id AS parent_id, surf,
               coalesce(hb.order, 0) AS boundary_order, {_MEMBER_RETURN}
        ORDER BY parent_id, boundary_order, morder
        """,
        ids=co_ids,
    )
    return _process_surface_rows(rows, "parent_id")


def query_nested_features(sess, parent_ids):
    """Bridge/Tunnel NestedFeatures (installations / construction elements) per
    parent, with geometry and boundary surfaces attached.
    Returns {parent_id: [{props, prop, order, geom, surfaces}]} ordered."""
    if not parent_ids:
        return {}
    rows = sess.run(
        "MATCH (p)-[hn:HAS_NESTED]->(nf:NestedFeature) WHERE p.id IN $ids "
        "RETURN p.id AS pid, nf, hn.prop AS prop, coalesce(hn.order,0) AS ord "
        "ORDER BY pid, ord",
        ids=parent_ids,
    ).data()
    nf_ids = [dict(r["nf"])["id"] for r in rows]
    geom_map = query_lod_geometry_by_parent(sess, nf_ids)
    surf_map = query_surfaces_by_parent(sess, nf_ids)
    result = {}
    for r in rows:
        nf = dict(r["nf"])
        nid = nf["id"]
        result.setdefault(r["pid"], []).append({
            "props": nf, "prop": r["prop"], "order": r["ord"],
            "geom": (geom_map.get(nid) or [None])[0],
            "surfaces": surf_map.get(nid, []),
        })
    return result


def query_group_members(sess, group_ids):
    """CityObjectGroup → ordered member references (xlink targets)."""
    if not group_ids:
        return {}
    rows = sess.run(
        "MATCH (g:CityObjectGroup)-[r:HAS_GROUP_MEMBER]->(m) WHERE g.id IN $ids "
        "RETURN g.id AS gid, m.id AS mid, coalesce(r.order,0) AS ord, r.role AS role "
        "ORDER BY gid, ord",
        ids=group_ids,
    ).data()
    result = {}
    for r in rows:
        result.setdefault(r["gid"], []).append(
            {"ref": r["mid"], "role": r["role"]})
    return result


# ---------------------------------------------------------------------------
# Building XML element construction
# ---------------------------------------------------------------------------

def _build_envelope_el(crs, srs_dim, lower, upper):
    bb  = etree.Element(f"{GML}boundedBy")
    env = etree.SubElement(bb, f"{GML}Envelope",
                           srsName=str(crs), srsDimension=str(srs_dim))
    lc = etree.SubElement(env, f"{GML}lowerCorner", srsDimension=str(srs_dim))
    lc.text = lower
    uc = etree.SubElement(env, f"{GML}upperCorner", srsDimension=str(srs_dim))
    uc.text = upper
    return bb


def _populate_building(bldg, core, surfaces, crs, srs_dim):
    """Fill an existing bldg:Building or bldg:BuildingPart element from a core
    dict. Shared by top-level buildings and (recursively) building parts so a
    BuildingPart carries the full AbstractBuilding content."""
    props   = core["props"]
    bldg_id = props["id"]

    # gml:AbstractFeature head: description, name (canonical order)
    if props.get("gml_description"):
        _sub(bldg, f"{GML}description", props["gml_description"])
    if props.get("gml_name"):
        _sub(bldg, f"{GML}name", props["gml_name"])

    # Per-building gml:boundedBy/Envelope — only if the source carried one.
    if props.get("has_bbox_envelope") and \
            props.get("bbox_lower_corner") and props.get("bbox_upper_corner"):
        bb  = _sub(bldg, f"{GML}boundedBy")
        env = _sub(bb, f"{GML}Envelope",
                   srsName=str(crs), srsDimension=str(srs_dim))
        _sub(env, f"{GML}lowerCorner", props["bbox_lower_corner"],
             srsDimension=str(srs_dim))
        _sub(env, f"{GML}upperCorner", props["bbox_upper_corner"],
             srsDimension=str(srs_dim))

    # core:_CityObject: creationDate, relativeToTerrain/Water, externalReference
    if props.get("creation_date"):
        _sub(bldg, f"{CG}creationDate", props["creation_date"])
    if props.get("relative_to_terrain"):
        _sub(bldg, f"{CG}relativeToTerrain", props["relative_to_terrain"])
    if props.get("relative_to_water"):
        _sub(bldg, f"{CG}relativeToWater", props["relative_to_water"])
    # externalReference: prefer the verbatim subtree(s) (lossless, supports
    # multiple); fall back to the reconstructed form for legacy-ingested data.
    if props.get("external_information_system") and not props.get("external_reference_xml"):
        ext_ref = _sub(bldg, f"{CG}externalReference")
        _sub(ext_ref, f"{CG}informationSystem",
             props["external_information_system"])
        ext_obj = _sub(ext_ref, f"{CG}externalObject")
        _sub(ext_obj, f"{CG}name",
             props.get("external_object_name", bldg_id))

    # terminationDate + verbatim metaDataProperty/location/generalizesTo/
    # externalReference/ADE.
    _emit_common_extras(bldg, props)

    # Generic attributes
    _gen_string_attrs(bldg, props, _load_gen_map(props))

    # AbstractBuilding scalars (class, function, usage, year*, roofType, height,
    # storeys) — coded values carry their codeSpace.
    def _coded(tag, val, cs_key):
        el = _sub(bldg, tag, val)
        if props.get(cs_key):
            el.set("codeSpace", props[cs_key])
    if props.get("class_code"):
        _coded(f"{BLDG}class", props["class_code"], "class_codespace")
    if props.get("function_code"):
        _coded(f"{BLDG}function", props["function_code"], "function_codespace")
    if props.get("usage_code"):
        _coded(f"{BLDG}usage", props["usage_code"], "usage_codespace")
    if props.get("year_of_construction"):
        _sub(bldg, f"{BLDG}yearOfConstruction", props["year_of_construction"])
    if props.get("year_of_demolition"):
        _sub(bldg, f"{BLDG}yearOfDemolition", props["year_of_demolition"])
    if props.get("roof_type_code"):
        _coded(f"{BLDG}roofType", props["roof_type_code"], "roof_type_codespace")
    if props.get("measured_height") is not None:
        uom = props.get("measured_height_uom") or "urn:adv:uom:m"
        _sub(bldg, f"{BLDG}measuredHeight",
             str(props["measured_height"]), uom=uom)
    if props.get("storeys_above_ground") is not None:
        _sub(bldg, f"{BLDG}storeysAboveGround",
             str(props["storeys_above_ground"]))
    if props.get("storeys_below_ground") is not None:
        _sub(bldg, f"{BLDG}storeysBelowGround",
             str(props["storeys_below_ground"]))
    if props.get("storey_heights_above_ground"):
        el = _sub(bldg, f"{BLDG}storeyHeightsAboveGround",
                  props["storey_heights_above_ground"])
        if props.get("storey_heights_above_ground_uom"):
            el.set("uom", props["storey_heights_above_ground_uom"])
    if props.get("storey_heights_below_ground"):
        el = _sub(bldg, f"{BLDG}storeyHeightsBelowGround",
                  props["storey_heights_below_ground"])
        if props.get("storey_heights_below_ground_uom"):
            el.set("uom", props["storey_heights_below_ground_uom"])

    # lod0FootPrint / lod0RoofEdge come before the solid (canonical order).
    building_ms = core.get("building_ms", [])

    def _write_building_ms(ms_entry):
        role = ms_entry.get("role") or "multisurface"
        lod  = ms_entry.get("lod") or "0"
        prop = {"footprint": f"{BLDG}lod0FootPrint",
                "roofedge":  f"{BLDG}lod0RoofEdge"}.get(
                    role, f"{BLDG}lod{lod}MultiSurface")
        _write_multisurface(bldg, prop, ms_entry["ms"].get("id"),
                            ms_entry["members"],
                            composite_id=ms_entry["ms"].get("composite_surface_id"))

    for ms_entry in building_ms:
        if (ms_entry.get("role") or "") in ("footprint", "roofedge"):
            _write_building_ms(ms_entry)

    # boundedBy (shell surfaces) — source order places these before lod*Solid
    for surf_data in surfaces:
        _write_bldg_surface(bldg, surf_data)

    for solid in core.get("solids") or []:
        _write_solid(bldg, solid)

    if core.get("lines"):
        ti_el = _sub(bldg, f"{BLDG}lod{core.get('ti_lod', '2')}TerrainIntersection")
        mc    = _sub(ti_el, f"{GML}MultiCurve")
        for ls in core["lines"]:
            cm    = _sub(mc, f"{GML}curveMember")
            ls_el = _sub(cm, f"{GML}LineString")
            _sub(ls_el, f"{GML}posList",
                 ls["pos_list"],
                 srsDimension=str(ls.get("srs_dimension", 3)))

    # lod{N} MultiCurve (annotation/edge curves), grouped by LOD
    for lod_n, lines in core.get("multicurves", []):
        mc_prop = _sub(bldg, f"{BLDG}lod{lod_n}MultiCurve")
        mc      = _sub(mc_prop, f"{GML}MultiCurve")
        for ls in lines:
            cm    = _sub(mc, f"{GML}curveMember")
            ls_el = _sub(cm, f"{GML}LineString")
            _sub(ls_el, f"{GML}posList", ls["pos_list"],
                 srsDimension=str(ls.get("srs_dimension", 3)))

    # lodN MultiSurface attached directly to the building
    for ms_entry in building_ms:
        if (ms_entry.get("role") or "") not in ("footprint", "roofedge"):
            _write_building_ms(ms_entry)

    # outerBuildingInstallation
    for inst in core.get("outer_installations", []):
        _write_installation(bldg, inst, f"{BLDG}outerBuildingInstallation")

    # interiorBuildingInstallation
    for inst in core.get("interior_installations", []):
        _write_installation(bldg, inst, f"{BLDG}interiorBuildingInstallation")

    # interiorRoom → Room
    for room in core.get("rooms", []):
        _write_room(bldg, room)

    # consistsOfBuildingPart → BuildingPart (recursive)
    for part_core in core.get("parts", []):
        _write_building_part(bldg, part_core, crs, srs_dim)

    # bldg:address — re-emit the stored xAL subtree(s) verbatim (lossless).
    # New format: JSON list of fragments (bldg:address is unbounded); legacy
    # graphs may still hold a single raw XML fragment string.
    raw_addr = props.get("address_xml")
    if raw_addr:
        try:
            frags = json.loads(raw_addr)
            if isinstance(frags, str):
                frags = [frags]
        except ValueError:
            frags = [raw_addr]
        for frag in frags:
            try:
                bldg.append(etree.fromstring(frag))
            except etree.XMLSyntaxError:
                pass


def build_building_element(core, surfaces, crs, srs_dim):
    """Build and return a standalone cityObjectMember element for a Building."""
    member = etree.Element(f"{CG}cityObjectMember")
    bldg   = _sub(member, f"{BLDG}Building")
    # Emit the authoritative gml:id: when ingest synthesized a unique node id to
    # avoid a MERGE collapse, source_gml_id holds the original (see
    # ingest_citygml._unique_feature_id) so duplicate-id sources round-trip faithfully.
    bldg.set(f"{GML}id", core["props"].get("source_gml_id") or core["props"]["id"])
    _populate_building(bldg, core, surfaces, crs, srs_dim)
    return member


def _write_building_part(parent_el, core, crs, srs_dim):
    """Emit a bldg:consistsOfBuildingPart/bldg:BuildingPart, populated with the
    full AbstractBuilding content (recursively, incl. nested parts)."""
    bp = _sub(_sub(parent_el, f"{BLDG}consistsOfBuildingPart"), f"{BLDG}BuildingPart")
    bp.set(f"{GML}id", core["props"].get("source_gml_id") or core["props"]["id"])
    _populate_building(bp, core, core.get("surfaces", []), crs, srs_dim)


def _write_feature_scalars(feat_el, props):
    """gml:description, gml:name, boundedBy, creationDate, relativeToTerrain/Water,
    class/function/usage(+codeSpace) — for Room/Furniture/Installation/Opening."""
    if props.get("gml_description"):
        _sub(feat_el, f"{GML}description", props["gml_description"])
    if props.get("gml_name"):
        _sub(feat_el, f"{GML}name", props["gml_name"])
    if props.get("has_bbox_envelope") and props.get("bbox_lower_corner") and \
            props.get("bbox_upper_corner"):
        crs = _EXPORT_CRS.get("crs"); sdim = _EXPORT_CRS.get("srs_dim", 3)
        env = _sub(_sub(feat_el, f"{GML}boundedBy"), f"{GML}Envelope")
        if crs:
            env.set("srsName", str(crs))
        env.set("srsDimension", str(sdim))
        _sub(env, f"{GML}lowerCorner", props["bbox_lower_corner"], srsDimension=str(sdim))
        _sub(env, f"{GML}upperCorner", props["bbox_upper_corner"], srsDimension=str(sdim))
    if props.get("creation_date"):
        _sub(feat_el, f"{CG}creationDate", props["creation_date"])
    if props.get("relative_to_terrain"):
        _sub(feat_el, f"{CG}relativeToTerrain", props["relative_to_terrain"])
    if props.get("relative_to_water"):
        _sub(feat_el, f"{CG}relativeToWater", props["relative_to_water"])
    _gen_string_attrs(feat_el, props, _load_gen_map(props))
    for code_key, tag, cs_key in (
        ("class_code",    f"{BLDG}class",    "class_codespace"),
        ("function_code", f"{BLDG}function", "function_codespace"),
        ("usage_code",    f"{BLDG}usage",    "usage_codespace"),
    ):
        if props.get(code_key):
            el = _sub(feat_el, tag, props[code_key])
            if props.get(cs_key):
                el.set("codeSpace", props[cs_key])
    _emit_common_extras(feat_el, props)


def _write_lod_geometry(feat_el, geom_entry):
    """Write bldg:lod{N}Geometry → gml:MultiSurface for furniture/installation."""
    lod = geom_entry.get("lod", "4")
    _write_multisurface(feat_el, f"{BLDG}lod{lod}Geometry",
                        geom_entry["ms"].get("id"), geom_entry["members"],
                        composite_id=geom_entry["ms"].get("composite_surface_id"))


def _write_furniture(parent_el, furn):
    f_el = _sub(_sub(parent_el, f"{BLDG}interiorFurniture"), f"{BLDG}BuildingFurniture")
    f_el.set(f"{GML}id", furn["props"]["id"])
    _write_feature_scalars(f_el, furn["props"])
    if furn.get("geom"):
        _write_lod_geometry(f_el, furn["geom"])


def _write_installation(parent_el, inst, prop_tag):
    kind  = inst["props"].get("installation_kind", "IntBuildingInstallation")
    i_el  = _sub(_sub(parent_el, prop_tag), f"{BLDG}{kind}")
    i_el.set(f"{GML}id", inst["props"]["id"])
    _write_feature_scalars(i_el, inst["props"])
    for surf_data in inst.get("surfaces", []):
        _write_bldg_surface(i_el, surf_data)
    if inst.get("geom"):
        _write_lod_geometry(i_el, inst["geom"])


def _write_room(parent_el, room):
    r_el = _sub(_sub(parent_el, f"{BLDG}interiorRoom"), f"{BLDG}Room")
    r_el.set(f"{GML}id", room["props"]["id"])
    _write_feature_scalars(r_el, room["props"])
    if room.get("solid"):
        _write_solid(r_el, room["solid"])
    for surf_data in room.get("surfaces", []):
        _write_bldg_surface(r_el, surf_data)
    for furn in room.get("furniture", []):
        _write_furniture(r_el, furn)
    for inst in room.get("installations", []):
        _write_installation(r_el, inst, f"{BLDG}roomInstallation")


def _write_co_surface(parent_el, surf_data, bsurf_ns_uri, crs=None, srs_dim=3):
    """Write a module boundary surface (brid/tun/wtr namespace) with its members."""
    BSURF     = f"{{{bsurf_ns_uri}}}"
    sp        = surf_data["props"]
    surf_type = sp.get("surface_type", "WallSurface")
    lod_n     = str(sp.get("lod", "2"))
    surf_el   = _sub(_sub(parent_el, f"{BSURF}boundedBy"), f"{BSURF}{surf_type}")
    surf_el.set(f"{GML}id", sp["id"])
    if sp.get("gml_description"):
        _sub(surf_el, f"{GML}description", sp["gml_description"])
    if sp.get("gml_name"):
        _sub(surf_el, f"{GML}name", sp["gml_name"])
    if sp.get("has_bbox_envelope") and sp.get("bbox_lower_corner") and \
            sp.get("bbox_upper_corner"):
        env = _sub(_sub(surf_el, f"{GML}boundedBy"), f"{GML}Envelope")
        if crs:
            env.set("srsName", str(crs))
        env.set("srsDimension", str(srs_dim))
        _sub(env, f"{GML}lowerCorner", sp["bbox_lower_corner"], srsDimension=str(srs_dim))
        _sub(env, f"{GML}upperCorner", sp["bbox_upper_corner"], srsDimension=str(srs_dim))
    if sp.get("creation_date"):
        _sub(surf_el, f"{CG}creationDate", sp["creation_date"])
    if sp.get("relative_to_terrain"):
        _sub(surf_el, f"{CG}relativeToTerrain", sp["relative_to_terrain"])
    if sp.get("relative_to_water"):
        _sub(surf_el, f"{CG}relativeToWater", sp["relative_to_water"])
    _gen_string_attrs(surf_el, sp, _load_gen_map(sp))
    if surf_data.get("members"):
        if sp.get("geom_prop") == "Surface":
            # WaterBody WaterSurface/WaterGroundSurface: lodNSurface → CompositeSurface
            prop_el = _sub(surf_el, f"{BSURF}lod{lod_n}Surface")
            cs = _sub(prop_el, f"{GML}CompositeSurface")
            cs.set(f"{GML}id", sp.get("composite_surface_id") or _new_id())
            _write_members(cs, surf_data["members"])
        else:
            _write_multisurface(surf_el, f"{BSURF}lod{lod_n}MultiSurface",
                                sp.get("multi_surface_id"), surf_data["members"],
                                composite_id=sp.get("composite_surface_id"))


def _write_nested_feature(parent_el, nf, ns_uri, crs, srs_dim):
    """Write a Bridge/Tunnel installation or construction element in its
    containment property wrapper (e.g. brid:outerBridgeInstallation)."""
    NB = f"{{{ns_uri}}}"
    p  = nf["props"]
    fe = _sub(_sub(parent_el, f"{NB}{nf['prop']}"), f"{NB}{p['feature_kind']}")
    fe.set(f"{GML}id", p["id"])
    if p.get("gml_description"):
        _sub(fe, f"{GML}description", p["gml_description"])
    if p.get("gml_name"):
        _sub(fe, f"{GML}name", p["gml_name"])
    if p.get("has_bbox_envelope") and p.get("bbox_lower_corner") and \
            p.get("bbox_upper_corner") and crs:
        env = _sub(_sub(fe, f"{GML}boundedBy"), f"{GML}Envelope",
                   srsName=str(crs), srsDimension=str(srs_dim))
        _sub(env, f"{GML}lowerCorner", p["bbox_lower_corner"], srsDimension=str(srs_dim))
        _sub(env, f"{GML}upperCorner", p["bbox_upper_corner"], srsDimension=str(srs_dim))
    if p.get("creation_date"):
        _sub(fe, f"{CG}creationDate", p["creation_date"])
    if p.get("relative_to_terrain"):
        _sub(fe, f"{CG}relativeToTerrain", p["relative_to_terrain"])
    if p.get("relative_to_water"):
        _sub(fe, f"{CG}relativeToWater", p["relative_to_water"])
    _gen_string_attrs(fe, p, _load_gen_map(p))
    for attr in ("class", "function", "usage"):
        if p.get(attr):
            el = _sub(fe, f"{NB}{attr}", p[attr])
            if p.get(f"{attr}_codespace"):
                el.set("codeSpace", p[f"{attr}_codespace"])
    if nf.get("geom"):
        ge = nf["geom"]
        _write_multisurface(fe, f"{NB}lod{ge.get('lod', '3')}Geometry",
                            ge["ms"].get("id"), ge["members"],
                            composite_id=ge["ms"].get("composite_surface_id"))
    for surf_data in nf.get("surfaces", []):
        _write_co_surface(fe, surf_data, ns_uri, crs, srs_dim)


# ---------------------------------------------------------------------------
# CityObject XML element construction
# ---------------------------------------------------------------------------

def attach_prototypes(sess, ms_map):
    """Attach the GeometryPrototype (street-tree pattern: implicit GeometryMultiSurface
    -[:HAS_PROTOTYPE]-> GeometryPrototype) to each implicit MS entry, with its ordered
    face rings, so the template can be written inline once."""
    ids = [e["ms"]["id"] for lst in ms_map.values() for e in lst if e["ms"].get("is_implicit")]
    if not ids:
        return
    protos = {}
    for r in sess.run(
            "MATCH (ms:GeometryMultiSurface)-[:HAS_PROTOTYPE]->(p:GeometryPrototype) "
            "WHERE ms.id IN $ids RETURN ms.id AS ms, p", ids=ids):
        protos[r["ms"]] = dict(r["p"])
    faces = {}
    pids = sorted({p["id"] for p in protos.values()})
    for r in sess.run(
            "MATCH (p:GeometryPrototype)-[m:HAS_SURFACE_MEMBER]->(poly)-[:HAS_EXTERIOR_RING]->(rg) "
            "WHERE p.id IN $ids RETURN p.id AS pid, coalesce(m.order, 0) AS o, poly.id AS poly, "
            "rg AS ring ORDER BY pid, o", ids=pids):
        faces.setdefault(r["pid"], []).append({"poly": r["poly"], "ring": dict(r["ring"])})
    for lst in ms_map.values():
        for e in lst:
            pr = protos.get(e["ms"]["id"])
            if pr is not None:
                e["prototype"] = dict(pr, faces=faces.get(pr["id"], []))


def _write_prototype_inline(rel_geom, proto):
    """A GeometryPrototype's template geometry, inline (TriangulatedSurface or
    MultiSurface), carrying the prototype id so later instances can reference it."""
    if proto.get("geometry_type") == "TriangulatedSurface":
        ts = _sub(rel_geom, f"{GML}TriangulatedSurface")
        ts.set(f"{GML}id", proto["id"])
        if proto.get("srs_name"):
            ts.set("srsName", proto["srs_name"])
        patches = _sub(ts, f"{GML}trianglePatches")
        for f in proto.get("faces", []):
            lr = _sub(_sub(_sub(patches, f"{GML}Triangle"), f"{GML}exterior"), f"{GML}LinearRing")
            if f["ring"].get("id"):
                lr.set(f"{GML}id", f["ring"]["id"])
            _sub(lr, f"{GML}posList", f["ring"].get("pos_list", ""),
                 srsDimension=str(f["ring"].get("srs_dimension", 3)))
        return
    ms = _sub(rel_geom, f"{GML}MultiSurface")
    ms.set(f"{GML}id", proto["id"])
    for f in proto.get("faces", []):
        _write_polygon(ms, {"props": {"id": f["poly"]}, "ext_ring": f["ring"], "int_rings": []})


def query_co_network_lines(sess, co_ids):
    """2.0 tran:lod0Network curves (HAS_LOD_MULTICURVE {role:'network'})."""
    out = {}
    for r in sess.run(
            "MATCH (n:CityObject)-[r:HAS_LOD_MULTICURVE {role: 'network'}]->(ls:GeometryLineString) "
            "WHERE n.id IN $ids RETURN n.id AS id, ls, coalesce(r.order, 0) AS o ORDER BY id, o",
            ids=co_ids):
        out.setdefault(r["id"], []).append(dict(r["ls"]))
    return out


def _write_traffic_area(parent_el, surf_data, prop, crs, srs_dim):
    """2.0 tran:trafficArea/TrafficArea (or auxiliary…) from a BoundarySurface:
    CityObject head, class/function*/usage*/surfaceMaterial, then lodN MultiSurface."""
    sp   = surf_data["props"]
    kind = sp.get("surface_type", "TrafficArea")
    TR   = f"{{{_TRAN_NS}}}"
    el   = _sub(_sub(parent_el, f"{TR}{prop}"), f"{TR}{kind}")
    el.set(f"{GML}id", sp["id"])
    if sp.get("gml_description"):
        _sub(el, f"{GML}description", sp["gml_description"])
    if sp.get("gml_name"):
        _sub(el, f"{GML}name", sp["gml_name"])
    if sp.get("creation_date"):
        _sub(el, f"{CG}creationDate", sp["creation_date"])
    _emit_common_extras(el, sp)
    if sp.get("relative_to_terrain"):
        _sub(el, f"{CG}relativeToTerrain", sp["relative_to_terrain"])
    if sp.get("relative_to_water"):
        _sub(el, f"{CG}relativeToWater", sp["relative_to_water"])
    _gen_string_attrs(el, sp, _load_gen_map(sp))
    for key, local in (("class", "class"), ("function", "function"), ("usage", "usage"),
                       ("surface_material", "surfaceMaterial")):
        vals = sp.get(f"{key}_list") or ([sp[key]] if sp.get(key) else [])
        for v in vals:
            c = _sub(el, f"{TR}{local}", v)
            if sp.get(f"{key}_codespace"):
                c.set("codeSpace", sp[f"{key}_codespace"])
    if surf_data.get("members"):
        _write_multisurface(el, f"{TR}lod{sp.get('lod', '2')}MultiSurface",
                            sp.get("multi_surface_id"), surf_data["members"])


def build_city_object_element(co_id, core, ms_list, surfaces, crs, srs_dim):
    """
    Build a cityObjectMember element for any non-building CityGML feature type.
    co_id    : gml:id string
    core     : {"props": {...}, "solids": [{...}, …], "solid_poly_ids": [...]}
    ms_list  : [{"ms": ms_props, "lod": lod_str, "polys": {pid: poly_data}}, ...]
    surfaces : [surf_data, ...] from query_city_objects_surfaces_batch
    """
    props        = core["props"]
    feature_type = props.get("feature_type", "")

    if feature_type not in CO_MODULE_MAP:
        return None

    ns_uri, scalar_attrs, bsurf_ns_uri, geom_style = CO_MODULE_MAP[feature_type]
    NB = f"{{{ns_uri}}}"   # namespace brace-wrapped

    member  = etree.Element(f"{CG}cityObjectMember")
    feat_el = _sub(member, f"{NB}{feature_type}")
    feat_el.set(f"{GML}id", props.get("source_gml_id") or co_id)

    # gml:AbstractFeature head: description, name
    if props.get("gml_description"):
        _sub(feat_el, f"{GML}description", props["gml_description"])
    if props.get("gml_name"):
        _sub(feat_el, f"{GML}name", props["gml_name"])

    # Per-feature gml:boundedBy/Envelope — only if the source carried one
    if props.get("has_bbox_envelope") and \
            props.get("bbox_lower_corner") and props.get("bbox_upper_corner") and crs:
        bb  = _sub(feat_el, f"{GML}boundedBy")
        env = _sub(bb, f"{GML}Envelope",
                   srsName=str(crs), srsDimension=str(srs_dim))
        _sub(env, f"{GML}lowerCorner", props["bbox_lower_corner"], srsDimension=str(srs_dim))
        _sub(env, f"{GML}upperCorner", props["bbox_upper_corner"], srsDimension=str(srs_dim))

    # core:_CityObject: creationDate, relativeToTerrain/Water, externalReference
    if props.get("creation_date"):
        _sub(feat_el, f"{CG}creationDate", props["creation_date"])
    if props.get("relative_to_terrain"):
        _sub(feat_el, f"{CG}relativeToTerrain", props["relative_to_terrain"])
    if props.get("relative_to_water"):
        _sub(feat_el, f"{CG}relativeToWater", props["relative_to_water"])
    if props.get("external_information_system") and not props.get("external_reference_xml"):
        ext_ref = _sub(feat_el, f"{CG}externalReference")
        _sub(ext_ref, f"{CG}informationSystem", props["external_information_system"])
        ext_obj = _sub(ext_ref, f"{CG}externalObject")
        _sub(ext_obj, f"{CG}name", props.get("external_object_name", co_id))

    # terminationDate + verbatim metaDataProperty/location/generalizesTo/
    # externalReference/ADE — shared by every CityGML feature.
    _emit_common_extras(feat_el, props)

    # gen:*Attribute values
    _gen_string_attrs(feat_el, props, _load_gen_map(props))

    # Module-specific scalar attributes (+ codeSpace where present)
    for prop_key, elem_name in scalar_attrs:
        val = props.get(prop_key)
        if val:
            el = _sub(feat_el, f"{NB}{elem_name}", str(val))
            if props.get(f"{prop_key}_codespace"):
                el.set("codeSpace", props[f"{prop_key}_codespace"])

    # Transportation (2.0-sourced): trafficArea* / auxiliaryTrafficArea* / lod0Network*
    # come after the codes and before the lodN MultiSurfaces.
    if ns_uri == _TRAN_NS:
        for prop, kind in (("trafficArea", "TrafficArea"),
                           ("auxiliaryTrafficArea", "AuxiliaryTrafficArea")):
            for surf_data in surfaces:
                if surf_data["props"].get("surface_type") == kind:
                    _write_traffic_area(feat_el, surf_data, prop, crs, srs_dim)
        if core.get("network"):
            gc = _sub(_sub(feat_el, f"{NB}lod0Network"), f"{GML}CompositeCurve")
            for ls in core["network"]:
                ls_el = _sub(_sub(gc, f"{GML}curveMember"), f"{GML}LineString")
                ls_el.set(f"{GML}id", ls["id"])
                _sub(ls_el, f"{GML}posList", ls["pos_list"],
                     srsDimension=str(ls.get("srs_dimension", 3)))

    # ── Geometry ──────────────────────────────────────────────────────────────

    # Solids (solid_or_surface only) — one lod{N}Solid per stored solid; members
    # re-emit xlink:href / inline / orientable
    if geom_style == "solid_or_surface":
        for solid in core.get("solids") or []:
            lod_n  = str(solid.get("lod", "2"))
            sol_el = _sub(_sub(feat_el, f"{NB}lod{lod_n}Solid"), f"{GML}Solid")
            sol_el.set(f"{GML}id", solid["id"])
            cs = _sub(_sub(sol_el, f"{GML}exterior"), f"{GML}CompositeSurface")
            cs.set(f"{GML}id", solid.get("composite_surface_id") or _new_id())
            _write_members(cs, solid.get("members", []))

    # MultiSurface / Geometry / ImplicitRepresentation
    for ms_entry in ms_list:
        ms_props = ms_entry["ms"]
        lod_n    = str(ms_entry.get("lod") or "2")
        is_impl  = ms_props.get("is_implicit", False)

        if ms_props.get("geometry_type") == "TIN":
            _write_tin(feat_el, ms_entry, crs, srs_dim)
        elif is_impl and geom_style == "geometry":
            # Implicit geometry: lod{N}ImplicitRepresentation → core:ImplicitGeometry
            impl_prop = _sub(feat_el, f"{NB}lod{lod_n}ImplicitRepresentation")
            impl_el   = _sub(impl_prop, f"{CG}ImplicitGeometry")
            mat_str   = ms_props.get("transformation_matrix", "")
            if mat_str:
                _sub(impl_el, f"{CG}transformationMatrix", mat_str)
            rel_geom  = _sub(impl_el, f"{CG}relativeGMLGeometry")
            proto     = ms_props.get("prototype_href")
            gproto    = ms_entry.get("prototype")
            if gproto is not None and gproto["id"] not in _EMITTED_PROTOTYPES:
                # GeometryPrototype (street-tree pattern) not yet in this document:
                # write the template inline once
                _write_prototype_inline(rel_geom, gproto)
                _EMITTED_PROTOTYPES.add(gproto["id"])
            elif proto:
                # referencing instance: the xlink belongs on the geometry PROPERTY
                # (<gml:MultiSurface xlink:href> is invalid GML)
                rel_geom.set(f"{XLINK}href", f"#{proto}")
            else:
                ms_el = _sub(rel_geom, f"{GML}MultiSurface")
                ms_el.set(f"{GML}id", ms_props.get("id") or _new_id())
                _write_members(ms_el, ms_entry["members"])
            ref_str = ms_props.get("reference_point", "")
            if ref_str:
                ref_el = _sub(impl_el, f"{CG}referencePoint")
                pt_el  = _sub(ref_el, f"{GML}Point")
                _sub(pt_el, f"{GML}pos", ref_str)
        else:
            prop_tag = (f"{NB}lod{lod_n}Geometry" if geom_style == "geometry"
                        else f"{NB}lod{lod_n}MultiSurface")
            _write_multisurface(feat_el, prop_tag, ms_props.get("id"),
                                ms_entry["members"],
                                composite_id=ms_props.get("composite_surface_id"))

    # ── Boundary surfaces (WaterBody, Bridge, Tunnel) ─────────────────────────
    if surfaces and bsurf_ns_uri:
        for surf_data in surfaces:
            _write_co_surface(feat_el, surf_data, bsurf_ns_uri, crs, srs_dim)

    # ── Nested sub-features (bridge/tunnel installations & construction) ──────
    for nf in core.get("nested", []):
        _write_nested_feature(feat_el, nf, ns_uri, crs, srs_dim)

    # ── CityObjectGroup membership ────────────────────────────────────────────
    for gm in core.get("group_members", []):
        m = _sub(feat_el, f"{NB}groupMember")
        m.set(f"{XLINK}href", f"#{gm['ref']}")
        if gm.get("role"):
            m.set("role", gm["role"])

    return member


# ---------------------------------------------------------------------------
# Export entry point
# ---------------------------------------------------------------------------

def export(out_path, bbox_wkt=None, dataset=None, ids=None, driver=None):
    """Legacy CityGML 2.0 writer (2.0-sourced data → 2.0).

    out_path : target .gml file
    bbox_wkt : optional WKT polygon (dataset CRS): Buildings intersecting it
    dataset  : optional Dataset id/name/source (default: the only Dataset, or the Dataset
               of the selected ids)
    ids      : optional list of top-level feature ids (Building / CityObject gml:ids as
               stored in the graph); exports exactly those features, with a tight
               envelope around them; overrides bbox_wkt
    driver   : optional open Neo4j driver (default: one from the NEO4J_* environment)
    """
    own_driver = driver is None
    if own_driver:
        driver = GraphDatabase.driver(
            neo4j_uri(),
            auth=neo4j_auth(),
            # UNRECOGNIZED: the queries probe optional structure (Opening, BuildingPart,
            # orientation, …) that a given city may not have
            notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"},
        )
    driver.verify_connectivity()
    print("Connected to Neo4j.")
    _EMITTED_PROTOTYPES.clear()

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with driver.session(database="neo4j") as sess:
        ds      = query_dataset(sess, dataset=dataset, feature_ids=ids)
        if not ds:
            if own_driver:
                driver.close()
            raise SystemExit(f"ERROR: no Dataset found{f' matching {dataset!r}' if dataset else ''}.")
        crs     = ds.get("crs")
        srs_dim = int(ds.get("srs_dimension", 3))
        source  = ds.get("source") or ds.get("name", "")
        _EXPORT_CRS["crs"] = crs
        _EXPORT_CRS["srs_dim"] = srs_dim

        print(
            f"Dataset: {source}  "
            f"CRS: {crs}  "
            f"LOD: {ds.get('lod', 'unknown')}"
        )

        lc_str = ds.get("bbox_lower_corner")
        uc_str = ds.get("bbox_upper_corner")

        if ids:
            bldg_ids, co_ids, missing = split_feature_ids(sess, ids)
            print(f"Selection: {len(bldg_ids)} buildings, {len(co_ids)} city objects"
                  + (f", {len(missing)} id(s) not found" if missing else ""))
            if missing:
                print(f"  not found / not top-level: {missing[:10]}"
                      + (" …" if len(missing) > 10 else ""))
            env = query_selection_envelope(sess, bldg_ids + co_ids, srs_dim)
            if env:
                lc_str, uc_str = env
        else:
            use_spatial = _spatial_ok(sess)
            if bbox_wkt and not use_spatial:
                print("WARNING: Neo4j Spatial unavailable, falling back to full scan.")
            if bbox_wkt:
                print(f"Spatial filter active: {bbox_wkt}")

            bldg_ids = query_all_building_ids(sess, source, bbox_wkt=bbox_wkt,
                                              dataset_id=ds.get("id"),
                                              layer=ds.get("rtree_layer") or "features")
            # With a bbox filter only the intersecting Buildings are exported (as
            # before); CityObjects are not bbox-filtered, so they would otherwise
            # bring in the whole city around a small spatial selection.
            co_ids   = [] if bbox_wkt else query_all_city_object_ids(sess, source, dataset_id=ds.get("id"))

        total_bldg = len(bldg_ids)
        total_co   = len(co_ids)
        print(f"Exporting {total_bldg + total_co} top-level features …")
        progress("export.start", done=0, total=total_bldg + total_co, unit="features")

        exported_bldg  = 0
        exported_co    = 0
        co_type_counts: dict = {}   # feature_type → exported count

        root_attribs = {f"{XSI}schemaLocation": ds.get("schema_location") or SCHEMA_LOCATION}
        root_el = etree.Element(f"{CG}CityModel", nsmap=NSMAP, **root_attribs)

        # CityModel-level gml:description / gml:name (canonical: description, name)
        if ds.get("gml_description"):
            _sub(root_el, f"{GML}description", ds["gml_description"])
        if ds.get("gml_name"):
            _sub(root_el, f"{GML}name", ds["gml_name"])

        if lc_str and uc_str and crs:
            root_el.append(_build_envelope_el(crs, srs_dim, lc_str, uc_str))

        # ── Buildings ─────────────────────────────────────────────────────────
        for batch_start in range(0, total_bldg, EXPORT_BATCH_SIZE):
            batch_ids = bldg_ids[batch_start:batch_start + EXPORT_BATCH_SIZE]
            cores_map = query_buildings_core_batch(sess, batch_ids)
            # Geometry, interior, installations, multicurves + nested BuildingParts.
            _enrich_building_cores(sess, cores_map, "Building", crs, srs_dim)

            for bid in batch_ids:
                core = cores_map.get(bid)
                if core is None:
                    continue
                member = build_building_element(core, core.get("surfaces", []),
                                                crs, srs_dim)
                root_el.append(member)
                exported_bldg += 1

            print(f"  buildings {exported_bldg}/{total_bldg}", flush=True)
            progress("export.features", done=exported_bldg + exported_co,
                     total=total_bldg + total_co, unit="features")

        # ── City objects (all other modules) ──────────────────────────────────
        for batch_start in range(0, total_co, EXPORT_BATCH_SIZE):
            batch_ids  = co_ids[batch_start:batch_start + EXPORT_BATCH_SIZE]
            cores_map  = query_city_objects_core_batch(sess, batch_ids)
            ms_map     = query_city_objects_ms_batch(sess, batch_ids)
            attach_prototypes(sess, ms_map)
            surf_map   = query_city_objects_surfaces_batch(sess, batch_ids)
            net_map    = query_co_network_lines(sess, batch_ids)

            co_solid_index = {}
            for core in cores_map.values():
                for solid in core.get("solids") or []:
                    co_solid_index.setdefault(solid["id"], []).append(solid)
            co_members_map = query_solid_members(sess, list(co_solid_index))
            for sid, solids in co_solid_index.items():
                for s in solids:
                    s["members"] = co_members_map.get(sid, [])

            nested_map = query_nested_features(sess, batch_ids)
            group_ids  = [cid for cid, c in cores_map.items()
                          if c["props"].get("feature_type") == "CityObjectGroup"]
            groups_map = query_group_members(sess, group_ids)

            for co_id in batch_ids:
                core = cores_map.get(co_id)
                if core is None:
                    continue
                core["nested"]        = nested_map.get(co_id, [])
                core["network"]       = net_map.get(co_id, [])
                core["group_members"] = groups_map.get(co_id, [])
                ms_list  = ms_map.get(co_id, [])
                surfaces = surf_map.get(co_id, [])
                member   = build_city_object_element(
                    co_id, core, ms_list, surfaces, crs, srs_dim
                )
                if member is not None:
                    root_el.append(member)
                    exported_co += 1
                    ft = core["props"].get("feature_type", "Unknown")
                    co_type_counts[ft] = co_type_counts.get(ft, 0) + 1

            print(f"  city objects {exported_co}/{total_co}", flush=True)
            progress("export.features", done=exported_bldg + exported_co,
                     total=total_bldg + total_co, unit="features")

        etree.indent(root_el, space="  ")
        out_path.write_bytes(
            etree.tostring(root_el, pretty_print=True,
                           xml_declaration=True, encoding="UTF-8")
        )

    if own_driver:
        driver.close()

    size_kb = out_path.stat().st_size // 1024
    all_type_counts = {"Building": exported_bldg, **co_type_counts}
    print(f"Written: {out_path} ({size_kb:,} KB)")
    print(f"  Top-level features exported: {exported_bldg + exported_co}")
    for _ft, _n in sorted(all_type_counts.items()):
        if _n > 0:
            print(f"    {_ft}: {_n}")
    progress("export.done", done=exported_bldg + exported_co,
             total=exported_bldg + exported_co, unit="features",
             out=str(out_path), by_type=all_type_counts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_ID_HEADERS = {"id", "gml_id", "building_id", "feature_id"}


def _read_ids_file(path: Path) -> list[str]:
    """Ids from a .json list, or one per line (first column of a CSV/TSV;
    a header row named id/gml_id/building_id/feature_id is skipped)."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return [str(x) for x in json.loads(text)]
    ids = []
    for line in text.splitlines():
        field = line.replace(";", ",").replace("\t", ",").split(",")[0].strip().strip('"')
        if field and field.lower() not in _ID_HEADERS:
            ids.append(field)
    return ids


def _bbox_to_wkt(bbox: str) -> str:
    """A WKT polygon as given, or minx,miny,maxx,maxy → WKT polygon."""
    if bbox.strip().upper().startswith("POLYGON"):
        return bbox.strip()
    minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    return (f"POLYGON(({minx} {miny}, {maxx} {miny}, {maxx} {maxy}, "
            f"{minx} {maxy}, {minx} {miny}))")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Export a pykci Dataset from Neo4j as CityGML 2.0 (default) or 3.0. "
                    "2.0-sourced data → 2.0 uses the legacy writer (unchanged output); every "
                    "other combination uses the strict profile-driven writer "
                    "(citygml_writer.py), which writes the XSD content-model order and a loss "
                    "report for whatever the target version cannot express.")
    ap.add_argument("--version", default="2", choices=["2", "3", "2.0", "3.0"],
                    help="target CityGML version (default 2)")
    ap.add_argument("--dataset", default=None,
                    help="Dataset id, name or source (required when the graph holds several)")
    ap.add_argument("--out", default=None,
                    help="output .gml (default output/citygml/<stem>_export[_v3].gml)")
    ap.add_argument("--bbox", default=None,
                    help="WKT POLYGON or minx,miny,maxx,maxy in the dataset CRS: only features "
                         "whose R-tree bbox intersects it (layer = Dataset.rtree_layer or 'features')")
    ap.add_argument("--ids", default=None,
                    help="Comma-separated top-level feature ids (Building/CityObject); "
                         "export exactly these features")
    ap.add_argument("--ids-file", default=None,
                    help="File with feature ids: .json list, or one per line / first CSV column")
    ap.add_argument("--clip", default=None, action=argparse.BooleanOptionalAction,
                    help="with --bbox (default on): write only the transportation sub-features "
                         "(Sections, Intersections, spaces, areas, markings, …) whose bbox "
                         "meets the box; a Road/Section/space is written when a descendant "
                         "is kept. --no-clip writes every selected feature whole (strict writer)")
    ap.add_argument("--profile", action="store_true",
                    help="print elapsed time and peak RSS after each export phase")
    ap.add_argument("--batch", type=int, default=200,
                    help="top-level features loaded and streamed per batch (strict writer)")
    ap.add_argument("--writer", default="auto", choices=["auto", "legacy", "strict"],
                    help="auto: legacy for 2.0-sourced → 2.0, strict otherwise")
    ap.add_argument("--v2-subfeatures", default="drop", choices=["drop", "genattrs"],
                    help="3.0 → 2.0: Sections/Intersections/TrafficSpaces/Markings/relations "
                         "are dropped (loss report) or kept as gen:stringAttributes "
                         "(citygml3_*) on the traffic areas / the road")
    ap.add_argument("--loss-report", default=None,
                    help="loss-report JSON (default <out>.loss.json; strict writer only)")
    ap.add_argument("--uri", default=NEO4J_URI)
    ap.add_argument("--user", default=NEO4J_USER)
    ap.add_argument("--password", default=NEO4J_PASSWORD)
    args = ap.parse_args(argv)

    sel_ids = []
    if args.ids:
        sel_ids += [i.strip() for i in args.ids.split(",") if i.strip()]
    if args.ids_file:
        sel_ids += _read_ids_file(Path(args.ids_file))
    sel_ids = list(dict.fromkeys(sel_ids))          # de-duplicate, keep order
    if (args.ids or args.ids_file) and not sel_ids:
        sys.exit("ERROR: --ids/--ids-file given but no ids found.")
    bbox_wkt = _bbox_to_wkt(args.bbox) if args.bbox else None

    target = "3.0" if args.version.startswith("3") else "2.0"
    drv = GraphDatabase.driver(args.uri, auth=(args.user, args.password),
                               notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"})
    drv.verify_connectivity()
    with drv.session(database="neo4j") as sess:
        ds = query_dataset(sess, args.dataset, feature_ids=sel_ids or None)
    if not ds:
        raise SystemExit("ERROR: no Dataset in the graph.")
    src_version = str(ds.get("citygml_version") or
                      ("3.0" if "3.0" in str(ds.get("format", "")) else "2.0"))
    writer = args.writer
    if writer == "auto":
        writer = "legacy" if (target == "2.0" and src_version.startswith("2")) else "strict"
    stem = Path(ds.get("source") or ds.get("name") or "dataset").stem
    sel = "_selection" if (sel_ids or bbox_wkt) else ""
    out = Path(args.out) if args.out else \
        Path("output/citygml") / (stem + sel + ("_export_v3.gml" if target == "3.0" else "_export.gml"))
    print(f"Dataset {ds.get('id')} (CityGML {src_version}) → CityGML {target} "
          f"[{writer} writer] → {out}")
    if writer == "legacy":
        if target == "3.0":
            raise SystemExit("ERROR: the legacy writer writes CityGML 2.0 only.")
        export(out, bbox_wkt, dataset=ds.get("id"), ids=sel_ids or None, driver=drv)
    else:
        import time
        from citygml_writer import export_document
        t0 = time.perf_counter()
        with drv.session(database="neo4j") as sess:
            counts, loss = export_document(sess, ds, target, out,
                                           {"v2_subfeatures": args.v2_subfeatures},
                                           feature_ids=sel_ids or None,
                                           bbox_wkt=None if sel_ids else bbox_wkt, clip=args.clip,
                                           batch=args.batch)
        rep = Path(args.loss_report) if args.loss_report else out.with_suffix(".loss.json")
        rep.write_text(json.dumps({"dataset": ds.get("id"), "source_version": src_version,
                                   "target_version": target, "writer": writer,
                                   "written": counts, "loss": loss}, indent=2,
                                  ensure_ascii=False))
        print(f"Written: {out} ({out.stat().st_size // 1024:,} KB) in "
              f"{time.perf_counter() - t0:.1f}s")
        for k, v in sorted(counts.items()):
            print(f"    {k}: {v}")
        print(f"  Loss report ({loss['total']} items): {rep}")
        for k, v in sorted(loss["by_category"].items(), key=lambda kv: -kv[1])[:25]:
            print(f"    {v:>7}  {k}")
        for k, v in sorted(loss.get("export_stats", {}).items()):
            print(f"  {k}: {v:,}")
        if args.profile:
            for ph in loss.get("profile", []):
                rss = ph['peak_rss_mb'] if ph['peak_rss_mb'] is not None else '?'
                print(f"  [profile] {ph['t_s']:>8.2f} s  peak RSS {rss:>6} MB  {ph['phase']}")
    drv.close()
    print("Done.")


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):   # a cp1252 console must not crash on non-ASCII output
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    main()
