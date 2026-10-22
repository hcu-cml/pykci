"""
citygml_writer.py — strict, profile-driven CityGML writer (3.0 and 2.0) for pykci.

Used by export_citygml.py for every export that the legacy 2.0 writer cannot do
faithfully:  3.0-sourced → 3.0,  3.0-sourced → 2.0 (flattening + loss report) and
2.0-sourced → 3.0 (synthesised traffic spaces). 2.0-sourced → 2.0 stays on the legacy
writer (byte-identical output to before).

Design
  * One generic graph loader (GraphTree) pulls a feature's owned subtree by following
    containment edges (HAS_SECTION, HAS_TRAFFIC_SPACE, HAS_BOUNDARY, HAS_POLYGON,
    HAS_LOD_*, HAS_SURFACE_MEMBER, …) and records reference edges (RELATED_TO,
    PREDECESSOR/SUCCESSOR, HAS_PROTOTYPE, TARGETS) without expanding them; xlinked
    members are recorded, not expanded.
  * Every element is written in the strict xs:sequence order of the target version's
    content model (derived from the CityGML 2.0 and 3.0 XSDs), block by block:
    gml head → boundedBy → lifespan → CityObject block → Space / ThematicSurface block
    → PhysicalSpace → OccupiedSpace → construction block → module properties.
  * Namespaces / structural names come from citygml_profiles (PROFILE_V2 / PROFILE_V3).
  * gml:id uniqueness is enforced (a polygon met twice is written once, then
    referenced); ids synthesised at ingest (synthetic_id) are not written.
  * Implicit-geometry templates (GeometryPrototype) are written inline at their first
    use (preferring the instance that defined them inline) and referenced afterwards.
  * Everything the target version cannot express is counted in a loss report
    (category → count, with examples) instead of being dropped silently.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from lxml import etree

from citygml_profiles import PROFILE_V2, PROFILE_V3, XLINK_NS, XSI_NS, href_fragment

XLINK_HREF = f"{{{XLINK_NS}}}href"

# Containment (owned) edges are expanded; reference edges are only recorded.
OWN_RELS = (
    "HAS_SECTION", "HAS_INTERSECTION", "HAS_TRAFFIC_SPACE", "HAS_MARKING", "HAS_HOLE",
    "HAS_CLEARANCE_SPACE", "HAS_BOUNDARY", "HAS_OPENING", "HAS_BUILDING_PART",
    "HAS_BRIDGE_PART", "HAS_NESTED", "HAS_OUTER_INSTALLATION", "HAS_INTERIOR_INSTALLATION",
    "HAS_INTERIOR_ROOM", "HAS_ROOM_INSTALLATION", "HAS_INTERIOR_FURNITURE", "HAS_LOD_SOLID",
    "HAS_LOD_SURFACE", "HAS_LOD_GEOMETRY", "HAS_LOD_MULTICURVE", "HAS_LOD_POINT",
    "HAS_TERRAIN_INTERSECTION", "HAS_LINE", "HAS_SURFACE_MEMBER", "HAS_SOLID_MEMBER",
    "HAS_POLYGON", "HAS_EXTERIOR_RING", "HAS_INTERIOR_RING", "HAS_APPEARANCE",
    "HAS_SURFACE_DATA", "HAS_DYNAMIZER",
)
REF_RELS = ("RELATED_TO", "PREDECESSOR", "SUCCESSOR", "TARGETS", "HAS_PROTOTYPE",
            "HAS_GROUP_MEMBER")
_ALL_RELS = list(OWN_RELS + REF_RELS)

_BLDG_SURF = {"WallSurface", "RoofSurface", "GroundSurface", "FloorSurface",
              "OuterFloorSurface", "OuterCeilingSurface", "InteriorWallSurface",
              "CeilingSurface", "ClosureSurface"}


class _Ref:
    """Target of a still pending cross-file reference (not in the graph yet)."""
    partial = True
    labels = frozenset()

    def __init__(self, tid):
        self.id = tid


def refs_with_pending(node, rel):
    """Resolved reference edges of type `rel` plus the node's parked (pending)
    references of that type (PendingRefs, ingest_citygml.resolve_pending_refs), in
    document order; pending ones carry their verbatim cross-document href."""
    out = list(node.kids(rel))
    for ref in _loads(node.p.get("pending_refs_json"), []):
        if ref.get("rel") == rel:
            out.append((dict(ref.get("props") or {}, order=(ref.get("key") or {}).get("order", 0)),
                        _Ref(ref.get("tgt"))))
    out.sort(key=lambda x: _num(x[0].get("order")))
    return out


class Node:
    __slots__ = ("eid", "labels", "p", "out", "partial", "expanded")

    def __init__(self, eid, labels, props, partial=False):
        self.eid, self.labels, self.p, self.out, self.partial = eid, set(labels), dict(props), [], partial
        self.expanded = False

    @property
    def id(self):
        return self.p.get("id")

    def kids(self, *rels):
        """[(rel_props, child)] of the given edge types, in document order."""
        out = [(rp, c) for t, rp, c in self.out if t in rels]
        out.sort(key=lambda x: (_num(x[0].get("order")), str(x[1].id)))
        return out

    def first(self, *rels):
        k = self.kids(*rels)
        return k[0][1] if k else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --clip: children of transportation spaces are only loaded when their bbox
# (sub-feature / surface bbox_min/max, see GRAPH.md) intersects the query box
TRANSPORT_LABELS = {"Road", "Railway", "Track", "Square", "Waterway", "Section", "Intersection",
                    "TrafficSpace", "AuxiliaryTrafficSpace", "Hole", "ClearanceSpace"}
TRANSPORT_TOP = {"Road", "Railway", "Track", "Square", "Waterway", "TransportationComplex"}
CLIP_RELS = {"HAS_SECTION", "HAS_INTERSECTION", "HAS_TRAFFIC_SPACE", "HAS_MARKING", "HAS_HOLE",
             "HAS_CLEARANCE_SPACE", "HAS_BOUNDARY"}
FEATURE_RELS = ("HAS_SECTION", "HAS_INTERSECTION", "HAS_TRAFFIC_SPACE", "HAS_MARKING", "HAS_HOLE",
                "HAS_CLEARANCE_SPACE", "HAS_BOUNDARY", "HAS_OPENING", "HAS_NESTED",
                "HAS_BRIDGE_PART", "HAS_BUILDING_PART", "HAS_OUTER_INSTALLATION",
                "HAS_INTERIOR_INSTALLATION", "HAS_INTERIOR_ROOM")


def bbox_outside(props, box):
    """True / False when the node's bbox misses / meets box (minx, miny, maxx, maxy);
    None when the node has no bbox (it is then kept — conservative)."""
    if props.get("bbox_min_x") is None or props.get("bbox_max_x") is None:
        return None
    return (props["bbox_max_x"] < box[0] or props["bbox_min_x"] > box[2] or
            props["bbox_max_y"] < box[1] or props["bbox_min_y"] > box[3])


class GraphTree:
    """Loads owned subtrees from Neo4j into Node objects (shared across one batch so a
    node reached twice is one object). With clip_box, children of transportation spaces
    whose bbox misses the box are skipped before they are expanded; with plan (the ids
    the document will contain), an xlinked MultiSurface whose owner is not in the
    document is loaded and written inline instead of as a dangling reference."""

    def __init__(self, sess, clip_box=None, plan=None):
        self.sess = sess
        self.nodes: dict = {}
        self.clip_box = clip_box
        self.plan = plan
        self.clipped = 0
        self.unclippable = 0

    def load(self, ids):
        tops = []
        new = []
        for r in self.sess.run("UNWIND $ids AS i MATCH (n:Entity {id: i}) "
                               "RETURN elementId(n) AS e, labels(n) AS l, properties(n) AS p",
                               ids=list(ids)):
            n = self.nodes.get(r["e"])
            if n is None:
                n = self.nodes[r["e"]] = Node(r["e"], r["l"], r["p"])
            elif n.partial:                 # known so far only as a reference target
                n.p, n.partial = dict(r["p"]), False
            if not n.expanded:
                n.expanded = True
                new.append(r["e"])
            tops.append(n)
        self._expand(new)
        return tops

    _EXPAND_Q = (
        "UNWIND $eids AS e MATCH (a) WHERE elementId(a) = e "
        "MATCH (a)-[r:" + "|".join(OWN_RELS + REF_RELS) + "]->(b) "
        # clip test in the database: a child of a transportation space whose bbox misses
        # the box comes back as an id-less marker row (counted, never expanded)
        "WITH e, a, r, b, ($box IS NOT NULL AND type(r) IN $clip "
        "     AND any(l IN labels(a) WHERE l IN $tlabels) "
        "     AND b.bbox_min_x IS NOT NULL AND (b.bbox_max_x < $box[0] OR b.bbox_min_x > $box[2] "
        "     OR b.bbox_max_y < $box[1] OR b.bbox_min_y > $box[3])) AS outside "
        "RETURN e AS a, type(r) AS t, outside, "
        "CASE WHEN outside THEN null ELSE properties(r) END AS rp, elementId(b) AS b, "
        "CASE WHEN outside THEN null ELSE labels(b) END AS bl, "
        "CASE WHEN outside THEN null WHEN type(r) IN $refs THEN {id: b.id} "
        "ELSE properties(b) END AS bp")

    def _expand(self, frontier):
        box = list(self.clip_box) if self.clip_box is not None else None
        while frontier:
            nxt = []
            for i in range(0, len(frontier), 2000):
                chunk = frontier[i:i + 2000]
                rows = self.sess.run(self._EXPAND_Q, eids=chunk, refs=list(REF_RELS),
                                     box=box, clip=list(CLIP_RELS),
                                     tlabels=list(TRANSPORT_LABELS))
                for row in rows:
                    if row["outside"]:
                        self.clipped += 1
                        continue
                    t, rp = row["t"], row["rp"]
                    if self.clip_box is not None and t in CLIP_RELS and \
                            self.nodes[row["a"]].labels & TRANSPORT_LABELS and \
                            row["bp"].get("bbox_min_x") is None:
                        self.unclippable += 1
                    if self.plan is not None and t == "HAS_LOD_SURFACE" and rp.get("via_xlink") \
                            and row["bp"].get("id") not in self.plan:
                        rp = dict(rp, via_xlink=False)   # owner not exported: write inline
                    is_ref = t in REF_RELS or bool(rp.get("via_xlink")) or \
                        (t == "HAS_LOD_SURFACE" and rp.get("role") == "primary")
                    child = self.nodes.get(row["b"])
                    if child is None:
                        child = self.nodes[row["b"]] = Node(row["b"], row["bl"], row["bp"],
                                                            partial=t in REF_RELS)
                    elif child.partial and t not in REF_RELS:
                        child.p = dict(row["bp"])        # now known with all properties
                        child.partial = False
                    if not is_ref and not child.expanded:
                        # owned content (possibly met before through an xlink): expand once
                        child.expanded = True
                        nxt.append(row["b"])
                    self.nodes[row["a"]].out.append((t, rp, child))
            frontier = nxt


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def _frag_ns(frag):
    m = re.match(r"\s*<([A-Za-z_][\w.-]*:)?[\w.-]+[^>]*?\sxmlns(?::\w+)?=\"([^\"]+)\"", frag or "")
    try:
        return etree.QName(etree.fromstring(frag)).namespace
    except Exception:
        return m.group(2) if m else None


class LossReport:
    def __init__(self):
        self.counts = Counter()
        self.examples = defaultdict(list)

    def add(self, cat, example=None, n=1):
        self.counts[cat] += n
        if example is not None and len(self.examples[cat]) < 5:
            self.examples[cat].append(str(example))

    def as_dict(self):
        return {"total": sum(self.counts.values()),
                "by_category": dict(sorted(self.counts.items())),
                "examples": {k: v for k, v in sorted(self.examples.items())}}


class BaseWriter:
    """Shared XML + geometry emission for both serializers."""

    def __init__(self, profile, dataset, opts):
        self.P = profile
        self.v3 = profile.version == "3.0"
        self.ds = dataset
        self.crs = dataset.get("crs")
        self.srs_dim = int(dataset.get("srs_dimension") or 3)
        self.opts = opts
        self.loss = LossReport()
        self.written: set = set()          # gml:ids written in this document
        self.hrefs: list = []              # (href id, context) written as references
        self.proto_done: set = set()
        self.proto_nodes: dict = {}        # prototype id → Node
        self.doc_ids: set = set()          # feature-level ids this document will contain
        self.proto_ids: set = set()
        self.clip = bool(opts.get("clip"))
        self._content: dict = {}
        self.stats = Counter()

    # -- clip support -----------------------------------------------------------
    _GEOM_RELS = ("HAS_POLYGON", "HAS_LOD_SURFACE", "HAS_LOD_SOLID", "HAS_LOD_MULTICURVE",
                  "HAS_LOD_POINT", "HAS_PROTOTYPE", "HAS_TERRAIN_INTERSECTION", "HAS_LOD_GEOMETRY")
    _SUB_RELS = ("HAS_SECTION", "HAS_INTERSECTION", "HAS_TRAFFIC_SPACE", "HAS_MARKING",
                 "HAS_HOLE", "HAS_CLEARANCE_SPACE", "HAS_BOUNDARY")

    def has_content(self, n):
        """A node has content when it carries geometry or a kept child with content."""
        r = self._content.get(n.eid)
        if r is None:
            r = any((t in self._GEOM_RELS and rp.get("role") != "primary") or
                    (t in self._SUB_RELS and self.has_content(c)) for t, rp, c in n.out)
            self._content[n.eid] = r
        return r

    def keep(self, c):
        """With --clip, containers whose children were all clipped away are pruned."""
        return not self.clip or self.has_content(c)

    def clipped_away(self, n):
        """A top-level transportation feature with no kept sub-feature is not written."""
        if not self.clip or n.p.get("feature_type") not in TRANSPORT_TOP:
            return False
        if any(t in self._SUB_RELS and self.has_content(c) for t, _rp, c in n.out):
            return False
        self.stats["transportation features without kept sub-features"] += 1
        return True

    # -- basics ---------------------------------------------------------------
    def E(self, parent, prefix, local, text=None, **attrs):
        el = etree.SubElement(parent, self.P.q(prefix, local))
        for k, v in attrs.items():
            if v is not None:
                el.set(k, str(v))
        if text is not None:
            el.text = str(text)
        return el

    def set_id(self, el, gid, synthetic=False):
        """Write gml:id unless synthesised at ingest; returns False if the id was already
        written (the caller should then reference instead)."""
        if not gid or synthetic:
            return True
        if gid in self.written:
            return False
        self.written.add(gid)
        el.set(self.P.gml_id, str(gid))
        return True

    def href(self, el, target, ctx=""):
        el.set(XLINK_HREF, f"#{target}")
        self.hrefs.append((target, ctx))

    def code(self, parent, prefix, local, value, cs=None):
        if value is None or value == "":
            return None
        return self.E(parent, prefix, local, value, codeSpace=cs)

    def codes(self, parent, prefix, local, p, key):
        vals = p.get(f"{key}_list") or ([p[key]] if p.get(key) not in (None, "") else [])
        css = p.get(f"{key}_codespace_list") or [p.get(f"{key}_codespace")] * len(vals)
        for v, cs in zip(vals, css):
            self.code(parent, prefix, local, v, cs or None)

    def measure(self, parent, prefix, local, p, key, default_uom="m"):
        v = p.get(key)
        if v in (None, ""):
            return
        self.E(parent, prefix, local, v, uom=p.get(f"{key}_uom") or default_uom)

    def envelope(self, parent, lower, upper):
        bb = self.E(parent, "gml", "boundedBy")
        env = self.E(bb, "gml", "Envelope", srsName=self.crs, srsDimension=self.srs_dim)
        self.E(env, "gml", "lowerCorner", lower)
        self.E(env, "gml", "upperCorner", upper)

    def verbatim(self, parent, frag, cat):
        """Re-append a stored verbatim XML fragment only if it is in this version's
        namespaces; otherwise count it as a loss."""
        ns = _frag_ns(frag)
        if ns in self.P.known_namespaces():
            try:
                parent.append(etree.fromstring(frag))
                return True
            except etree.XMLSyntaxError:
                pass
        self.loss.add(cat)
        return False

    # -- geometry -------------------------------------------------------------
    def poslist(self, parent, ring_or_ls):
        rp = ring_or_ls.p
        self.E(parent, "gml", "posList", rp.get("pos_list", ""),
               srsDimension=rp.get("srs_dimension") or 3)

    def ring(self, parent, ring):
        lr = self.E(parent, "gml", "LinearRing")
        self.set_id(lr, ring.id, ring.p.get("synthetic_id"))
        self.poslist(lr, ring)

    def polygon(self, parent, poly):
        pel = self.E(parent, "gml", "Polygon")
        if not self.set_id(pel, poly.id, poly.p.get("synthetic_id")):
            return None
        if poly.p.get("srs_name"):
            pel.set("srsName", poly.p["srs_name"])
        ext = poly.first("HAS_EXTERIOR_RING")
        if ext is not None:
            self.ring(self.E(pel, "gml", "exterior"), ext)
        ints = [(rp, r) for t, rp, r in poly.out if t == "HAS_INTERIOR_RING"]
        ints.sort(key=lambda x: _num(x[0].get("ring_index")))
        for _rp, r in ints:
            self.ring(self.E(pel, "gml", "interior"), r)
        return pel

    def member(self, container, rp, target, member_local="surfaceMember"):
        """One surfaceMember: xlink, OrientableSurface wrapper, inline Polygon or
        CompositeSurface; a polygon already written in this document becomes an xlink."""
        orient = rp.get("orientation")
        sm = self.E(container, "gml", member_local)
        holder = sm
        if orient:
            os_ = self.E(sm, "gml", "OrientableSurface", orientation=orient)
            holder = self.E(os_, "gml", "baseSurface")
        if rp.get("via_xlink") or target.partial or target.id in self.written:
            self.href(holder, target.id, "surfaceMember")
            return
        if "GeometryCompositeSurface" in target.labels:
            cs = self.E(holder, "gml", "CompositeSurface")
            self.set_id(cs, target.id, target.p.get("synthetic_id"))
            for crp, c in target.kids("HAS_SURFACE_MEMBER"):
                self.member(cs, crp, c)
            return
        tmp = etree.Element("tmp")
        pel = self.polygon(tmp, target)
        if pel is None:
            self.href(holder, target.id, "surfaceMember")
        else:
            holder.append(pel)

    def multisurface(self, prop_parent, prefix, local, ms_nodes=None, members=None,
                     ms_id=None, synthetic=False, composite_id=None):
        """<prefix:local><gml:MultiSurface>…; ms_nodes (merged when several, e.g. a space
        continued in another tile) or an explicit member list."""
        prop = self.E(prop_parent, prefix, local)
        ms = self.E(prop, "gml", "MultiSurface")
        if ms_nodes:
            first = ms_nodes[0]
            self.set_id(ms, first.id, first.p.get("synthetic_id"))
            if first.p.get("srs_name"):
                ms.set("srsName", first.p["srs_name"])
            members = [m for n in ms_nodes for m in n.kids("HAS_SURFACE_MEMBER")]
            composite_id = composite_id or first.p.get("composite_surface_id")
        else:
            self.set_id(ms, ms_id, synthetic)
        holder = ms
        if composite_id and composite_id not in self.written:
            cs = self.E(self.E(ms, "gml", "surfaceMember"), "gml", "CompositeSurface")
            self.set_id(cs, composite_id)
            holder = cs
        for rp, t in members or []:
            if "GeometryCompositeSurface" in t.labels and t.id == composite_id:
                for crp, c in t.kids("HAS_SURFACE_MEMBER"):
                    self.member(holder, crp, c)
                continue
            self.member(holder, rp, t)
        if not len(holder):
            prop_parent.remove(prop)
            return None
        return prop

    def solid(self, prop_parent, prefix, local, snode):
        prop = self.E(prop_parent, prefix, local)
        self._solid_into(prop, snode)
        return prop

    def _solid_into(self, holder, snode):
        gt = snode.p.get("geometry_type")
        if gt in ("CompositeSolid", "MultiSolid"):
            el = self.E(holder, "gml", gt)
            self.set_id(el, snode.id, snode.p.get("synthetic_id"))
            for rp, c in snode.kids("HAS_SOLID_MEMBER"):
                sm = self.E(el, "gml", "solidMember")
                if rp.get("via_xlink") or c.id in self.written or c.partial:
                    self.href(sm, c.id, "solidMember")
                else:
                    self._solid_into(sm, c)
            return el
        el = self.E(holder, "gml", "Solid")
        self.set_id(el, snode.id, snode.p.get("synthetic_id"))
        ext = self.E(el, "gml", "exterior")
        shell = self.E(ext, "gml", self.P.solid_exterior_local)
        cs_id = snode.p.get("composite_surface_id")
        if cs_id and cs_id not in self.written:
            self.set_id(shell, cs_id)
        for rp, c in snode.kids("HAS_SURFACE_MEMBER"):
            self.member(shell, rp, c)
        return el

    def multicurve(self, prop_parent, prefix, local, lines, mc_id=None):
        prop = self.E(prop_parent, prefix, local)
        mc = self.E(prop, "gml", "MultiCurve")
        if mc_id and mc_id not in self.written:
            self.set_id(mc, mc_id)
        for ls in lines:
            cm = self.E(mc, "gml", "curveMember")
            if ls.id in self.written:
                self.href(cm, ls.id, "curveMember")
                continue
            le = self.E(cm, "gml", "LineString")
            self.set_id(le, ls.id, ls.p.get("synthetic_id"))
            self.poslist(le, ls)
        return prop

    def point_into(self, holder, pos, pid=None, dim=None):
        pt = self.E(holder, "gml", "Point", srsDimension=dim or 3)
        if pid:
            self.set_id(pt, pid)
        self.E(pt, "gml", "pos", pos)
        return pt

    # -- generic attributes ---------------------------------------------------
    _T3 = {"string": "StringAttribute", "int": "IntAttribute", "double": "DoubleAttribute",
           "date": "DateAttribute", "uri": "UriAttribute", "measure": "MeasureAttribute",
           "code": "CodeAttribute"}

    def gen_attrs(self, parent, p, extra=None):
        """Generic attributes from props + _gen_attrs_map (types, sets, uom, codeSpace),
        in the target version's encoding; `extra` = [(name, value)] appended as strings
        (used for the 2.0 'genattrs' flattening policy)."""
        gmap = _loads(p.get("_gen_attrs_map"), {})
        sets = {}

        def container(path):
            if not path:
                return parent
            key = tuple((e.get("name", ""), e.get("i", 0), e.get("codeSpace")) for e in path)
            if key in sets:
                return sets[key]
            up = container(path[:-1])
            leaf = path[-1]
            if self.v3:
                wrap = self.E(up, "core" if up is parent else "gen",
                              "genericAttribute")
                s = self.E(wrap, "gen", "GenericAttributeSet")
                self.E(s, "gen", "name", leaf.get("name", ""))
                if leaf.get("codeSpace"):
                    self.E(s, "gen", "codeSpace", leaf["codeSpace"])
            else:
                s = self.E(up, "gen", "genericAttributeSet", name=leaf.get("name", ""),
                           codeSpace=leaf.get("codeSpace"))
            sets[key] = s
            return s

        def emit(cont, name, gtype, value, uom=None, cs=None, top=False):
            if self.v3:
                wrap = self.E(cont, "core" if top else "gen", "genericAttribute")
                a = self.E(wrap, "gen", self._T3.get(gtype, "StringAttribute"))
                self.E(a, "gen", "name", name)
                v = self.E(a, "gen", "value", value)
                if gtype == "measure":
                    v.set("uom", uom or "m")
                if gtype == "code" and cs:
                    v.set("codeSpace", cs)
            else:
                t2 = gtype if gtype in ("string", "int", "double", "date", "uri", "measure") else "string"
                if gtype == "code":
                    self.loss.add("gen:CodeAttribute → 2.0 stringAttribute (codeSpace lost)", name)
                a = self.E(cont, "gen", f"{t2}Attribute", name=name)
                v = self.E(a, "gen", "value", value)
                if t2 == "measure":
                    v.set("uom", uom or "m")

        for key, meta in gmap.items():
            if key not in p:
                continue
            if not isinstance(meta, dict):
                meta = {"name": meta, "type": "string"}
            path = meta.get("set") or []
            cont = container(path)
            emit(cont, meta.get("name", key), meta.get("type", "string"), p[key],
                 uom=p.get(f"{key}_uom"), cs=meta.get("codeSpace"), top=not path)
        for name, value in extra or []:
            emit(parent, name, "string", value, top=True)

    # -- prototypes -----------------------------------------------------------
    def prototype_geometry(self, holder, proto):
        """Write a GeometryPrototype's template geometry inline (first use)."""
        gt = proto.p.get("geometry_type")
        pid = proto.id
        synth = proto.p.get("synthetic_id")
        if gt in ("MultiSurface", "CompositeSurface", None):
            el = self.E(holder, "gml", gt or "MultiSurface")
            self.set_id(el, pid, synth)
            for rp, c in proto.kids("HAS_SURFACE_MEMBER"):
                self.member(el, rp, c)
        elif gt == "Solid":
            el = self.E(holder, "gml", "Solid")
            self.set_id(el, pid, synth)
            shell = self.E(self.E(el, "gml", "exterior"), "gml", self.P.solid_exterior_local)
            if proto.p.get("shell_id"):
                self.set_id(shell, proto.p["shell_id"])
            for rp, c in proto.kids("HAS_SURFACE_MEMBER"):
                self.member(shell, rp, c)
        elif gt in ("CompositeSolid", "MultiSolid"):
            el = self.E(holder, "gml", gt)
            self.set_id(el, pid, synth)
            for rp, c in proto.kids("HAS_SOLID_MEMBER"):
                self._solid_into(self.E(el, "gml", "solidMember"), c)
        elif gt == "MultiCurve":
            el = self.E(holder, "gml", "MultiCurve")
            self.set_id(el, pid, synth)
            for _rp, ls in proto.kids("HAS_LINE"):
                le = self.E(self.E(el, "gml", "curveMember"), "gml", "LineString")
                self.set_id(le, ls.id, ls.p.get("synthetic_id"))
                self.poslist(le, ls)
        elif gt == "TriangulatedSurface":
            el = self.E(holder, "gml", "TriangulatedSurface")
            self.set_id(el, pid, synth)
            if proto.p.get("srs_name"):
                el.set("srsName", proto.p["srs_name"])
            patches = self.E(el, "gml", "patches" if self.v3 else "trianglePatches")
            for _rp, poly in proto.kids("HAS_SURFACE_MEMBER"):
                tri = self.E(patches, "gml", "Triangle")
                ext = poly.first("HAS_EXTERIOR_RING")
                if ext is not None:
                    self.ring(self.E(tri, "gml", "exterior"), ext)
        else:
            self.loss.add(f"implicit template of type {gt}", pid)
            holder.getparent().remove(holder)
            return None
        self.proto_done.add(pid)
        return el


