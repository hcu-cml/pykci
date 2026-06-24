#!/usr/bin/env python3
"""
Precise CityGML 2.0 inventory script using lxml.
"""
import sys
from collections import defaultdict
from lxml import etree

# Default GML when no path is given on the command line; any CityGML 2.0 file works.
# Relative to the project root — run this script from there.
DEFAULT_GML_FILE = "input/citygml/Railway_Scene_LoD3/Railway_Scene_LoD3.gml"
GML_FILE = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GML_FILE

# ── Namespace maps ────────────────────────────────────────────────────────────
NS_GML  = "http://www.opengis.net/gml"
NS_CORE = "http://www.opengis.net/citygml/2.0"

# Known CityGML 2.0 module namespaces
CITYGML_MODULES = {
    "http://www.opengis.net/citygml/2.0":                       "core",
    "http://www.opengis.net/citygml/building/2.0":              "bldg",
    "http://www.opengis.net/citygml/bridge/2.0":                "brid",
    "http://www.opengis.net/citygml/tunnel/2.0":                "tun",
    "http://www.opengis.net/citygml/transportation/2.0":        "tran",
    "http://www.opengis.net/citygml/vegetation/2.0":            "veg",
    "http://www.opengis.net/citygml/waterbody/2.0":             "wtr",
    "http://www.opengis.net/citygml/landuse/2.0":               "luse",
    "http://www.opengis.net/citygml/relief/2.0":                "dem",
    "http://www.opengis.net/citygml/cityobjectgroup/2.0":       "grp",
    "http://www.opengis.net/citygml/appearance/2.0":            "app",
    "http://www.opengis.net/citygml/generics/2.0":              "gen",
    "http://www.opengis.net/citygml/textureparam/2.0":          "tex",
}

# Top-level city feature elements in transportation module
TRAN_FEATURES = {"Road", "Railway", "Square", "Track", "RailwayStopArea",
                 "TransportationComplex", "AuxiliaryTrafficArea",
                 "TrafficArea", "RailwayLine", "Section"}

BLDG_FEATURES = {"Building", "BuildingPart", "BuildingInstallation",
                 "IntBuildingInstallation", "Room", "Door", "Window",
                 "CeilingSurface", "ClosureSurface", "FloorSurface",
                 "GroundSurface", "InteriorWallSurface", "OuterCeilingSurface",
                 "OuterFloorSurface", "RoofSurface", "WallSurface"}

def tag(ns, local):
    return f"{{{ns}}}{local}"

def local(qname):
    return etree.QName(qname).localname

def nsuri(qname):
    return etree.QName(qname).namespace

def gml_id(elem):
    return elem.get(tag(NS_GML, "id"))

def find_lod_geometries(elem):
    """Return a list of lod*Geometry / lod*Solid / lod*MultiSurface tag local names present."""
    found = []
    for child in elem.iter():
        lname = local(child.tag) if child.tag and child.tag[0] == '{' else None
        if lname and lname.startswith("lod") and (
            lname.endswith("Geometry") or lname.endswith("Solid") or
            lname.endswith("MultiSurface") or lname.endswith("MultiCurve") or
            lname.endswith("TerrainIntersection")
        ):
            found.append(lname)
    return list(set(found))

# ── Parse ─────────────────────────────────────────────────────────────────────
print("Parsing …", flush=True)
tree = etree.parse(GML_FILE)
root = tree.getroot()

# ── 1. srsName ────────────────────────────────────────────────────────────────
envelope = root.find(f".//{{{NS_GML}}}Envelope")
srsname = envelope.get("srsName") if envelope is not None else "NOT FOUND"
print(f"\n{'='*70}")
print("1. srsName from gml:Envelope")
print(f"{'='*70}")
print(f"   {srsname}")

# ── Collect all namespaces actually used ──────────────────────────────────────
all_ns_used = set()
for elem in root.iter():
    if elem.tag and elem.tag[0] == '{':
        all_ns_used.add(nsuri(elem.tag))

# ── 2. Module inventory ───────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("2. CityGML 2.0 module namespaces present")
print(f"{'='*70}")

# Map: module_uri -> { local_name -> [gml:id, ...] }
module_features = defaultdict(lambda: defaultdict(list))

for elem in root.iter():
    if not elem.tag or elem.tag[0] != '{':
        continue
    ns = nsuri(elem.tag)
    if ns not in CITYGML_MODULES:
        continue
    lname = local(elem.tag)
    eid = gml_id(elem)
    if eid:
        module_features[ns][lname].append(eid)

for ns, prefix in sorted(CITYGML_MODULES.items(), key=lambda x: x[1]):
    if ns not in all_ns_used:
        continue
    print(f"\n  Namespace : {ns}  (prefix: {prefix})")
    if ns in module_features:
        for fname in sorted(module_features[ns]):
            ids = module_features[ns][fname]
            count = len(ids)
            display = ids[:5]
            suffix = f" … (+{count-5} more)" if count > 5 else ""
            print(f"    {fname:40s}  count={count}")
            print(f"      ids: {display}{suffix}")
    else:
        print("    (elements present but none with gml:id)")

