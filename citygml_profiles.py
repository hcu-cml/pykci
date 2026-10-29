"""
citygml_profiles.py — version profiles for the CityGML 2.0 and 3.0 codecs.

Ingest (ingest_citygml.py) and export (export_citygml.py) are codecs around ONE
version-neutral graph model (see GRAPH.md / CITYGML3.md). Everything that differs
between the two encodings lives here as data, not as scattered string literals:

  * namespace URIs per module (+ GML 3.1.1 vs 3.2, xAL 2 vs xAL 3),
  * structural names: where LoD geometry properties live (module vs core),
    the space-boundary property (bldg:boundedBy vs core:boundary), the Solid
    exterior surface (gml:CompositeSurface vs gml:Shell), the implicit-geometry
    property (relativeGMLGeometry vs relativeGeometry), the terrain-intersection
    property name, the city-model member wrappers,
  * schemaLocation strings for export.

Namespace wildcarding ("{*}local") is deliberately NOT used: the 3.0 structure
differs (LoD properties moved to core:, boundaries are core:boundary, building
surfaces are con:*), so the codecs ask the profile for the exact qualified name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

_MODULES_2 = {
    "app": "appearance", "bldg": "building", "brid": "bridge", "frn": "cityfurniture",
    "grp": "cityobjectgroup", "gen": "generics", "luse": "landuse", "dem": "relief",
    "tran": "transportation", "tun": "tunnel", "veg": "vegetation", "wtr": "waterbody",
}
_MODULES_3 = dict(_MODULES_2, con="construction", dyn="dynamizer", vers="versioning",
                  pcl="pointcloud")


def _module_ns(version: str, modules: dict) -> dict:
    return {p: f"http://www.opengis.net/citygml/{m}/{version}" for p, m in modules.items()}


@dataclass(frozen=True)
class CityGMLProfile:
    version: str                       # "2.0" | "3.0"
    gml_version: str                   # "3.1.1" | "3.2"
    ns: dict = field(default_factory=dict)
    # structure
    lod_prefix_is_core: bool = False   # 3.0: core:lodN…; 2.0: <module>:lodN…
    boundary_prefix: str | None = None  # 3.0: "core" (core:boundary); 2.0: module prefix
    boundary_local: str = "boundedBy"
    solid_exterior_local: str = "CompositeSurface"
    implicit_geometry_local: str = "relativeGMLGeometry"
    terrain_intersection_fmt: str = "lod{n}TerrainIntersection"
    member_local: str = "cityObjectMember"
    appearance_member_prefix: str = "app"
    xal_ns: str = ""
    max_lod: int = 4

    # -- naming helpers ---------------------------------------------------------------
    def q(self, prefix: str, local: str) -> str:
        """Clark name {uri}local for a profile prefix."""
        return f"{{{self.ns[prefix]}}}{local}"

    @property
    def gml_id(self) -> str:
        return f"{{{self.ns['gml']}}}id"

    def lod_prop(self, module_prefix: str, local: str) -> str:
        """Qualified name of an LoD geometry property, e.g. lod_prop('bldg', 'lod2Solid')
        → bldg:lod2Solid in 2.0 and core:lod2Solid in 3.0."""
        return self.q("core" if self.lod_prefix_is_core else module_prefix, local)

    def boundary_prop(self, module_prefix: str) -> str:
        return self.q(self.boundary_prefix or module_prefix, self.boundary_local)

    @property
    def appearance_member(self) -> str:
        return self.q(self.appearance_member_prefix, "appearanceMember")

    @property
    def city_object_member(self) -> str:
        return self.q("core", self.member_local)

    def known_namespaces(self) -> frozenset:
        return frozenset(self.ns.values())

    def schema_location(self, prefixes) -> str:
        """xsi:schemaLocation for the given module prefixes (+ core)."""
        pairs = []
        base = "http://schemas.opengis.net/citygml"
        files2 = {"core": "2.0/cityGMLBase.xsd", "app": "appearance/2.0/appearance.xsd",
                  "bldg": "building/2.0/building.xsd", "brid": "bridge/2.0/bridge.xsd",
                  "frn": "cityfurniture/2.0/cityFurniture.xsd",
                  "grp": "cityobjectgroup/2.0/cityObjectGroup.xsd",
                  "gen": "generics/2.0/generics.xsd", "luse": "landuse/2.0/landUse.xsd",
                  "dem": "relief/2.0/relief.xsd", "tran": "transportation/2.0/transportation.xsd",
                  "tun": "tunnel/2.0/tunnel.xsd", "veg": "vegetation/2.0/vegetation.xsd",
                  "wtr": "waterbody/2.0/waterBody.xsd"}
        files3 = {"core": "3.0/core.xsd", "app": "appearance/3.0/appearance.xsd",
                  "bldg": "building/3.0/building.xsd", "brid": "bridge/3.0/bridge.xsd",
                  "con": "construction/3.0/construction.xsd",
                  "frn": "cityfurniture/3.0/cityFurniture.xsd",
                  "grp": "cityobjectgroup/3.0/cityObjectGroup.xsd",
                  "gen": "generics/3.0/generics.xsd", "luse": "landuse/3.0/landUse.xsd",
                  "dem": "relief/3.0/relief.xsd", "tran": "transportation/3.0/transportation.xsd",
                  "tun": "tunnel/3.0/tunnel.xsd", "veg": "vegetation/3.0/vegetation.xsd",
                  "wtr": "waterbody/3.0/waterBody.xsd", "dyn": "dynamizer/3.0/dynamizer.xsd",
                  "pcl": "pointcloud/3.0/pointCloud.xsd"}
        files = files3 if self.version == "3.0" else files2
        for p in ["core"] + [p for p in prefixes if p != "core"]:
            if p in files and p in self.ns:
                pairs.append(f"{self.ns[p]} {base}/{files[p]}")
        return " ".join(pairs)


PROFILE_V2 = CityGMLProfile(
    version="2.0", gml_version="3.1.1",
    ns={"core": "http://www.opengis.net/citygml/2.0", "gml": "http://www.opengis.net/gml",
        "xlink": XLINK_NS, "xsi": XSI_NS,
        "xAL": "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0",
        **_module_ns("2.0", _MODULES_2)},
    lod_prefix_is_core=False, boundary_prefix=None, boundary_local="boundedBy",
    solid_exterior_local="CompositeSurface", implicit_geometry_local="relativeGMLGeometry",
    terrain_intersection_fmt="lod{n}TerrainIntersection", member_local="cityObjectMember",
    appearance_member_prefix="app", xal_ns="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0",
    max_lod=4,
)

PROFILE_V3 = CityGMLProfile(
    version="3.0", gml_version="3.2",
    ns={"core": "http://www.opengis.net/citygml/3.0", "gml": "http://www.opengis.net/gml/3.2",
        "xlink": XLINK_NS, "xsi": XSI_NS,
        "xAL": "urn:oasis:names:tc:ciq:xal:3", "ct": "urn:oasis:names:tc:ciq:ct:3",
        **_module_ns("3.0", _MODULES_3)},
    lod_prefix_is_core=True, boundary_prefix="core", boundary_local="boundary",
    solid_exterior_local="Shell", implicit_geometry_local="relativeGeometry",
    terrain_intersection_fmt="lod{n}TerrainIntersectionCurve", member_local="cityObjectMember",
    appearance_member_prefix="core", xal_ns="urn:oasis:names:tc:ciq:xal:3",
    max_lod=3,
)

PROFILES = {"2.0": PROFILE_V2, "3.0": PROFILE_V3}

_CORE_NS_RE = re.compile(r"^\{http://www\.opengis\.net/citygml/([\d.]+)\}CityModel$")


def detect_citygml_version(path) -> str | None:
    """Return the CityGML version of a file ('1.0', '2.0', '3.0') from the namespace of
    its root CityModel element (first start event only — cheap for huge files), or None
    when the root is not a CityModel."""
    from lxml import etree
    try:
        for _event, el in etree.iterparse(str(path), events=("start",), huge_tree=True):
            m = _CORE_NS_RE.match(el.tag if isinstance(el.tag, str) else "")
            return m.group(1) if m else None
    except etree.XMLSyntaxError:
        return None
    return None


def profile_for(version: str) -> CityGMLProfile:
    return PROFILES["3.0" if str(version).startswith("3") else "2.0"]


def href_fragment(href: str | None) -> str:
    """'#id' → 'id'; 'tile_b.gml#id' → 'id' (cross-document reference resolved by
    its fragment id, see GRAPH.md 'pending references'); 'id' → 'id'."""
    if not href:
        return ""
    return href.rsplit("#", 1)[-1] if "#" in href else href