# ---------------------------------------------------------------------------
# version-neutral access to a feature's content
# ---------------------------------------------------------------------------

def _to_datetime(s):
    return s + "T00:00:00" if s and re.fullmatch(r"-?\d{4}-\d{2}-\d{2}", s) else s


def _to_date(s):
    return s[:10] if s and len(s) > 10 and s[4] == "-" else s


def geoms(node):
    """Collect a node's LoD geometry by kind and LoD (both codecs' edge patterns)."""
    g = {"solid": defaultdict(list), "ms": defaultdict(list), "ms_role": {}, "impl_ms": {},
         "mc": defaultdict(list), "mc_id": {}, "point": {}, "tic": {}, "impl": {},
         "geom": defaultdict(list)}
    for _rp, s in node.kids("HAS_LOD_SOLID"):
        g["solid"][str(s.p.get("lod") or "2")].append(s)
    for rp, m in node.kids("HAS_LOD_SURFACE"):
        if rp.get("role") == "primary":
            continue
        lod = str(rp.get("lod") or m.p.get("lod") or "2")
        if m.p.get("is_implicit"):
            g["impl_ms"][lod] = m
            continue
        role = rp.get("role") or "multisurface"
        g["ms"][(lod, role)].append((rp, m))
    for rp, m in node.kids("HAS_LOD_GEOMETRY"):
        g["geom"][str(rp.get("lod") or m.p.get("lod") or "2")].append(m)
    for rp, ls in node.kids("HAS_LOD_MULTICURVE"):
        key = (str(rp.get("lod") or "0"), rp.get("role") or "multicurve")
        g["mc"][key].append(ls)
        if rp.get("multi_curve_id"):
            g["mc_id"].setdefault(key, rp["multi_curve_id"])
    for rp, pt in node.kids("HAS_LOD_POINT"):
        g["point"][str(rp.get("lod") or "0")] = pt
    for _rp, ti in node.kids("HAS_TERRAIN_INTERSECTION"):
        g["tic"][str(ti.p.get("lod") or "2")] = ti
    for rp, pr in node.kids("HAS_PROTOTYPE"):
        g["impl"][str(rp.get("lod") or "1")] = (rp, pr)
    return g


def implicit_items(node, g=None):
    """Implicit representations as dicts — the generalised 3.0 pattern (HAS_PROTOTYPE
    edge with matrix/ref point) and the 2.0 / street-tree pattern
    (GeometryMultiSurface{is_implicit} [→ HAS_PROTOTYPE])."""
    g = g or geoms(node)
    out = {}
    for lod, (rp, proto) in g["impl"].items():
        out[lod] = {"matrix": rp.get("transformation_matrix"), "ref": rp.get("reference_point"),
                    "ref_id": rp.get("reference_point_id"),
                    "ref_dim": rp.get("reference_point_srs_dimension"),
                    "mime": rp.get("mime_type"), "mime_cs": rp.get("mime_type_codespace"),
                    "lib": rp.get("library_object"), "impl_id": rp.get("implicit_geometry_id"),
                    "proto": proto, "via_xlink": rp.get("via_xlink"), "inline_ms": None,
                    "ref_ms": None}
    for lod, ms in g["impl_ms"].items():
        proto = ms.first("HAS_PROTOTYPE")
        out.setdefault(lod, {
            "matrix": ms.p.get("transformation_matrix"), "ref": ms.p.get("reference_point"),
            "ref_id": None, "ref_dim": None, "mime": None, "mime_cs": None, "lib": None,
            "impl_id": None, "proto": proto, "via_xlink": not ms.p.get("prototype_inline"),
            "inline_ms": ms if (proto is None and not ms.p.get("prototype_href")) else None,
            "ref_ms": ms.p.get("prototype_href") if proto is None else None})
    return out