# ── 3. Building details ───────────────────────────────────────────────────────
BLDG_NS = "http://www.opengis.net/citygml/building/2.0"
GEN_NS  = "http://www.opengis.net/citygml/generics/2.0"

print(f"\n{'='*70}")
print("3. Building details")
print(f"{'='*70}")

buildings = root.findall(f".//{{{BLDG_NS}}}Building")
print(f"  Total Building elements: {len(buildings)}")

for bldg in buildings:
    bid = gml_id(bldg)
    print(f"\n  Building gml:id = {bid}")

    # lod*Solid
    solids = [local(e.tag) for e in bldg.iter()
              if e.tag and local(e.tag).startswith("lod") and local(e.tag).endswith("Solid")]
    print(f"    lod*Solid          : {sorted(set(solids)) or 'none'}")

    # lod*MultiSurface
    msurf = [local(e.tag) for e in bldg.iter()
             if e.tag and local(e.tag).startswith("lod") and local(e.tag).endswith("MultiSurface")]
    print(f"    lod*MultiSurface   : {sorted(set(msurf)) or 'none'}")

    # boundedBy surface types
    bounded = bldg.findall(f".//{{{BLDG_NS}}}boundedBy")
    surf_types = defaultdict(int)
    for b in bounded:
        for child in b:
            surf_types[local(child.tag)] += 1
    print(f"    boundedBy surfaces : {dict(surf_types) or 'none'}")

    # gen:*Attribute
    gen_attrs = set()
    for ga in bldg.iter():
        if ga.tag and nsuri(ga.tag) == GEN_NS and "Attribute" in local(ga.tag):
            name_el = ga.find(f"{{{GEN_NS}}}name")
            if name_el is not None and name_el.text:
                gen_attrs.add(f"{local(ga.tag)}[{name_el.text.strip()}]")
    print(f"    gen:*Attributes    : {sorted(gen_attrs) or 'none'}")

    # bldg:function
    functions = [e.text.strip() for e in bldg.findall(f"{{{BLDG_NS}}}function") if e.text]
    print(f"    bldg:function      : {functions or 'none'}")

    # bldg:roofType
    rooftypes = [e.text.strip() for e in bldg.findall(f"{{{BLDG_NS}}}roofType") if e.text]
    print(f"    bldg:roofType      : {rooftypes or 'none'}")

# ── 4. Non-building city features ─────────────────────────────────────────────
print(f"\n{'='*70}")
print("4. Non-building feature details")
print(f"{'='*70}")

# City object feature namespaces excluding building
NON_BLDG_MODULES = {ns: p for ns, p in CITYGML_MODULES.items()
                    if p not in ("bldg", "core")}

# Collect top-level cityObjectMember children
city_obj_members = root.findall(f".//{{{NS_CORE}}}cityObjectMember")

seen_non_bldg = []
for com in city_obj_members:
    for child in com:
        if not child.tag or child.tag[0] != '{':
            continue
        ns = nsuri(child.tag)
        if ns == BLDG_NS:
            continue  # already handled
        lname = local(child.tag)
        eid = gml_id(child)
        lod_geoms = find_lod_geometries(child)
        seen_non_bldg.append((ns, lname, eid, lod_geoms))

if not seen_non_bldg:
    print("  None found at cityObjectMember level — scanning all elements …")
    for ns, prefix in NON_BLDG_MODULES.items():
        for elem in root.findall(f".//{{{ns}}}*"):
            lname = local(elem.tag)
            eid = gml_id(elem)
            if eid:
                lod_geoms = find_lod_geometries(elem)
                seen_non_bldg.append((ns, lname, eid, lod_geoms))

for ns, lname, eid, lod_geoms in seen_non_bldg:
    prefix = CITYGML_MODULES.get(ns, ns)
    print(f"\n  {prefix}:{lname}  gml:id={eid}")
    print(f"    LoD geometries present: {sorted(set(lod_geoms)) or 'none'}")

# ── 5. xlink:href targets ─────────────────────────────────────────────────────
XLINK_NS = "http://www.w3.org/1999/xlink"
href_attr = f"{{{XLINK_NS}}}href"

print(f"\n{'='*70}")
print("5. xlink:href targets (referenced gml:id values)")
print(f"{'='*70}")

hrefs = set()
for elem in root.iter():
    h = elem.get(href_attr)
    if h and h.startswith("#"):
        hrefs.add(h[1:])  # strip leading '#'

# Also collect all gml:ids
all_ids = set()
for elem in root.iter():
    eid = gml_id(elem)
    if eid:
        all_ids.add(eid)

print(f"  Total xlink:href references found : {len(hrefs)}")
print(f"  Unique referenced gml:id values   : {len(hrefs)}")
for h in sorted(hrefs):
    exists = "OK defined" if h in all_ids else "NOT FOUND in file"
    print(f"    #{h}  ({exists})")

# ── 6. Total gml:Polygon count ────────────────────────────────────────────────
print(f"\n{'='*70}")
print("6. Total gml:Polygon count")
print(f"{'='*70}")
poly_count = sum(1 for _ in root.iter(tag(NS_GML, "Polygon")))
print(f"  gml:Polygon elements: {poly_count}")

print(f"\n{'='*70}")
print("Done.")