def traffic_areas(node, seen=None):
    """All (Auxiliary)TrafficAreas below a transportation space, depth-first in document
    order, with the chain of spaces they hang below (for the 2.0 flattening)."""
    seen = seen if seen is not None else set()
    out = []
    for rp, s in node.kids("HAS_BOUNDARY"):
        if s.p.get("surface_type") in ("TrafficArea", "AuxiliaryTrafficArea", "HoleSurface") \
                and s.eid not in seen:
            seen.add(s.eid)
            out.append((s, [node]))
    for rel in ("HAS_TRAFFIC_SPACE", "HAS_SECTION", "HAS_INTERSECTION", "HAS_HOLE"):
        for _rp, c in node.kids(rel):
            for s, chain in traffic_areas(c, seen):
                out.append((s, [node] + chain))
    return out


def curves_below(node):
    out = []
    for (lod, role), lines in geoms(node).get("mc", {}).items():
        if lod == "0":
            out.extend(lines)
    for rel in ("HAS_TRAFFIC_SPACE", "HAS_SECTION", "HAS_INTERSECTION"):
        for _rp, c in node.kids(rel):
            out.extend(curves_below(c))
    return out


# ---------------------------------------------------------------------------
# CityGML 3.0 serializer
# ---------------------------------------------------------------------------

_SURF3 = {"WallSurface": "con", "RoofSurface": "con", "GroundSurface": "con",
          "FloorSurface": "con", "OuterFloorSurface": "con", "OuterCeilingSurface": "con",
          "InteriorWallSurface": "con", "CeilingSurface": "con", "ClosureSurface": "core",
          "WaterClosureSurface": "core", "TrafficArea": "tran", "AuxiliaryTrafficArea": "tran",
          "HoleSurface": "tran", "WaterSurface": "wtr", "WaterGroundSurface": "wtr",
          "GenericThematicSurface": "gen"}

# feature_type → (prefix, local, base, construction)
_TOP3 = {
    "Road": ("tran", "Road", "unocc", False), "Railway": ("tran", "Railway", "unocc", False),
    "Track": ("tran", "Track", "unocc", False), "Square": ("tran", "Square", "unocc", False),
    "Waterway": ("tran", "Waterway", "unocc", False),
    "TransportationComplex": ("tran", "Road", "unocc", False),
    "CityFurniture": ("frn", "CityFurniture", "occ", False),
    "SolitaryVegetationObject": ("veg", "SolitaryVegetationObject", "occ", False),
    "PlantCover": ("veg", "PlantCover", "occ", False),
    "WaterBody": ("wtr", "WaterBody", "occ", False),
    "LandUse": ("luse", "LandUse", "thematic", False),
    "Bridge": ("brid", "Bridge", "occ", True),
    "OtherConstruction": ("con", "OtherConstruction", "occ", True),
    "GenericOccupiedSpace": ("gen", "GenericOccupiedSpace", "occ", False),
    "GenericUnoccupiedSpace": ("gen", "GenericUnoccupiedSpace", "unocc", False),
    "GenericLogicalSpace": ("gen", "GenericLogicalSpace", "logical", False),
    "GenericThematicSurface": ("gen", "GenericThematicSurface", "thematic", False),
    "GenericCityObject": ("gen", "GenericOccupiedSpace", "occ", False),
}


class Writer3(BaseWriter):
    def __init__(self, dataset, opts):
        super().__init__(PROFILE_V3, dataset, opts)

    # -- shared blocks ----------------------------------------------------------
    def head(self, el, n, top=False):
        p = n.p
        for f in _loads(p.get("metadata_xml"), []):
            self.verbatim(el, f, "gml:metaDataProperty (other GML version)")
        if p.get("gml_description"):
            self.E(el, "gml", "description", p["gml_description"])
        if p.get("gml_identifier"):
            self.E(el, "gml", "identifier", p["gml_identifier"],
                   codeSpace=p.get("gml_identifier_codespace") or "unknown")
        if p.get("gml_name"):
            self.E(el, "gml", "name", p["gml_name"])
        if p.get("has_bbox_envelope") and p.get("bbox_lower_corner") and p.get("bbox_upper_corner"):
            self.envelope(el, p["bbox_lower_corner"], p["bbox_upper_corner"])
        if p.get("location_xml"):
            self.verbatim(el, p["location_xml"], "gml:location (other GML version)")
        for key, local in (("creation_date", "creationDate"), ("termination_date", "terminationDate"),
                           ("valid_from", "validFrom"), ("valid_to", "validTo")):
            if p.get(key):
                self.E(el, "core", local, _to_datetime(p[key]))

    def cityobject(self, el, n):
        p = n.p
        frags = _loads(p.get("external_reference_xml"), [])
        if frags:
            for f in frags:
                if _frag_ns(f) == self.P.ns["core"]:
                    self.verbatim(el, f, "externalReference")
                else:
                    self.extref_from_v2(el, f)
        elif p.get("external_information_system") or p.get("external_object_name"):
            er = self.E(self.E(el, "core", "externalReference"), "core", "ExternalReference")
            self.E(er, "core", "targetResource", p.get("external_object_name") or n.id)
            if p.get("external_information_system"):
                self.E(er, "core", "informationSystem", p["external_information_system"])
        for f in _loads(p.get("generalizes_to_xml"), []):
            if _frag_ns(f) == self.P.ns["core"]:
                self.verbatim(el, f, "generalizesTo")
            else:
                self.loss.add("generalizesTo (2.0 encoding)", n.id)
        for key, local in (("relative_to_terrain", "relativeToTerrain"),
                           ("relative_to_water", "relativeToWater")):
            if p.get(key):
                self.E(el, "core", local, p[key])
        for rp, tgt in refs_with_pending(n, "RELATED_TO"):
            href = self.ref_target(tgt.id, rp.get("href"))
            if href is None:
                self.loss.add("relatedTo target not in export", tgt.id)
                continue
            cor = self.E(self.E(el, "core", "relatedTo"), "core", "CityObjectRelation")
            if rp.get("relation_id"):
                self.set_id(cor, rp["relation_id"])
            ga = _loads(rp.get("gen_attrs_json"), {})
            if ga:
                self.gen_attrs(cor, dict(ga.get("props", {}), _gen_attrs_map=json.dumps(ga.get("map", {}))))
            self.E(cor, "core", "relationType", rp.get("relation_type") or "related",
                   codeSpace=rp.get("relation_type_codespace"))
            rel = self.E(cor, "core", "relatedTo")
            rel.set(XLINK_HREF, href)
        for _rp, app in n.kids("HAS_APPEARANCE"):
            self.appearance(self.E(el, "core", "appearance"), app)
        self.gen_attrs(el, p)
        for _rp, dyn in n.kids("HAS_DYNAMIZER"):
            self.dynamizer(self.E(el, "core", "dynamizer"), dyn)
        if p.get("ade_xml"):
            self.loss.add("ADE content (not re-emitted in 3.0)", n.id)

    # dyn:SensorConnection children in XSD order (Dynamizer node key, local name)
    _SENSOR_FIELDS = (("connection_type", "connectionType"), ("observation_property", "observationProperty"),
                      ("uom", "uom"), ("sensor_id", "sensorID"), ("sensor_name", "sensorName"),
                      ("observation_id", "observationID"), ("datastream_id", "datastreamID"),
                      ("base_url", "baseURL"), ("auth_type", "authType"), ("mqtt_server", "mqttServer"),
                      ("mqtt_topic", "mqttTopic"), ("link_to_observation", "linkToObservation"),
                      ("link_to_sensor_description", "linkToSensorDescription"))

    def dynamizer(self, holder, d):
        """(:Dynamizer) → dyn:Dynamizer: attributeRef, start/end time, the verbatim
        dynamicData timeseries and/or the SensorConnection. Live values (live_*) are graph
        state, not part of the model, and are not written."""
        p = d.p
        el = self.E(holder, "dyn", "Dynamizer")
        self.set_id(el, d.id, p.get("synthetic_id"))
        self.head(el, d)
        self.E(el, "dyn", "attributeRef", p.get("attribute_ref") or "")
        for key, local in (("start_time", "startTime"), ("end_time", "endTime")):
            if p.get(key):
                self.E(el, "dyn", local, p[key])
        if p.get("dynamic_data_xml"):
            self.verbatim(self.E(el, "dyn", "dynamicData"), p["dynamic_data_xml"], "dyn:dynamicData")
        if p.get("connection_type") or p.get("observation_property"):
            sc = self.E(self.E(el, "dyn", "sensorConnection"), "dyn", "SensorConnection")
            for key, local in self._SENSOR_FIELDS:
                if key == "connection_type":
                    self.E(sc, "dyn", local, p.get(key) or "unknown", codeSpace=p.get(f"{key}_codespace"))
                elif key == "observation_property":
                    self.E(sc, "dyn", local, p.get(key) or "")
                elif p.get(key) is not None:
                    attrs = {"codeSpace": p[f"{key}_codespace"]} if p.get(f"{key}_codespace") else {}
                    self.E(sc, "dyn", local, str(p[key]), **attrs)
            if p.get("sensor_location"):
                href = self.ref_target(p["sensor_location"], p.get("sensor_location_href"))
                if href is not None:
                    self.E(sc, "dyn", "sensorLocation").set(XLINK_HREF, href)
                else:
                    self.loss.add("dyn:sensorLocation target not in export", d.id)

    def extref_from_v2(self, el, frag):
        try:
            x = etree.fromstring(frag)
        except etree.XMLSyntaxError:
            self.loss.add("externalReference (unparsable)")
            return
        ns = etree.QName(x).namespace
        info = x.findtext(f"{{{ns}}}informationSystem")
        tgt = x.findtext(f"{{{ns}}}externalObject/{{{ns}}}uri") or \
            x.findtext(f"{{{ns}}}externalObject/{{{ns}}}name")
        er = self.E(self.E(el, "core", "externalReference"), "core", "ExternalReference")
        self.E(er, "core", "targetResource", (tgt or "").strip() or "unknown")
        if info:
            self.E(er, "core", "informationSystem", info.strip())

    def ref_target(self, tid, raw_href=None):
        if tid in self.doc_ids:
            self.hrefs.append((tid, "reference"))
            return f"#{tid}"
        return raw_href or None

    def space(self, el, n, g, base, boundaries=None):
        p = n.p
        if p.get("space_type"):
            self.E(el, "core", "spaceType", p["space_type"])
        for key in ("volume_xml", "area_xml"):
            for f in _loads(p.get(key), []):
                self.verbatim(el, f, key)
        for rp, s in (boundaries if boundaries is not None else n.kids("HAS_BOUNDARY")):
            if not self.keep(s):
                continue
            if rp.get("via_xlink") or s.partial:
                self.href(self.E(el, "core", "boundary"), s.id, "boundary")
            else:
                self.surface(self.E(el, "core", "boundary"), s)
        self.lod_geometry(el, n, g, thematic=False)
        if base in ("occ", "unocc"):
            for lod in ("1", "2", "3"):
                ti = g["tic"].get(lod) or (g["tic"].get("4") if lod == "3" else None)
                if ti is not None:
                    lines = [ls for _rp, ls in ti.kids("HAS_LINE")]
                    self.multicurve(el, "core", f"lod{lod}TerrainIntersectionCurve", lines,
                                    ti.p.get("multi_curve_id"))
            if "4" in g["tic"] and "3" in g["tic"]:
                self.loss.add("lod4TerrainIntersection (LoD4 removed in 3.0)", n.id)
        if base == "occ":
            items = implicit_items(n, g)
            for lod in ("1", "2", "3"):
                it = items.get(lod) or (items.get("4") if lod == "3" and "3" not in items else None)
                if it is not None:
                    self.implicit(el, f"lod{lod}ImplicitRepresentation", it, n)
            if "4" in items and "3" in items:
                self.loss.add("lod4ImplicitRepresentation (LoD4 removed in 3.0)", n.id)
            if "0" in items:
                self.loss.add("lod0 implicit representation (not in 3.0)", n.id)

    def lod_geometry(self, el, n, g, thematic):
        """core LoD geometry in content-model order. Space: lod0Point, lod0MultiSurface,
        lod0MultiCurve, lod1Solid, lod2Solid, lod2MultiSurface, lod2MultiCurve, lod3Solid,
        lod3MultiSurface, lod3MultiCurve. ThematicSurface (non-primary part): lod0MultiCurve,
        lod0..3MultiSurface. LoD4 → LoD3 when free; lod1 MultiSurface is not a space
        property in 3.0 (loss)."""
        ms = {}
        for (lod, role), lst in g["ms"].items():
            tl = lod
            if role == "footprint":
                tl = "0"
            elif role == "roofedge":
                self.loss.add("lod0RoofEdge (no 3.0 space property)", n.id)
                continue
            if tl == "4":
                tl = "3"
            ms.setdefault(tl, []).extend(lst)
        for lod, lst in g["geom"].items():          # 2.0 lodNGeometry (MultiSurface)
            ms.setdefault("3" if lod == "4" else lod, []).extend((None, m) for m in lst)
        solids = {}
        for lod, lst in g["solid"].items():
            tl = "3" if lod == "4" else lod
            if tl in solids:
                self.loss.add("LoD4 solid (LoD3 slot taken)", n.id)
                continue
            solids[tl] = lst[0]
        mcs = {}
        for (lod, role), lst in g["mc"].items():
            tl = "3" if lod == "4" else lod
            mcs.setdefault(tl, ([], g["mc_id"].get((lod, role))))[0].extend(lst)

        def write_ms(lod):
            lst = ms.get(lod)
            if not lst:
                return
            xl = [m for rp, m in lst if rp and rp.get("via_xlink")]
            if xl:
                self.href(self.E(el, "core", f"lod{lod}MultiSurface"), xl[0].id, "MultiSurface")
                return
            self.multisurface(el, "core", f"lod{lod}MultiSurface", ms_nodes=[m for _rp, m in lst])

        def write_mc(lod):
            if lod in mcs:
                lines, mcid = mcs[lod]
                self.multicurve(el, "core", f"lod{lod}MultiCurve", lines, mcid)

        if thematic:
            write_mc("0")
            for lod in ("0", "1", "2", "3"):
                write_ms(lod)
            for lod in mcs:
                if lod != "0":
                    self.loss.add(f"lod{lod}MultiCurve on a thematic surface", n.id)
            return
        if "0" in g["point"]:
            pt = g["point"]["0"]
            self.point_into(self.E(el, "core", "lod0Point"), pt.p.get("pos_list"),
                            None if pt.p.get("synthetic_id") else pt.id, pt.p.get("srs_dimension"))
        write_ms("0")
        write_mc("0")
        if "1" in solids:
            self.solid(el, "core", "lod1Solid", solids["1"])
        if "1" in ms:
            self.loss.add("lod1MultiSurface (not a 3.0 space property)", n.id)
        for lod in ("2", "3"):
            if lod in solids:
                self.solid(el, "core", f"lod{lod}Solid", solids[lod])
            write_ms(lod)
            write_mc(lod)
        if "1" in mcs:
            self.loss.add("lod1MultiCurve (not in 3.0)", n.id)

    def implicit(self, el, prop_local, it, owner):
        prop = self.E(el, "core", prop_local)
        ig = self.E(prop, "core", "ImplicitGeometry")
        if it.get("impl_id"):
            self.set_id(ig, it["impl_id"])
        self.E(ig, "core", "transformationMatrix",
               " ".join((it.get("matrix") or "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1").split()))
        if it.get("mime"):
            self.E(ig, "core", "mimeType", it["mime"], codeSpace=it.get("mime_cs"))
        if it.get("lib"):
            self.E(ig, "core", "libraryObject", it["lib"])
        rp_el = self.E(ig, "core", "referencePoint")
        self.point_into(rp_el, it.get("ref") or "0 0 0", it.get("ref_id"), it.get("ref_dim"))
        self.relative_geometry(self.E(ig, "core", "relativeGeometry"), it, owner)

    def relative_geometry(self, rg, it, owner):
        proto = it.get("proto")
        if proto is not None:
            pid = proto.id
            full = self.proto_nodes.get(pid, proto)
            if pid not in self.proto_done and not full.partial:   # inline at first use
                if self.prototype_geometry(rg, full) is not None:
                    return
            self.href(rg, pid, "relativeGeometry")
            return
        if it.get("inline_ms") is not None:
            ms = it["inline_ms"]
            m = self.E(rg, "gml", "MultiSurface")
            self.set_id(m, ms.id, ms.p.get("synthetic_id"))
            for rp, c in ms.kids("HAS_SURFACE_MEMBER"):
                self.member(m, rp, c)
            return
        if it.get("ref_ms"):
            self.href(rg, it["ref_ms"], "relativeGeometry")
            return
        rg.getparent().remove(rg)

    # -- appearance -------------------------------------------------------------
    def appearance(self, holder, app):
        a = self.E(holder, "app", "Appearance")
        self.set_id(a, app.id, app.p.get("synthetic_id"))
        if app.p.get("gml_description"):
            self.E(a, "gml", "description", app.p["gml_description"])
        if app.p.get("gml_name"):
            self.E(a, "gml", "name", app.p["gml_name"])
        if app.p.get("theme") is not None:
            self.E(a, "app", "theme", app.p["theme"])
        for _rp, sd in app.kids("HAS_SURFACE_DATA"):
            self.surface_data(self.E(a, "app", "surfaceData"), sd)

    def _targets(self, sd):
        # targets whose geometry is not written end up removed by drop_unwritten_targets()
        return sd.kids("TARGETS")

    def surface_data(self, holder, sd):
        kind = sd.p.get("surface_data_type") or next(
            (lab for lab in ("X3DMaterial", "ParameterizedTexture", "GeoreferencedTexture")
             if lab in sd.labels), "X3DMaterial")
        el = self.E(holder, "app", kind)
        self.set_id(el, sd.id, sd.p.get("synthetic_id"))
        p = sd.p
        if p.get("is_front") is not None:
            self.E(el, "app", "isFront", p["is_front"])
        if kind == "X3DMaterial":
            for key, local in (("ambient_intensity", "ambientIntensity"), ("diffuse_color", "diffuseColor"),
                               ("emissive_color", "emissiveColor"), ("specular_color", "specularColor"),
                               ("shininess", "shininess"), ("transparency", "transparency"),
                               ("is_smooth", "isSmooth")):
                if p.get(key) is not None:
                    self.E(el, "app", local, p[key])
            for _rp, t in self._targets(sd):
                self.E(el, "app", "target", f"#{t.id}")
            return
        self.E(el, "app", "imageURI", p.get("image_uri") or "")
        if p.get("mime_type"):
            self.E(el, "app", "mimeType", p["mime_type"], codeSpace=p.get("mime_type_codespace"))
        for key, local in (("texture_type", "textureType"), ("wrap_mode", "wrapMode"),
                           ("border_color", "borderColor")):
            if p.get(key) is not None:
                self.E(el, "app", local, p[key])
        if kind == "ParameterizedTexture":
            for rp, t in self._targets(sd):
                ta = self.E(self.E(el, "app", "textureParameterization"), "app", "TextureAssociation")
                if rp.get("texture_association_id"):
                    self.set_id(ta, rp["texture_association_id"])
                self.E(ta, "app", "target", f"#{t.id}")
                inner = self.E(ta, "app", "textureParameterization")
                if rp.get("world_to_texture"):
                    self.E(self.E(inner, "app", "TexCoordGen"), "app", "worldToTexture",
                           rp["world_to_texture"])
                else:
                    tcl = self.E(inner, "app", "TexCoordList")
                    pairs = _loads(rp.get("texture_coordinates_json"), [])
                    for _ring, coords in pairs:
                        self.E(tcl, "app", "textureCoordinates", coords)
                    for ring, _coords in pairs:
                        self.E(tcl, "app", "ring", f"#{ring}")
            return
        if p.get("prefer_world_file") is not None:
            self.E(el, "app", "preferWorldFile", p["prefer_world_file"])
        if p.get("reference_point"):
            self.point_into(self.E(el, "app", "referencePoint"), p["reference_point"])
        if p.get("orientation"):
            self.E(el, "app", "orientation", p["orientation"])
        for _rp, t in self._targets(sd):
            self.E(el, "app", "target", f"#{t.id}")

    # -- thematic surfaces --------------------------------------------------------
    def surface(self, holder, s):
        st = s.p.get("surface_type") or "WallSurface"
        local = "ClosureSurface" if st == "WaterClosureSurface" else st
        prefix = _SURF3.get(st)
        if prefix is None:
            self.loss.add(f"boundary surface {st} (no 3.0 mapping)", s.id)
            holder.getparent().remove(holder)
            return
        el = self.E(holder, prefix, local)
        self.set_id(el, s.id, s.p.get("synthetic_id"))
        self.head(el, s)
        self.cityobject(el, s)
        p = s.p
        for f in _loads(p.get("area_xml"), []):
            self.verbatim(el, f, "area_xml")
        g = geoms(s)
        # the primary MultiSurface (HAS_POLYGON) joins the other LoDs
        polys = s.kids("HAS_POLYGON")
        if polys:
            lod = str(p.get("lod") or "2")
            lod = "3" if lod == "4" else lod
            prim = Node(None, ["GeometryMultiSurface"], {
                "id": p.get("multi_surface_id"), "synthetic_id": not p.get("multi_surface_id"),
                "composite_surface_id": p.get("composite_surface_id")})
            prim.out = [("HAS_SURFACE_MEMBER", rp, c) for rp, c in polys]
            g["ms"][(lod, "multisurface")].insert(0, (None, prim))
        self.lod_geometry(el, s, g, thematic=True)
        if st in ("TrafficArea", "AuxiliaryTrafficArea"):
            self.code(el, "tran", "class", p.get("class"), p.get("class_codespace"))
            self.codes(el, "tran", "function", p, "function")
            self.codes(el, "tran", "usage", p, "usage")
            self.code(el, "tran", "surfaceMaterial", p.get("surface_material"),
                      p.get("surface_material_codespace"))
        elif st == "WaterSurface":
            self.code(el, "wtr", "waterLevel", p.get("water_level"), p.get("water_level_codespace"))
        elif st == "GenericThematicSurface":
            self.cfu(el, "gen", p)
        elif prefix == "con":
            for _rp, o in s.kids("HAS_OPENING"):
                self.opening(self.E(el, "con", "fillingSurface"), o)

    def opening(self, holder, o):
        kind = (o.p.get("opening_type") or "Window").replace("Surface", "")
        el = self.E(holder, "con", f"{kind}Surface")
        self.set_id(el, o.id, o.p.get("synthetic_id"))
        self.head(el, o)
        self.cityobject(el, o)
        self.lod_geometry(el, o, geoms(o), thematic=True)

    def cfu(self, el, prefix, p, cls_key="class", fn_key="function", us_key="usage"):
        self.code(el, prefix, "class", p.get(cls_key), p.get(f"{cls_key.replace('_code', '')}_codespace"))
        self.codes(el, prefix, "function", p, fn_key)
        self.codes(el, prefix, "usage", p, us_key)

    def construction(self, el, n):
        p = n.p
        if p.get("condition_of_construction"):
            self.E(el, "con", "conditionOfConstruction", p["condition_of_construction"])
        doc = p.get("date_of_construction")
        if not doc and p.get("year_of_construction"):
            doc = f"{str(p['year_of_construction'])[:4]}-01-01"
            self.loss.add("yearOfConstruction → dateOfConstruction YYYY-01-01 (policy)", n.id)
        if doc:
            self.E(el, "con", "dateOfConstruction", _to_date(doc))
        dod = p.get("date_of_demolition")
        if not dod and p.get("year_of_demolition"):
            dod = f"{str(p['year_of_demolition'])[:4]}-01-01"
        if dod:
            self.E(el, "con", "dateOfDemolition", _to_date(dod))
        for e in _loads(p.get("elevations_json"), []):
            ev = self.E(self.E(el, "con", "elevation"), "con", "Elevation")
            self.E(ev, "con", "elevationReference", e.get("reference") or "unknown",
                   codeSpace=e.get("reference_codespace"))
            self.E(ev, "con", "elevationValue", e.get("value") or "", srsDimension=e.get("srs_dimension"))
        heights = _loads(p.get("heights_json"), [])
        if not heights and p.get("measured_height") is not None:
            heights = [{"high_reference": p.get("height_high_reference") or "highestRoofEdge",
                        "low_reference": p.get("height_low_reference") or "lowestGroundPoint",
                        "status": p.get("height_status") or "measured",
                        "value": p["measured_height"], "uom": p.get("measured_height_uom")}]
            if not p.get("height_high_reference"):
                self.loss.add("measuredHeight → con:Height (references assumed: "
                              "highestRoofEdge / lowestGroundPoint, status measured)", n.id)
        for h in heights:
            he = self.E(self.E(el, "con", "height"), "con", "Height")
            self.E(he, "con", "highReference", h.get("high_reference") or "highestRoofEdge",
                   codeSpace=h.get("high_reference_codespace"))
            self.E(he, "con", "lowReference", h.get("low_reference") or "lowestGroundPoint",
                   codeSpace=h.get("low_reference_codespace"))
            self.E(he, "con", "status", h.get("status") or "measured")
            self.E(he, "con", "value", h.get("value"), uom=h.get("uom") or "m")
        self.occupancy(el, "con", p)

    def occupancy(self, el, prefix, p):
        for o in _loads(p.get("occupancy_json"), []):
            oc = self.E(self.E(el, prefix, "occupancy"), "core", "Occupancy")
            self.E(oc, "core", "numberOfOccupants", o.get("number_of_occupants") or "0")
            if o.get("interval"):
                self.E(oc, "core", "interval", o["interval"], codeSpace=o.get("interval_codespace"))
            if o.get("occupant_type"):
                self.E(oc, "core", "occupantType", o["occupant_type"],
                       codeSpace=o.get("occupant_type_codespace"))

    def address(self, el, prefix, n):
        for f in _loads(n.p.get("address_xml"), []):
            if isinstance(f, str) and _frag_ns(f) == self.P.ns[prefix]:
                self.verbatim(el, f, "address")
            else:
                conv = xal2_to_xal3(f)
                if conv is None:
                    self.loss.add("address (xAL 2 → xAL 3 not convertible)", n.id)
                    continue
                a = self.E(self.E(el, prefix, "address"), "core", "Address")
                xa = self.E(self.E(a, "core", "xalAddress"), "xAL", "Address")
                _write_xal3(self, xa, conv)
                self.loss.add("address converted xAL 2 → xAL 3 (main fields only)", n.id)

    # -- features -----------------------------------------------------------------
    def feature(self, holder, n):
        """Dispatch a node to its 3.0 element; returns the element or None."""
        if self.clipped_away(n):
            return None
        lab = n.labels
        if "Building" in lab or "BuildingPart" in lab:
            return self.building(holder, n, part="BuildingPart" in lab)
        ft = n.p.get("feature_type")
        spec = _TOP3.get(ft)
        if spec is None:
            self.loss.add(f"feature type {ft} (no 3.0 writer)", n.id)
            return None
        prefix, local, base, constr = spec
        if ft in ("TransportationComplex", "GenericCityObject"):
            self.loss.add(f"2.0 {ft} written as {prefix}:{local}", n.id)
        return self.generic_feature(holder, n, prefix, local, base, constr)

    def generic_feature(self, holder, n, prefix, local, base, constr):
        el = self.E(holder, prefix, local)
        self.set_id(el, n.p.get("source_gml_id") or n.id, n.p.get("synthetic_id"))
        self.head(el, n, top=True)
        self.cityobject(el, n)
        g = geoms(n)
        p = n.p
        if local in ("Road", "Railway", "Track", "Square", "Waterway", "Section", "Intersection"):
            own = [(rp, s) for rp, s in n.kids("HAS_BOUNDARY")
                   if rp.get("prop") not in ("trafficArea", "auxiliaryTrafficArea")]
            self.space(el, n, g, base, boundaries=own)
            self.transport_block(el, n)
            if local in ("Section", "Intersection"):
                self.code(el, "tran", "class", p.get("class"), p.get("class_codespace"))
            else:
                self.cfu(el, "tran", p)
                if local != "Square":
                    for rel, prop, sub in (("HAS_SECTION", "section", "Section"),
                                           ("HAS_INTERSECTION", "intersection", "Intersection")):
                        for rp, c in n.kids(rel):
                            if self.keep(c):
                                self.sub_feature(self.E(el, "tran", prop), c, rp)
            return el
        if base == "thematic":
            self.lod_geometry(el, n, g, thematic=True)
        else:
            self.space(el, n, g, base)
        if constr:
            self.construction(el, n)
        if local in ("CityFurniture", "WaterBody", "LandUse", "OtherConstruction") or prefix == "gen":
            self.cfu(el, prefix, p)
        elif local == "PlantCover":
            self.cfu(el, "veg", p)
            for key, loc in (("average_height", "averageHeight"), ("min_height", "minHeight"),
                             ("max_height", "maxHeight")):
                self.measure(el, "veg", loc, p, key)
        elif local == "SolitaryVegetationObject":
            self.cfu(el, "veg", p)
            self.code(el, "veg", "species", p.get("species"), p.get("species_codespace"))
            for key, loc in (("height", "height"), ("trunk_diameter", "trunkDiameter"),
                             ("crown_diameter", "crownDiameter"),
                             ("root_ball_diameter", "rootBallDiameter"),
                             ("max_root_ball_depth", "maxRootBallDepth")):
                self.measure(el, "veg", loc, p, key)
        elif local in ("Bridge", "BridgePart"):
            self.bridge_module(el, n, part=local == "BridgePart")
        return el

    def transport_block(self, el, n):
        p = n.p
        if p.get("traffic_direction"):
            self.E(el, "tran", "trafficDirection", p["traffic_direction"])
        self.occupancy(el, "tran", p)
        spaces = n.kids("HAS_TRAFFIC_SPACE")
        synth = [(rp, s) for rp, s in n.kids("HAS_BOUNDARY")
                 if rp.get("prop") in ("trafficArea", "auxiliaryTrafficArea")]
        for aux in (False, True):
            for rp, c in spaces:
                if bool(rp.get("auxiliary")) == aux and self.keep(c):
                    prop = "auxiliaryTrafficSpace" if aux else "trafficSpace"
                    self.sub_feature(self.E(el, "tran", prop), c, rp)
            for rp, ta in synth:
                if (rp.get("prop") == "auxiliaryTrafficArea") == aux and self.keep(ta):
                    self.synth_space(el, ta, aux)
        for rel, prop in (("HAS_HOLE", "hole"), ("HAS_MARKING", "marking")):
            for rp, c in n.kids(rel):
                if self.keep(c):
                    self.sub_feature(self.E(el, "tran", prop), c, rp)

    def synth_space(self, el, ta, aux):
        """2.0-sourced TrafficArea → synthesised (Auxiliary)TrafficSpace:
        id <ta_id>__space, granularity 'way', the area as its core:boundary."""
        kind = "AuxiliaryTrafficSpace" if aux else "TrafficSpace"
        sp = self.E(self.E(el, "tran", "auxiliaryTrafficSpace" if aux else "trafficSpace"), "tran", kind)
        self.set_id(sp, f"{ta.id}__space")
        self.surface(self.E(sp, "core", "boundary"), ta)
        self.E(sp, "tran", "granularity", "way")
        self.loss.add(f"synthesised {kind} for a 2.0 {ta.p.get('surface_type')} (granularity 'way')",
                      ta.id)

    def sub_feature(self, holder, c, rp=None):
        if (rp and rp.get("via_xlink")) or c.partial:
            self.href(holder, c.id, "sub-feature")
            return
        ft = c.p.get("feature_type") or next(iter(c.labels - {"Entity"}), "")
        el = self.E(holder, "tran", ft)
        self.set_id(el, c.id, c.p.get("synthetic_id"))
        self.head(el, c)
        self.cityobject(el, c)
        g = geoms(c)
        p = c.p
        if ft in ("Section", "Intersection"):
            own = [(r, s) for r, s in c.kids("HAS_BOUNDARY")]
            self.space(el, c, g, "unocc", boundaries=own)
            self.transport_block(el, c)
            self.code(el, "tran", "class", p.get("class"), p.get("class_codespace"))
        elif ft == "Marking":
            self.lod_geometry(el, c, g, thematic=True)
            self.code(el, "tran", "class", p.get("class"), p.get("class_codespace"))
        elif ft in ("TrafficSpace", "AuxiliaryTrafficSpace"):
            self.space(el, c, g, "unocc")
            self.cfu(el, "tran", p)
            self.E(el, "tran", "granularity", p.get("granularity") or "way")
            if ft == "TrafficSpace":
                if p.get("traffic_direction"):
                    self.E(el, "tran", "trafficDirection", p["traffic_direction"])
                self.occupancy(el, "tran", p)
                for rel, local in (("PREDECESSOR", "predecessor"), ("SUCCESSOR", "successor")):
                    for r, t in refs_with_pending(c, rel):
                        href = self.ref_target(t.id, r.get("href"))
                        if href is None:
                            self.loss.add(f"{local} target not in export", t.id)
                            continue
                        self.E(el, "tran", local).set(XLINK_HREF, href)
                for _r, cs in c.kids("HAS_CLEARANCE_SPACE"):
                    if self.keep(cs):
                        self.sub_feature(self.E(el, "tran", "clearanceSpace"), cs)
        elif ft in ("Hole", "ClearanceSpace"):
            self.space(el, c, g, "unocc")
            if ft == "Hole":
                self.code(el, "tran", "class", p.get("class"), p.get("class_codespace"))
            else:
                self.codes(el, "tran", "class", p, "class")
        else:
            self.loss.add(f"transportation sub-feature {ft}", c.id)

    def bridge_module(self, el, n, part=False):
        p = n.p
        self.cfu(el, "brid", p)
        if p.get("is_movable") not in (None, ""):
            self.E(el, "brid", "isMovable", str(p["is_movable"]).strip().lower())
        for want, prop, local in (("construct", "bridgeConstructiveElement", "BridgeConstructiveElement"),
                                  ("install", "bridgeInstallation", "BridgeInstallation")):
            for _rp, nf in n.kids("HAS_NESTED"):
                kind = nf.p.get("feature_kind") or ""
                is_c = kind in ("BridgeConstructiveElement", "BridgeConstructionElement")
                is_i = kind in ("BridgeInstallation", "IntBridgeInstallation")
                if (want == "construct" and is_c) or (want == "install" and is_i):
                    self.nested_bridge(self.E(el, "brid", prop), nf, local)
        self.address(el, "brid", n)
        if not part:
            for _rp, bp in n.kids("HAS_BRIDGE_PART"):
                self.generic_feature(self.E(el, "brid", "bridgePart"), bp, "brid", "BridgePart",
                                     "occ", True)

    def nested_bridge(self, holder, nf, local):
        el = self.E(holder, "brid", local)
        self.set_id(el, nf.id, nf.p.get("synthetic_id"))
        self.head(el, nf)
        self.cityobject(el, nf)
        self.space(el, nf, geoms(nf), "occ")
        p = nf.p
        if local == "BridgeConstructiveElement":
            if p.get("is_structural_element") is not None:
                self.E(el, "con", "isStructuralElement", p["is_structural_element"])
        else:
            rel = p.get("relation_to_construction") or \
                ("inside" if nf.p.get("feature_kind") == "IntBridgeInstallation" else None)
            if rel:
                self.E(el, "con", "relationToConstruction", rel)
        self.cfu(el, "brid", p)

    def building(self, holder, n, part=False):
        el = self.E(holder, "bldg", "BuildingPart" if part else "Building")
        self.set_id(el, n.p.get("source_gml_id") or n.id, n.p.get("synthetic_id"))
        self.head(el, n, top=not part)
        self.cityobject(el, n)
        self.space(el, n, geoms(n), "occ")
        self.construction(el, n)
        p = n.p
        self.code(el, "bldg", "class", p.get("class_code"), p.get("class_codespace"))
        for key, local in (("function_code", "function"), ("usage_code", "usage")):
            vals = p.get(f"{key}_list") or ([p[key]] if p.get(key) else [])
            for v in vals:
                self.code(el, "bldg", local, v, p.get(f"{key.replace('_code', '')}_codespace"))
        self.code(el, "bldg", "roofType", p.get("roof_type_code"), p.get("roof_type_codespace"))
        for key, local in (("storeys_above_ground", "storeysAboveGround"),
                           ("storeys_below_ground", "storeysBelowGround")):
            if p.get(key) is not None:
                self.E(el, "bldg", local, p[key])
        for key, local in (("storey_heights_above_ground", "storeyHeightsAboveGround"),
                           ("storey_heights_below_ground", "storeyHeightsBelowGround")):
            if p.get(key):
                self.E(el, "bldg", local, p[key], uom=p.get(f"{key}_uom") or "m")
        for rel in ("HAS_OUTER_INSTALLATION", "HAS_INTERIOR_INSTALLATION"):
            for _rp, inst in n.kids(rel):
                self.installation(self.E(el, "bldg", "buildingInstallation"), inst,
                                  inside=rel == "HAS_INTERIOR_INSTALLATION")
        for _rp, room in n.kids("HAS_INTERIOR_ROOM"):
            self.room(self.E(el, "bldg", "buildingRoom"), room)
        self.address(el, "bldg", n)
        if not part:
            for _rp, bp in n.kids("HAS_BUILDING_PART"):
                self.building(self.E(el, "bldg", "buildingPart"), bp, part=True)
        return el

    def installation(self, holder, inst, inside=False):
        el = self.E(holder, "bldg", "BuildingInstallation")
        self.set_id(el, inst.id, inst.p.get("synthetic_id"))
        self.head(el, inst)
        self.cityobject(el, inst)
        self.space(el, inst, geoms(inst), "occ")
        rel = inst.p.get("relation_to_construction") or ("inside" if inside else None)
        if rel:
            self.E(el, "con", "relationToConstruction", rel)
        self.cfu(el, "bldg", inst.p, "class_code", "function_code", "usage_code")

    def room(self, holder, room):
        el = self.E(holder, "bldg", "BuildingRoom")
        self.set_id(el, room.id, room.p.get("synthetic_id"))
        self.head(el, room)
        self.cityobject(el, room)
        self.space(el, room, geoms(room), "unocc")
        self.cfu(el, "bldg", room.p, "class_code", "function_code", "usage_code")
        n_f = len(room.kids("HAS_INTERIOR_FURNITURE")) + len(room.kids("HAS_ROOM_INSTALLATION"))
        if n_f:
            self.loss.add("room furniture / room installations (not written)", room.id, n_f)


# ---------------------------------------------------------------------------
# xAL 2 ↔ xAL 3 (main address fields only; anything else is a reported loss)
# ---------------------------------------------------------------------------

_XAL2 = "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"
_XAL3 = "urn:oasis:names:tc:ciq:xal:3"


def _parse_address(frag):
    try:
        x = etree.fromstring(frag)
    except (etree.XMLSyntaxError, ValueError, TypeError):
        return None, None
    a3 = next(x.iter(f"{{{_XAL3}}}Address"), None)
    if a3 is not None:
        q = lambda p: f"{{{_XAL3}}}{p}"  # noqa: E731
        d = {"country": a3.findtext(f"{q('Country')}/{q('NameElement')}"),
             "locality": a3.findtext(f"{q('Locality')}/{q('NameElement')}"),
             "locality_type": a3.find(q("Locality")).get(q("Type")) if a3.find(q("Locality")) is not None else None,
             "street": a3.findtext(f"{q('Thoroughfare')}/{q('NameElement')}"),
             "number": a3.findtext(f"{q('Thoroughfare')}/{q('Number')}"),
             "street_type": a3.find(q("Thoroughfare")).get(q("Type")) if a3.find(q("Thoroughfare")) is not None else None,
             "postcode": a3.findtext(f"{q('PostCode')}/{q('Identifier')}")}
        return "3", d
    a2 = next(x.iter(f"{{{_XAL2}}}AddressDetails"), None)
    if a2 is not None:
        q = lambda p: f"{{{_XAL2}}}{p}"  # noqa: E731
        loc = next(a2.iter(q("Locality")), None)
        th = next(a2.iter(q("Thoroughfare")), None)
        d = {"country": next((e.text for e in a2.iter(q("CountryName"))), None),
             "locality": loc.findtext(q("LocalityName")) if loc is not None else None,
             "locality_type": loc.get("Type") if loc is not None else None,
             "street": th.findtext(q("ThoroughfareName")) if th is not None else None,
             "number": th.findtext(q("ThoroughfareNumber")) if th is not None else None,
             "street_type": th.get("Type") if th is not None else None,
             "postcode": next((e.text for e in a2.iter(q("PostalCodeNumber"))), None)}
        return "2", d
    return None, None


def xal2_to_xal3(frag):
    ver, d = _parse_address(frag)
    return d if d and any(d.values()) else None


xal3_to_xal2 = xal2_to_xal3


def _write_xal3(w, xa, d):
    """xAL 3 (attributeFormDefault="qualified": xAL:Type / xAL:NameType)."""
    T, NT = f"{{{_XAL3}}}Type", f"{{{_XAL3}}}NameType"
    if d.get("country"):
        w.E(w.E(xa, "xAL", "Country"), "xAL", "NameElement", d["country"]).set(NT, "Name")
    if d.get("locality"):
        loc = w.E(xa, "xAL", "Locality")
        if d.get("locality_type"):
            loc.set(T, d["locality_type"])
        w.E(loc, "xAL", "NameElement", d["locality"]).set(NT, "Name")
    if d.get("street") or d.get("number"):
        th = w.E(xa, "xAL", "Thoroughfare")
        if d.get("street_type"):
            th.set(T, d["street_type"])
        if d.get("number"):
            w.E(th, "xAL", "Number", d["number"]).set(T, "Number")
        if d.get("street"):
            w.E(th, "xAL", "NameElement", d["street"])
    if d.get("postcode"):
        w.E(w.E(xa, "xAL", "PostCode"), "xAL", "Identifier", d["postcode"])


def _write_xal2(w, details, d):
    holder = details
    if d.get("country"):
        holder = w.E(details, "xAL", "Country")
        w.E(holder, "xAL", "CountryName", d["country"])
    loc = w.E(holder, "xAL", "Locality", Type=d.get("locality_type") or "Town")
    w.E(loc, "xAL", "LocalityName", d.get("locality") or "")
    if d.get("street") or d.get("number"):
        th = w.E(loc, "xAL", "Thoroughfare", Type=d.get("street_type") or "Street")
        if d.get("number"):
            w.E(th, "xAL", "ThoroughfareNumber", d["number"])
        if d.get("street"):
            w.E(th, "xAL", "ThoroughfareName", d["street"])
    if d.get("postcode"):
        w.E(w.E(loc, "xAL", "PostalCode"), "xAL", "PostalCodeNumber", d["postcode"])


# ---------------------------------------------------------------------------
# CityGML 2.0 serializer (flattening of 3.0-sourced content)
# ---------------------------------------------------------------------------

_SURF2_BLDG = {"WallSurface", "RoofSurface", "GroundSurface", "ClosureSurface", "FloorSurface",
               "InteriorWallSurface", "CeilingSurface", "OuterFloorSurface", "OuterCeilingSurface"}
# feature_type → (module prefix, 2.0 element)
_TOP2 = {"Road": ("tran", "Road"), "Railway": ("tran", "Railway"), "Track": ("tran", "Track"),
         "Square": ("tran", "Square"), "Waterway": ("tran", "TransportationComplex"),
         "TransportationComplex": ("tran", "TransportationComplex"),
         "CityFurniture": ("frn", "CityFurniture"),
         "SolitaryVegetationObject": ("veg", "SolitaryVegetationObject"),
         "PlantCover": ("veg", "PlantCover"), "WaterBody": ("wtr", "WaterBody"),
         "LandUse": ("luse", "LandUse"), "Bridge": ("brid", "Bridge"),
         "OtherConstruction": ("gen", "GenericCityObject"),
         "GenericOccupiedSpace": ("gen", "GenericCityObject"),
         "GenericUnoccupiedSpace": ("gen", "GenericCityObject"),
         "GenericLogicalSpace": ("gen", "GenericCityObject"),
         "GenericThematicSurface": ("gen", "GenericCityObject"),
         "GenericCityObject": ("gen", "GenericCityObject")}


class Writer2(BaseWriter):
    def __init__(self, dataset, opts):
        super().__init__(PROFILE_V2, dataset, opts)
        self.genattrs = opts.get("v2_subfeatures") == "genattrs"

    # -- blocks -----------------------------------------------------------------
    def head(self, el, n, extra=None):
        p = n.p
        for f in _loads(p.get("metadata_xml"), []):
            self.verbatim(el, f, "gml:metaDataProperty (GML 3.2 encoding)")
        if p.get("gml_description"):
            self.E(el, "gml", "description", p["gml_description"])
        if p.get("gml_identifier"):
            self.loss.add("gml:identifier (no GML 3.1.1 equivalent)", n.id)
        if p.get("gml_name"):
            self.E(el, "gml", "name", p["gml_name"])
        if p.get("has_bbox_envelope") and p.get("bbox_lower_corner") and p.get("bbox_upper_corner"):
            self.envelope(el, p["bbox_lower_corner"], p["bbox_upper_corner"])
        if p.get("location_xml"):
            self.verbatim(el, p["location_xml"], "gml:location (GML 3.2 encoding)")
        if p.get("creation_date"):
            self.E(el, "core", "creationDate", _to_date(p["creation_date"]))
        if p.get("termination_date"):
            self.E(el, "core", "terminationDate", _to_date(p["termination_date"]))
        for key in ("valid_from", "valid_to"):
            if p.get(key):
                self.loss.add(f"{key} (no 2.0 equivalent)", n.id)
        for f in _loads(p.get("external_reference_xml"), []):
            if _frag_ns(f) == self.P.ns["core"]:
                self.verbatim(el, f, "externalReference")
            else:
                self.extref_from_v3(el, f, n)
        if not p.get("external_reference_xml") and p.get("external_information_system"):
            er = self.E(el, "core", "externalReference")
            self.E(er, "core", "informationSystem", p["external_information_system"])
            self.E(self.E(er, "core", "externalObject"), "core", "name",
                   p.get("external_object_name") or n.id)
        for f in _loads(p.get("generalizes_to_xml"), []):
            if _frag_ns(f) == self.P.ns["core"]:
                self.verbatim(el, f, "generalizesTo")
            else:
                self.loss.add("generalizesTo (3.0 encoding)", n.id)
        for key, local in (("relative_to_terrain", "relativeToTerrain"),
                           ("relative_to_water", "relativeToWater")):
            if p.get(key):
                self.E(el, "core", local, p[key])
        ex = list(extra or [])
        for i, (rp, tgt) in enumerate(refs_with_pending(n, "RELATED_TO")):
            if self.genattrs:
                ex.append((f"citygml3_relatedTo_{i}", f"{rp.get('relation_type', '')} #{tgt.id}"))
            else:
                self.loss.add("core:relatedTo / CityObjectRelation (no 2.0 equivalent)", n.id)
        dyns = n.kids("HAS_DYNAMIZER")
        if dyns:
            self.loss.add("dyn:Dynamizer (no 2.0 equivalent)", n.id, n=len(dyns))
        self.gen_attrs(el, p, ex)
        for _rp, app in n.kids("HAS_APPEARANCE"):
            self.appearance(self.E(el, "app", "appearance"), app)
        for key in ("space_type", "volume_xml", "area_xml"):
            if p.get(key):
                self.loss.add(f"{key} (no 2.0 equivalent)", n.id)

    def extref_from_v3(self, el, frag, n):
        try:
            x = etree.fromstring(frag)
        except etree.XMLSyntaxError:
            self.loss.add("externalReference (unparsable)")
            return
        ns = etree.QName(x).namespace
        er3 = x.find(f"{{{ns}}}ExternalReference")
        if er3 is None:
            return
        info = er3.findtext(f"{{{ns}}}informationSystem")
        tgt = (er3.findtext(f"{{{ns}}}targetResource") or "").strip()
        er = self.E(el, "core", "externalReference")
        if info:
            self.E(er, "core", "informationSystem", info.strip())
        eo = self.E(er, "core", "externalObject")
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", tgt):
            self.E(eo, "core", "uri", tgt)
        else:
            self.E(eo, "core", "name", tgt)
        if er3.findtext(f"{{{ns}}}relationType"):
            self.loss.add("externalReference/relationType (no 2.0 equivalent)", n.id)

    def lod_maps(self, n, g):
        """3.0/2.0 geometry by LoD for the 2.0 encoding."""
        ms = defaultdict(list)
        roles = {}
        for (lod, role), lst in g["ms"].items():
            if role in ("footprint", "roofedge"):
                roles[role] = [m for _rp, m in lst]
                continue
            ms[lod].extend(lst)
        for lod, lst in g["geom"].items():
            ms[lod].extend((None, m) for m in lst)
        mcs = defaultdict(list)
        mcid = {}
        for (lod, role), lst in g["mc"].items():
            mcs[lod].extend(lst)
            mcid.setdefault(lod, g["mc_id"].get((lod, role)))
        solids = {lod: lst[0] for lod, lst in g["solid"].items()}
        return ms, roles, mcs, mcid, solids

    def write_ms(self, el, prefix, local, lst):
        xl = [m for rp, m in lst if rp and rp.get("via_xlink")]
        if xl:
            self.href(self.E(el, prefix, local), xl[0].id, "MultiSurface")
            return
        self.multisurface(el, prefix, local, ms_nodes=[m for _rp, m in lst])

    def write_tic(self, el, prefix, ti, lod):
        lines = [ls for _rp, ls in ti.kids("HAS_LINE")]
        self.multicurve(el, prefix, f"lod{lod}TerrainIntersection", lines, ti.p.get("multi_curve_id"))

    def implicit(self, el, prefix, local, it, owner):
        prop = self.E(el, prefix, local)
        ig = self.E(prop, "core", "ImplicitGeometry")
        if it.get("mime"):
            self.E(ig, "core", "mimeType", it["mime"], codeSpace=it.get("mime_cs"))
        if it.get("matrix"):
            self.E(ig, "core", "transformationMatrix", " ".join(it["matrix"].split()))
        if it.get("lib"):
            self.E(ig, "core", "libraryObject", it["lib"])
        rg = self.E(ig, "core", "relativeGMLGeometry")
        Writer3.relative_geometry(self, rg, it, owner)
        self.point_into(self.E(ig, "core", "referencePoint"), it.get("ref") or "0 0 0",
                        it.get("ref_id"), it.get("ref_dim"))

    def geometry_slots(self, el, prefix, n, g, lods, implicit_lods=(), tic_lods=()):
        """lodNGeometry (+ lodNTerrainIntersection + lodNImplicitRepresentation) for the
        2.0 'geometry' modules (CityFurniture, SolitaryVegetationObject, GenericCityObject,
        bridge construction elements / installations): one geometry per LoD — a solid,
        else a MultiSurface, else a MultiCurve, else a point (others are reported)."""
        ms, roles, mcs, mcid, solids = self.lod_maps(n, g)
        for lod in sorted(set(ms) | set(mcs) | set(solids) | set(g["point"])):
            if lod not in lods:
                self.loss.add(f"lod{lod} geometry of {n.p.get('feature_type') or 'feature'} "
                              f"(no 2.0 lod{lod}Geometry)", n.id)
        for lod in lods:
            prop_local = f"lod{lod}Geometry"
            cands = [k for k, present in (("solid", lod in solids), ("ms", bool(ms.get(lod))),
                                          ("mc", bool(mcs.get(lod))), ("pt", lod in g["point"]))
                     if present]
            if not cands:
                continue
            if len(cands) > 1:
                self.loss.add(f"several lod{lod} geometries → only one lod{lod}Geometry in 2.0", n.id)
            k = cands[0]
            if k == "solid":
                self.solid(el, prefix, prop_local, solids[lod])
            elif k == "ms":
                self.write_ms(el, prefix, prop_local, ms[lod])
            elif k == "mc":
                self.multicurve(el, prefix, prop_local, mcs[lod], mcid.get(lod))
            else:
                pt = g["point"][lod]
                self.point_into(self.E(el, prefix, prop_local), pt.p.get("pos_list"),
                                None if pt.p.get("synthetic_id") else pt.id)
        for lod in tic_lods:
            if lod in g["tic"]:
                self.write_tic(el, prefix, g["tic"][lod], lod)
        items = implicit_items(n, g)
        for lod in implicit_lods:
            if lod in items:
                self.implicit(el, prefix, f"lod{lod}ImplicitRepresentation", items[lod], n)
        for lod in set(items) - set(implicit_lods):
            self.loss.add(f"lod{lod} implicit representation (not allowed here in 2.0)", n.id)

    # -- appearance (2.0 encoding) --------------------------------------------
    def appearance(self, holder, app):
        a = self.E(holder, "app", "Appearance")
        self.set_id(a, app.id, app.p.get("synthetic_id"))
        if app.p.get("gml_description"):
            self.E(a, "gml", "description", app.p["gml_description"])
        if app.p.get("gml_name"):
            self.E(a, "gml", "name", app.p["gml_name"])
        if app.p.get("theme") is not None:
            self.E(a, "app", "theme", app.p["theme"])
        for _rp, sd in app.kids("HAS_SURFACE_DATA"):
            self.surface_data(self.E(a, "app", "surfaceDataMember"), sd)

    _targets = Writer3._targets

    def surface_data(self, holder, sd):
        kind = sd.p.get("surface_data_type") or "X3DMaterial"
        el = self.E(holder, "app", kind)
        self.set_id(el, sd.id, sd.p.get("synthetic_id"))
        p = sd.p
        if p.get("is_front") is not None:
            self.E(el, "app", "isFront", p["is_front"])
        if kind == "X3DMaterial":
            for key, local in (("ambient_intensity", "ambientIntensity"), ("diffuse_color", "diffuseColor"),
                               ("emissive_color", "emissiveColor"), ("specular_color", "specularColor"),
                               ("shininess", "shininess"), ("transparency", "transparency"),
                               ("is_smooth", "isSmooth")):
                if p.get(key) is not None:
                    self.E(el, "app", local, p[key])
            for _rp, t in self._targets(sd):
                self.E(el, "app", "target", f"#{t.id}")
            return
        self.E(el, "app", "imageURI", p.get("image_uri") or "")
        if p.get("mime_type"):
            self.E(el, "app", "mimeType", p["mime_type"], codeSpace=p.get("mime_type_codespace"))
        for key, local in (("texture_type", "textureType"), ("wrap_mode", "wrapMode"),
                           ("border_color", "borderColor")):
            if p.get(key) is not None:
                self.E(el, "app", local, p[key])
        if kind == "ParameterizedTexture":
            for rp, t in self._targets(sd):
                tg = self.E(el, "app", "target", uri=f"#{t.id}")
                if rp.get("world_to_texture"):
                    self.E(self.E(tg, "app", "TexCoordGen"), "app", "worldToTexture", rp["world_to_texture"])
                else:
                    tcl = self.E(tg, "app", "TexCoordList")
                    for ring, coords in _loads(rp.get("texture_coordinates_json"), []):
                        self.E(tcl, "app", "textureCoordinates", coords, ring=f"#{ring}")
            return
        if p.get("prefer_world_file") is not None:
            self.E(el, "app", "preferWorldFile", p["prefer_world_file"])
        if p.get("reference_point"):
            self.point_into(self.E(el, "app", "referencePoint"), p["reference_point"])
        if p.get("orientation"):
            self.E(el, "app", "orientation", p["orientation"])
        for _rp, t in self._targets(sd):
            self.E(el, "app", "target", f"#{t.id}")

    # -- surfaces ------------------------------------------------------------------
    def surface_lods(self, s, g, allowed):
        """{lod: [(rp, ms_node)]} incl. the primary (HAS_POLYGON) MultiSurface."""
        out = defaultdict(list)
        polys = s.kids("HAS_POLYGON")
        if polys:
            prim = Node(None, ["GeometryMultiSurface"], {
                "id": s.p.get("multi_surface_id"), "synthetic_id": not s.p.get("multi_surface_id"),
                "composite_surface_id": s.p.get("composite_surface_id")})
            prim.out = [("HAS_SURFACE_MEMBER", rp, c) for rp, c in polys]
            out[str(s.p.get("lod") or "2")].append((None, prim))
        for (lod, _role), lst in g["ms"].items():
            out[lod].extend(lst)
        for lod in list(out):
            if lod not in allowed:
                self.loss.add(f"lod{lod}MultiSurface of a {s.p.get('surface_type')} (not in 2.0)", s.id)
                del out[lod]
        if g["mc"]:
            self.loss.add(f"lod0MultiCurve of a {s.p.get('surface_type')} (not in 2.0)", s.id)
        return out

    def bsurface(self, holder_parent, prefix, s):
        st = s.p.get("surface_type")
        local = st
        if prefix == "brid" and st not in _SURF2_BLDG:
            self.loss.add(f"{st} on a bridge (no 2.0 mapping)", s.id)
            return
        if prefix == "wtr":
            local = "WaterClosureSurface" if st == "ClosureSurface" else st
            if local not in ("WaterSurface", "WaterGroundSurface", "WaterClosureSurface"):
                self.loss.add(f"{st} on a WaterBody (no 2.0 mapping)", s.id)
                return
        if prefix == "bldg" and st not in _SURF2_BLDG:
            self.loss.add(f"{st} on a building (no 2.0 mapping)", s.id)
            return
        el = self.E(self.E(holder_parent, prefix, "boundedBy"), prefix, local)
        self.set_id(el, s.id, s.p.get("synthetic_id"))
        self.head(el, s)
        g = geoms(s)
        lods = self.surface_lods(s, g, ("2", "3", "4"))
        for lod in ("2", "3", "4"):
            if lods.get(lod):
                if prefix == "wtr":
                    prop = self.E(el, "wtr", f"lod{lod}Surface")
                    cs = self.E(prop, "gml", "CompositeSurface")
                    first = lods[lod][0][1]
                    if first.p.get("composite_surface_id") and first.p["composite_surface_id"] not in self.written:
                        self.set_id(cs, first.p["composite_surface_id"])
                    for _rp, m in lods[lod]:
                        for rp, c in m.kids("HAS_SURFACE_MEMBER"):
                            self.member(cs, rp, c)
                else:
                    self.write_ms(el, prefix, f"lod{lod}MultiSurface", lods[lod])
        if prefix == "wtr" and local == "WaterSurface":
            self.code(el, "wtr", "waterLevel", s.p.get("water_level"), s.p.get("water_level_codespace"))
        for _rp, o in s.kids("HAS_OPENING"):
            if prefix != "bldg":
                self.loss.add("opening on a non-building surface", o.id)
                continue
            self.opening(self.E(el, "bldg", "opening"), o)

    def opening(self, holder, o):
        kind = (o.p.get("opening_type") or "Window").replace("Surface", "")
        el = self.E(holder, "bldg", kind)
        self.set_id(el, o.id, o.p.get("synthetic_id"))
        self.head(el, o)
        g = geoms(o)
        for (lod, _r), lst in sorted(g["ms"].items()):
            if lod in ("3", "4"):
                self.write_ms(el, "bldg", f"lod{lod}MultiSurface", lst)
            else:
                self.loss.add(f"lod{lod} opening geometry (2.0 openings are LoD3/4)", o.id)

    # -- features ------------------------------------------------------------------
    def feature(self, holder, n):
        if self.clipped_away(n):
            return None
        lab = n.labels
        if "Building" in lab or "BuildingPart" in lab:
            return self.building(holder, n, part="BuildingPart" in lab)
        ft = n.p.get("feature_type")
        spec = _TOP2.get(ft)
        if spec is None:
            self.loss.add(f"feature type {ft} (no 2.0 writer)", n.id)
            return None
        prefix, local = spec
        if local != ft:
            self.loss.add(f"3.0 {ft} written as 2.0 {prefix}:{local}", n.id)
        el = self.E(holder, prefix, local)
        self.set_id(el, n.p.get("source_gml_id") or n.id, n.p.get("synthetic_id"))
        g = geoms(n)
        p = n.p
        if prefix == "tran":
            self.transport(el, n, g, local)
        elif local == "CityFurniture":
            self.head(el, n)
            self.cfu(el, "frn", p)
            self.geometry_slots(el, "frn", n, g, ("1", "2", "3", "4"), ("1", "2", "3", "4"), ("1", "2", "3", "4"))
        elif local == "SolitaryVegetationObject":
            self.head(el, n)
            self.cfu(el, "veg", p)
            self.code(el, "veg", "species", p.get("species"), p.get("species_codespace"))
            for key, loc in (("height", "height"), ("trunk_diameter", "trunkDiameter"),
                             ("crown_diameter", "crownDiameter")):
                self.measure(el, "veg", loc, p, key)
            for key in ("root_ball_diameter", "max_root_ball_depth"):
                if p.get(key):
                    self.loss.add(f"veg {key} (3.0 only)", n.id)
            self.geometry_slots(el, "veg", n, g, ("1", "2", "3", "4"), ("1", "2", "3", "4"))
        elif local == "PlantCover":
            self.head(el, n)
            self.cfu(el, "veg", p)
            self.measure(el, "veg", "averageHeight", p, "average_height")
            for key in ("min_height", "max_height"):
                if p.get(key):
                    self.loss.add(f"PlantCover {key} (3.0 only)", n.id)
            ms, _roles, mcs, _mcid, solids = self.lod_maps(n, g)
            for lod in ("1", "2", "3", "4"):
                if ms.get(lod):
                    self.write_ms(el, "veg", f"lod{lod}MultiSurface", ms[lod])
            for lod in ("1", "2", "3", "4"):
                if lod in solids:
                    prop = self.E(el, "veg", f"lod{lod}MultiSolid")
                    msol = self.E(prop, "gml", "MultiSolid")
                    self._solid_into(self.E(msol, "gml", "solidMember"), solids[lod])
            for lod in set(ms) - {"1", "2", "3", "4"}:
                self.loss.add(f"PlantCover lod{lod}MultiSurface (2.0 has lod1–4 only)", n.id)
            if mcs or g["point"] or implicit_items(n, g):
                self.loss.add("PlantCover curve/point/implicit geometry (not in 2.0)", n.id)
        elif local == "WaterBody":
            self.head(el, n)
            self.cfu(el, "wtr", p)
            ms, _roles, mcs, mcid, solids = self.lod_maps(n, g)
            for lod in ("0", "1"):
                if mcs.get(lod):
                    self.multicurve(el, "wtr", f"lod{lod}MultiCurve", mcs[lod], mcid.get(lod))
                if ms.get(lod):
                    self.write_ms(el, "wtr", f"lod{lod}MultiSurface", ms[lod])
            for lod in ("1", "2", "3", "4"):
                if lod in solids:
                    self.solid(el, "wtr", f"lod{lod}Solid", solids[lod])
            for lod in set(ms) - {"0", "1"}:
                self.loss.add(f"WaterBody lod{lod}MultiSurface (2.0: lod0/1 only)", n.id)
            for _rp, s in n.kids("HAS_BOUNDARY"):
                self.bsurface(el, "wtr", s)
        elif local == "LandUse":
            self.head(el, n)
            self.cfu(el, "luse", p)
            ms, *_ = self.lod_maps(n, g)
            for lod in ("0", "1", "2", "3", "4"):
                if ms.get(lod):
                    self.write_ms(el, "luse", f"lod{lod}MultiSurface", ms[lod])
        elif local == "Bridge":
            self.bridge(el, n, g)
        elif local == "GenericCityObject":
            self.head(el, n)
            self.cfu(el, "gen", p)
            if n.kids("HAS_BOUNDARY"):
                self.loss.add(f"boundary surfaces of a {ft} (GenericCityObject has none)", n.id)
            if p.get("measured_height") is not None:
                self.loss.add(f"con:height of a {ft} → GenericCityObject", n.id)
            self.geometry_slots(el, "gen", n, g, ("0", "1", "2", "3", "4"),
                                ("0", "1", "2", "3", "4"), ("0", "1", "2", "3", "4"))
        return el

    def cfu(self, el, prefix, p, cls_key="class", fn_key="function", us_key="usage"):
        self.code(el, prefix, "class", p.get(cls_key), p.get(f"{cls_key.replace('_code', '')}_codespace"))
        self.codes(el, prefix, "function", p, fn_key)
        self.codes(el, prefix, "usage", p, us_key)

    def transport(self, el, n, g, local):
        """3.0 Road/Section/Intersection/TrafficSpace hierarchy flattened to the 2.0
        TransportationComplex: every (Auxiliary)TrafficArea below the Road becomes a
        tran:trafficArea / tran:auxiliaryTrafficArea; all lod0 curves of the hierarchy
        become tran:lod0Network. Spaces, sections, intersections, markings, holes,
        clearance spaces, predecessor/successor and granularity have no 2.0 encoding:
        dropped (loss report) or, with v2_subfeatures='genattrs', kept as generic
        attributes on the areas / the road."""
        p = n.p
        extra = []
        if self.genattrs:
            for i, (_rp, m) in enumerate(n.kids("HAS_MARKING")):
                extra.append((f"citygml3_marking_{i}", f"{m.id} class={m.p.get('class', '')}"))
            for rel in ("HAS_SECTION", "HAS_INTERSECTION"):
                for _rp, c in n.kids(rel):
                    for i, (_r2, m) in enumerate(c.kids("HAS_MARKING")):
                        extra.append((f"citygml3_marking_{c.id}_{i}", f"{m.id} class={m.p.get('class', '')}"))
        self.head(el, n, extra)
        self.cfu(el, "tran", p)
        areas = traffic_areas(n)
        for kind, prop in (("TrafficArea", "trafficArea"), ("AuxiliaryTrafficArea", "auxiliaryTrafficArea")):
            for s, chain in areas:
                if s.p.get("surface_type") == kind and self.keep(s):
                    self.traffic_area(self.E(el, "tran", prop), s, chain[1:])
        for s, _chain in areas:
            if s.p.get("surface_type") == "HoleSurface":
                self.loss.add("tran:Hole / HoleSurface (no 2.0 equivalent)", s.id)
        self._count_dropped(n)
        lines = curves_below(n)
        if lines:
            net = self.E(el, "tran", "lod0Network")
            cx = self.E(net, "gml", "CompositeCurve")
            for ls in lines:
                cm = self.E(cx, "gml", "curveMember")
                le = self.E(cm, "gml", "LineString")
                self.set_id(le, ls.id, ls.p.get("synthetic_id"))
                self.poslist(le, ls)
            if len(lines) > len(geoms(n)["mc"].get(("0", "multicurve"), [])) + \
                    len(geoms(n)["mc"].get(("0", "network"), [])):
                self.loss.add("space-level lod0MultiCurve merged into the Road's lod0Network "
                              "(one gml:CompositeCurve)", n.id)
        ms, *_ = self.lod_maps(n, g)
        for lod in ("1", "2", "3", "4"):
            if ms.get(lod):
                self.write_ms(el, "tran", f"lod{lod}MultiSurface", ms[lod])
        if ms.get("0"):
            self.loss.add("transportation lod0MultiSurface (2.0: lod1–4)", n.id)

    def _count_dropped(self, n):
        for rel, what in (("HAS_SECTION", "tran:Section"), ("HAS_INTERSECTION", "tran:Intersection"),
                          ("HAS_TRAFFIC_SPACE", "tran:TrafficSpace / AuxiliaryTrafficSpace"),
                          ("HAS_MARKING", "tran:Marking"), ("HAS_HOLE", "tran:Hole")):
            for _rp, c in n.kids(rel):
                self.loss.add(f"{what} (flattened; no 2.0 equivalent)", c.id)
                for r, _t in refs_with_pending(c, "PREDECESSOR") + refs_with_pending(c, "SUCCESSOR"):
                    self.loss.add("predecessor/successor (no 2.0 equivalent)", c.id)
                for _r, _cs in c.kids("HAS_CLEARANCE_SPACE"):
                    self.loss.add("tran:ClearanceSpace (no 2.0 equivalent)", c.id)
                self._count_dropped(c)

    def traffic_area(self, holder, s, chain):
        kind = s.p.get("surface_type")
        el = self.E(holder, "tran", kind)
        self.set_id(el, s.id, s.p.get("synthetic_id"))
        extra = []
        if self.genattrs and chain:
            sp = chain[-1]
            extra.append(("citygml3_space", f"{sp.p.get('feature_type', '')} {sp.id}"))
            for key in ("granularity", "traffic_direction", "function", "usage", "class"):
                if sp.p.get(key):
                    extra.append((f"citygml3_space_{key}", " ".join(sp.p.get(f"{key}_list") or [sp.p[key]])))
            for c in chain[:-1]:
                extra.append((f"citygml3_{(c.p.get('feature_type') or 'parent').lower()}", c.id))
        self.head(el, s, extra)
        p = s.p
        self.code(el, "tran", "class", p.get("class"), p.get("class_codespace"))
        self.codes(el, "tran", "function", p, "function")
        self.codes(el, "tran", "usage", p, "usage")
        self.code(el, "tran", "surfaceMaterial", p.get("surface_material"), p.get("surface_material_codespace"))
        lods = self.surface_lods(s, geoms(s), ("2", "3", "4"))
        for lod in ("2", "3", "4"):
            if lods.get(lod):
                self.write_ms(el, "tran", f"lod{lod}MultiSurface", lods[lod])

    def bridge(self, el, n, g, part=False):
        p = n.p
        self.head(el, n)
        self.cfu(el, "brid", p)
        if p.get("year_of_construction"):
            self.E(el, "brid", "yearOfConstruction", str(p["year_of_construction"])[:4])
        if p.get("year_of_demolition"):
            self.E(el, "brid", "yearOfDemolition", str(p["year_of_demolition"])[:4])
        if p.get("is_movable") not in (None, ""):
            self.E(el, "brid", "isMovable", str(p["is_movable"]).strip().lower())
        if p.get("measured_height") is not None:
            self.loss.add("con:height of a Bridge (no 2.0 equivalent)", n.id)
        ms, _roles, mcs, mcid, solids = self.lod_maps(n, g)

        def lod_block(lod):
            if lod in solids:
                self.solid(el, "brid", f"lod{lod}Solid", solids[lod])
            if ms.get(lod):
                self.write_ms(el, "brid", f"lod{lod}MultiSurface", ms[lod])
            if lod != "1" and mcs.get(lod):
                self.multicurve(el, "brid", f"lod{lod}MultiCurve", mcs[lod], mcid.get(lod))
            if lod in g["tic"]:
                self.write_tic(el, "brid", g["tic"][lod], lod)

        lod_block("1")
        lod_block("2")
        for _rp, nf in n.kids("HAS_NESTED"):
            kind = nf.p.get("feature_kind") or ""
            if kind in ("BridgeConstructiveElement", "BridgeConstructionElement"):
                self.nested(self.E(el, "brid", "outerBridgeConstruction"), nf, "BridgeConstructionElement")
        for _rp, nf in n.kids("HAS_NESTED"):
            if (nf.p.get("feature_kind") or "") == "BridgeInstallation":
                self.nested(self.E(el, "brid", "outerBridgeInstallation"), nf, "BridgeInstallation")
        for _rp, s in n.kids("HAS_BOUNDARY"):
            self.bsurface(el, "brid", s)
        lod_block("3")
        lod_block("4")
        for lod in set(ms) | set(mcs):
            if lod == "0":
                self.loss.add("Bridge lod0 geometry (2.0: lod1–4)", n.id)
        if not part:
            for _rp, bp in n.kids("HAS_BRIDGE_PART"):
                bel = self.E(self.E(el, "brid", "consistsOfBridgePart"), "brid", "BridgePart")
                self.set_id(bel, bp.id, bp.p.get("synthetic_id"))
                self.bridge(bel, bp, geoms(bp), part=True)
        self.address2(el, "brid", n)

    def nested(self, holder, nf, local):
        el = self.E(holder, "brid", local)
        self.set_id(el, nf.id, nf.p.get("synthetic_id"))
        self.head(el, nf)
        self.cfu(el, "brid", nf.p)
        g = geoms(nf)
        if local == "BridgeConstructionElement":
            self.geometry_slots(el, "brid", nf, g, ("1", "2", "3", "4"), ("1", "2", "3", "4"),
                                ("1", "2", "3", "4"))
        else:
            self.geometry_slots(el, "brid", nf, g, ("2", "3", "4"), ("2", "3", "4"))
        for _rp, s in nf.kids("HAS_BOUNDARY"):
            self.bsurface(el, "brid", s)

    def address2(self, el, prefix, n):
        for f in _loads(n.p.get("address_xml"), []):
            if isinstance(f, str) and _frag_ns(f) == self.P.ns[prefix]:
                self.verbatim(el, f, "address")
                continue
            d = xal3_to_xal2(f)
            if d is None:
                self.loss.add("address (not convertible to xAL 2)", n.id)
                continue
            a = self.E(self.E(el, prefix, "address"), "core", "Address")
            det = self.E(self.E(a, "core", "xalAddress"), "xAL", "AddressDetails")
            _write_xal2(self, det, d)
            self.loss.add("address converted xAL 3 → xAL 2 (main fields only)", n.id)

    def building(self, holder, n, part=False):
        el = self.E(holder, "bldg", "BuildingPart" if part else "Building")
        self.set_id(el, n.p.get("source_gml_id") or n.id, n.p.get("synthetic_id"))
        p = n.p
        self.head(el, n)
        self.code(el, "bldg", "class", p.get("class_code"), p.get("class_codespace"))
        for key, local in (("function_code", "function"), ("usage_code", "usage")):
            vals = p.get(f"{key}_list") or ([p[key]] if p.get(key) else [])
            for v in vals:
                self.code(el, "bldg", local, v, p.get(f"{key.replace('_code', '')}_codespace"))
        if p.get("year_of_construction"):
            self.E(el, "bldg", "yearOfConstruction", str(p["year_of_construction"])[:4])
        if p.get("year_of_demolition"):
            self.E(el, "bldg", "yearOfDemolition", str(p["year_of_demolition"])[:4])
        for key in ("date_of_construction", "date_of_demolition", "condition_of_construction"):
            if p.get(key) and key != "date_of_construction":
                self.loss.add(f"con:{key} (2.0 keeps the year only)", n.id)
        self.code(el, "bldg", "roofType", p.get("roof_type_code"), p.get("roof_type_codespace"))
        if p.get("measured_height") is not None:
            self.E(el, "bldg", "measuredHeight", p["measured_height"],
                   uom=p.get("measured_height_uom") or "m")
            if p.get("height_high_reference"):
                self.loss.add("con:Height high/low reference + status (2.0 measuredHeight has none)", n.id)
            if len(_loads(p.get("heights_json"), [])) > 1:
                self.loss.add("additional con:Height entries (2.0 has one measuredHeight)", n.id)
        for key, local in (("storeys_above_ground", "storeysAboveGround"),
                           ("storeys_below_ground", "storeysBelowGround")):
            v = p.get(key)
            if v is not None:
                if int(v) < 0:
                    self.loss.add(f"negative {local} (2.0 nonNegativeInteger)", n.id)
                else:
                    self.E(el, "bldg", local, v)
        for key, local in (("storey_heights_above_ground", "storeyHeightsAboveGround"),
                           ("storey_heights_below_ground", "storeyHeightsBelowGround")):
            if p.get(key):
                self.E(el, "bldg", local, p[key], uom=p.get(f"{key}_uom") or "m")
        g = geoms(n)
        ms, roles, mcs, mcid, solids = self.lod_maps(n, g)
        if roles.get("footprint") or ms.get("0"):
            lst = [(None, m) for m in roles.get("footprint", [])] or ms.get("0")
            self.write_ms(el, "bldg", "lod0FootPrint", lst)
            if roles.get("footprint") is None and ms.get("0"):
                self.loss.add("3.0 Building lod0MultiSurface → 2.0 lod0FootPrint (policy)", n.id)
        if roles.get("roofedge"):
            self.write_ms(el, "bldg", "lod0RoofEdge", [(None, m) for m in roles["roofedge"]])

        def lod_block(lod):
            if lod in solids:
                self.solid(el, "bldg", f"lod{lod}Solid", solids[lod])
            if ms.get(lod):
                self.write_ms(el, "bldg", f"lod{lod}MultiSurface", ms[lod])
            if lod != "1" and mcs.get(lod):
                self.multicurve(el, "bldg", f"lod{lod}MultiCurve", mcs[lod], mcid.get(lod))
            if lod in g["tic"]:
                self.write_tic(el, "bldg", g["tic"][lod], lod)

        lod_block("1")
        lod_block("2")
        if mcs.get("0") or mcs.get("1"):
            self.loss.add("Building lod0/1 MultiCurve (2.0: lod2–4)", n.id)
        if implicit_items(n, g):
            self.loss.add("Building implicit representation (not in 2.0)", n.id)
        for rel, prop in (("HAS_OUTER_INSTALLATION", "outerBuildingInstallation"),):
            for _rp, inst in n.kids(rel):
                self.installation(self.E(el, "bldg", prop), inst)
        for _rp, s in n.kids("HAS_BOUNDARY"):
            self.bsurface(el, "bldg", s)
        lod_block("3")
        lod_block("4")
        if n.kids("HAS_INTERIOR_ROOM"):
            self.loss.add("building rooms in a 3.0 → 2.0 export (not written)", n.id)
        if not part:
            for _rp, bp in n.kids("HAS_BUILDING_PART"):
                self.building(self.E(el, "bldg", "consistsOfBuildingPart"), bp, part=True)
        self.address2(el, "bldg", n)
        return el

    def installation(self, holder, inst):
        el = self.E(holder, "bldg", "BuildingInstallation")
        self.set_id(el, inst.id, inst.p.get("synthetic_id"))
        self.head(el, inst)
        self.cfu(el, "bldg", inst.p, "class_code", "function_code", "usage_code")
        g = geoms(inst)
        self.geometry_slots(el, "bldg", inst, g, ("2", "3", "4"), ("2", "3", "4"))
        for _rp, s in inst.kids("HAS_BOUNDARY"):
            self.bsurface(el, "bldg", s)


# ---------------------------------------------------------------------------
# document driver
# ---------------------------------------------------------------------------

_NS_ORDER = ("core", "gml", "xlink", "xsi", "xAL", "app", "bldg", "brid", "con", "frn", "gen",
             "luse", "tran", "veg", "wtr")


def _fmt(v):
    v = float(v)
    return str(int(v)) if v == int(v) else repr(v)


def select_features(sess, dataset_id, bbox_wkt=None, layer=None):
    """Top-level features of a Dataset (PART_OF — not the per-file `source`, which a
    tiled ingest overwrites per tile): Buildings first, then CityObjects by type, id."""
    if bbox_wkt:
        rows = sess.run(
            "CALL spatial.intersects($layer, $wkt) YIELD node AS n "
            "MATCH (n)-[:PART_OF]->(:Dataset {id: $did}) "
            "RETURN DISTINCT n.id AS id, n:Building AS b, coalesce(n.feature_type, '') AS ft",
            layer=layer or "features", wkt=bbox_wkt, did=dataset_id).data()
    else:
        rows = sess.run(
            "MATCH (d:Dataset {id: $did})<-[:PART_OF]-(n) WHERE n:Building OR n:CityObject "
            "RETURN n.id AS id, n:Building AS b, coalesce(n.feature_type, '') AS ft",
            did=dataset_id).data()
    rows.sort(key=lambda r: (not r["b"], r["ft"], r["id"]))
    return [r["id"] for r in rows]


def wkt_box(wkt):
    """(minx, miny, maxx, maxy) of the coordinates in a WKT polygon."""
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", wkt or "")]
    xs, ys = nums[0::2], nums[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def plan_ids(sess, top_ids, box=None, chunk=500):
    """The feature-level ids the document will contain: the selected top-level features
    and their sub-features / surfaces / openings (with --clip, below transportation
    features only those whose bbox meets the box, like GraphTree), plus the ids of the
    surfaces' MultiSurfaces (xlink targets). Used to decide whether a reference
    (relatedTo, predecessor/successor, an xlinked MultiSurface) can stay local."""
    plan = set(top_ids)
    # level-wise BFS over the containment edges of the SELECTED features only (typed
    # expansion, the clip test in the database — the same rule as GraphTree), so the
    # work is proportional to the kept subtree, never to a city-wide Road's descendants
    q = ("UNWIND $ids AS i MATCH (a:Entity {id: i})-[r:" + "|".join(FEATURE_RELS) + "]->(c) "
         "WHERE $box IS NULL OR NOT (type(r) IN $clip "
         "      AND any(l IN labels(a) WHERE l IN $tlabels)) "
         "   OR c.bbox_min_x IS NULL OR NOT (c.bbox_max_x < $box[0] OR c.bbox_min_x > $box[2] "
         "      OR c.bbox_max_y < $box[1] OR c.bbox_min_y > $box[3]) "
         "RETURN DISTINCT c.id AS id, c.multi_surface_id AS ms")
    frontier = list(top_ids)
    while frontier:
        nxt = []
        for i in range(0, len(frontier), chunk):
            for r in sess.run(q, ids=frontier[i:i + chunk], box=list(box) if box else None,
                              clip=list(CLIP_RELS), tlabels=list(TRANSPORT_LABELS)):
                if r["id"] not in plan:
                    plan.add(r["id"])
                    nxt.append(r["id"])
                if r["ms"]:
                    plan.add(r["ms"])
        frontier = nxt
    return plan


def drop_unwritten_targets(w, el, area_limited=False):
    """Remove appearance targets whose geometry was not written (clipped away, outside
    the selected area, or not expressible in the target version) — materials only for
    written polygons. In an area-limited export (--bbox / id list) a target outside the
    area is an export statistic, not a loss."""
    app_ns = w.P.ns["app"]
    for t in list(el.iter(f"{{{app_ns}}}target")):
        ref = (t.get("uri") or t.text or "").strip()
        if href_fragment(ref) in w.written:
            continue
        parent = t.getparent()
        if area_limited:
            w.stats["appearance targets outside the exported area (removed)"] += 1
        else:
            w.loss.add("appearance target whose geometry is not in this document", href_fragment(ref))
        if etree.QName(parent).localname == "TextureAssociation":   # 3.0
            wrapper = parent.getparent()
            wrapper.getparent().remove(wrapper)
        else:
            parent.remove(t)


def dataset_appearances(sess, w, dataset_id, area_limited=True, chunk=5000):
    """CityModel-level appearances for exactly the geometry written to this document.
    Street-space Datasets merge one Appearance per tile (hundreds in a city) that
    share a few dozen X3DMaterials with millions of TARGETS edges, so nothing is loaded
    from the Appearance side: the TARGETS edges are looked up from the written geometry
    ids (batched, index-backed), only the materials hit are fetched, and each material is
    written once, inside its host Appearance (the Appearance with the smallest id that
    holds it, original surfaceData order). A single-file Dataset therefore round-trips
    its appearanceMembers unchanged; a tiled one collapses to the few host Appearances.
    A whole-Dataset export (not area_limited) also keeps materials without written
    targets and reports targets whose geometry was not written as a loss (counted from
    the relationship degree, never loaded). Work and memory scale with the selection.
    Returns a list of Node trees (Appearance -> surface data -> TARGETS refs)."""
    ds_sd = {r["sd"]: r for r in sess.run(
        "MATCH (:Dataset {id: $did})-[:HAS_APPEARANCE]->(a:Appearance)-[h:HAS_SURFACE_DATA]->(sd) "
        "WITH sd, a, h ORDER BY a.id "
        "WITH sd, collect({app: a.id, ord: h.order})[0] AS f "
        "RETURN sd.id AS sd, f.app AS app, f.ord AS ord", did=dataset_id)}
    if not ds_sd:
        return []
    targets = defaultdict(list)            # material id -> [(edge props, target id)]
    written = [i for i in w.written if i]
    for i in range(0, len(written), chunk):
        for r in sess.run(
                "UNWIND $ids AS i MATCH (g:Entity {id: i})<-[t:TARGETS]-(sd) "
                "RETURN sd.id AS sd, i AS tgt, properties(t) AS tp", ids=written[i:i + chunk]):
            if r["sd"] in ds_sd:
                targets[r["sd"]].append((r["tp"], r["tgt"]))
    mats = set(ds_sd) if not area_limited else set(targets)
    if not mats:
        return []
    sds = {r["id"]: r for r in sess.run(
        "UNWIND $ids AS i MATCH (sd:Entity {id: i}) RETURN sd.id AS id, labels(sd) AS l, "
        "properties(sd) AS p, COUNT { (sd)-[:TARGETS]->() } AS deg", ids=sorted(mats))}
    hosts = sorted({ds_sd[m]["app"] for m in mats})
    apps_meta = {r["id"]: r["p"] for r in sess.run(
        "UNWIND $ids AS i MATCH (a:Appearance {id: i}) RETURN a.id AS id, properties(a) AS p",
        ids=hosts)}
    by_host = defaultdict(list)
    for m in mats:
        by_host[ds_sd[m]["app"]].append(m)
    out = []
    for host in hosts:
        app = Node(None, ["Appearance"], dict(apps_meta.get(host) or {"id": host}))
        ordered = sorted(by_host[host], key=lambda m: (ds_sd[m]["ord"] is None,
                                                        ds_sd[m]["ord"] or 0, m))
        for k, m in enumerate(ordered):
            meta = sds.get(m)
            if meta is None:
                continue
            sd = Node(None, meta["l"], meta["p"])
            sd.out = [("TARGETS", tp, _Ref(tgt)) for tp, tgt in targets.get(m, ())]
            missing = (meta["deg"] or 0) - len(sd.out)
            if missing > 0 and not area_limited:
                w.loss.add("appearance target whose geometry is not in this document", m, n=missing)
            app.out.append(("HAS_SURFACE_DATA", {"order": ds_sd[m]["ord"] if ds_sd[m]["ord"] is not None else k}, sd))
        out.append(app)
    w.stats["dataset Appearances written (one per host, materials once)"] = len(out)
    w.stats["appearance targets written"] = sum(len(v) for v in targets.values())
    return out


def export_document(sess, dataset, version, out_path, opts=None, feature_ids=None,
                    bbox_wkt=None, clip=None, batch=200):
    """Write `dataset` (a Dataset node's properties) as CityGML `version` ('2.0'/'3.0')
    with the strict writer, STREAMED: top-level features are loaded, written and
    released in batches of `batch` (lxml.etree.xmlfile), so memory is bounded by the
    batch, not by the document. With bbox_wkt the R-tree selects the top-level features;
    clip (default: on with bbox_wkt) additionally prunes the transportation hierarchy to
    the sub-features whose bbox meets the box. Returns (counts, loss_report_dict)."""
    import resource
    import time as _time
    t0 = _time.perf_counter()
    prof = []

    def mark(phase):
        prof.append((phase, round(_time.perf_counter() - t0, 2),
                     round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)))

    opts = dict(opts or {})
    box = wkt_box(bbox_wkt) if bbox_wkt else None
    if clip is None:
        clip = box is not None
    clip = bool(clip and box is not None)
    opts["clip"] = clip
    W = Writer3 if str(version).startswith("3") else Writer2
    w = W(dataset, opts)
    ids = feature_ids if feature_ids is not None else select_features(
        sess, dataset["id"], bbox_wkt, dataset.get("rtree_layer"))
    mark(f"select ({len(ids)} top-level features)")
    w.doc_ids = plan_ids(sess, ids, box if clip else None)
    mark(f"plan ({len(w.doc_ids)} ids)")
    area_limited = box is not None or feature_ids is not None
    proto_tree = GraphTree(sess)

    P = w.P
    nsmap = {p: P.ns[p] for p in _NS_ORDER if p in P.ns}
    root_attrib = {f"{{{XSI_NS}}}schemaLocation": P.schema_location(
        [p for p in nsmap if p not in ("core", "gml", "xlink", "xsi", "xAL")])}
    if dataset.get("gml_id"):
        root_attrib[P.gml_id] = dataset["gml_id"]
        w.written.add(dataset["gml_id"])
    head = etree.Element(P.q("core", "CityModel"), nsmap=nsmap)
    if dataset.get("gml_description"):
        w.E(head, "gml", "description", dataset["gml_description"])
    if dataset.get("gml_name"):
        w.E(head, "gml", "name", dataset["gml_name"])
    if dataset.get("bbox_lower_corner") and dataset.get("bbox_min_x") is not None:
        lc, uc = dataset["bbox_lower_corner"], dataset.get("bbox_upper_corner")
        try:
            lcv = [float(v) for v in lc.split()]
            ucv = [float(v) for v in uc.split()]
        except (AttributeError, ValueError):
            lcv = ucv = None
        num_l = [dataset.get(k) for k in ("bbox_min_x", "bbox_min_y", "bbox_min_z")]
        num_u = [dataset.get(k) for k in ("bbox_max_x", "bbox_max_y", "bbox_max_z")]
        if lcv and ucv and lcv[:3] == num_l[:len(lcv)] and ucv[:3] == num_u[:len(ucv)]:
            w.envelope(head, lc, uc)                    # single file: verbatim corners
        else:                                           # tiled Dataset: the union
            w.envelope(head, " ".join(_fmt(v) for v in num_l if v is not None),
                       " ".join(_fmt(v) for v in num_u if v is not None))
    counts = Counter()
    member_tag = P.q("core", "cityObjectMember")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh, etree.xmlfile(fh, encoding="UTF-8") as xf:
        xf.write_declaration()
        with xf.element(P.q("core", "CityModel"), attrib=root_attrib, nsmap=nsmap):
            for child in head:
                etree.indent(child, space="  ", level=1)
                xf.write("\n  ")
                xf.write(child)
            for bi in range(0, len(ids), batch):
                tree = GraphTree(sess, clip_box=box if clip else None, plan=w.doc_ids)
                tops = tree.load(ids[bi:bi + batch])
                need = sorted({c.id for n in tree.nodes.values() for t, _rp, c in n.out
                               if t == "HAS_PROTOTYPE" and "GeometryPrototype" in c.labels}
                              - set(w.proto_nodes))
                if need:
                    for pnode in proto_tree.load(need):
                        w.proto_nodes[pnode.id] = pnode
                        w.proto_ids.add(pnode.id)
                for n in tops:
                    m = etree.Element(member_tag, nsmap=nsmap)
                    el = w.feature(m, n)
                    if el is None or not len(m):
                        continue
                    drop_unwritten_targets(w, m, area_limited)
                    counts[etree.QName(el).localname] += 1
                    etree.indent(m, space="  ", level=1)
                    xf.write("\n  ")
                    xf.write(m)
                w.stats["sub-features clipped (outside the box)"] += tree.clipped
                w.stats["sub-features without bbox (kept)"] += tree.unclippable
                w._content.clear()
                del tree, tops
            mark(f"features ({sum(counts.values())} written)")
            for a in dataset_appearances(sess, w, dataset["id"], area_limited):
                holder = etree.Element(P.appearance_member, nsmap=nsmap)
                w.appearance(holder, a)
                drop_unwritten_targets(w, holder, area_limited)
                etree.indent(holder, space="  ", level=1)
                xf.write("\n  ")
                xf.write(holder)
                counts["appearanceMember"] += 1
            mark("appearances")
            xf.write("\n")
    for h, _ctx in w.hrefs:
        if h not in w.written:
            w.loss.add("dangling local reference (target not in this document)", h)
    loss = w.loss.as_dict()
    loss["export_stats"] = dict(w.stats)
    loss["profile"] = [{"phase": ph, "t_s": t, "peak_rss_mb": m} for ph, t, m in prof]
    return dict(counts), loss
