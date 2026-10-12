#!/usr/bin/env python3
"""
roundtrip_fidelity.py
Round-trip fidelity evaluation for any CityGML 2.0 file.

Verifies that every supported top-level CityGML 2.0 feature in the source
file is present in Neo4j after ingest and can be re-exported without loss of
identifiers, geometry connectivity, scalar attributes, or coordinate strings.
Works across all thematic modules (bldg/brid/tun/tran/frn/dem/wtr/veg/luse/grp)
and all LODs (0–4) — the dataset to check is selected with --gml.

Prerequisites (run once from project root, with the same file you pass to --gml):
    python ingest_citygml.py <path/to/file.gml>

Usage (from project root):
    python eval/roundtrip_fidelity.py --gml <path>   # check a specific file
    python eval/roundtrip_fidelity.py --gml <path> --skip-export   # graph checks only

The Neo4j Dataset/feature lookup key (Dataset.source) and the default export
path are both derived from the --gml filename, so any ingested file works.

Results written to:
    eval/results/eval_<gml-stem>_<YYYYMMDDTHHMMSS>.json
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows terminals that default to cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree
from neo4j import GraphDatabase

import export_citygml  # project export module

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "neo4jneo4j")

# Default GML used when --gml is omitted; any CityGML 2.0 file works.
DEFAULT_GML = Path("input/citygml/Railway_Scene_LoD3/Railway_Scene_LoD3.gml")
RESULTS_DIR = Path("eval/results")

# Runtime-resolved in main() from the --gml argument. SOURCE_NAME is the
# Dataset.source value in Neo4j (ingest stores it as the GML basename);
# GML_PATH is the source file the report records.
GML_PATH    = DEFAULT_GML
SOURCE_NAME = DEFAULT_GML.name           # Dataset.source value in Neo4j

# ---------------------------------------------------------------------------
# Namespace declarations
# ---------------------------------------------------------------------------

_NS = {
    "cg":    "http://www.opengis.net/citygml/2.0",
    "gml":   "http://www.opengis.net/gml",
    "bldg":  "http://www.opengis.net/citygml/building/2.0",
    "gen":   "http://www.opengis.net/citygml/generics/2.0",
    "tran":  "http://www.opengis.net/citygml/transportation/2.0",
    "veg":   "http://www.opengis.net/citygml/vegetation/2.0",
    "wtr":   "http://www.opengis.net/citygml/waterbody/2.0",
    "luse":  "http://www.opengis.net/citygml/landuse/2.0",
    "frn":   "http://www.opengis.net/citygml/cityfurniture/2.0",
    "dem":   "http://www.opengis.net/citygml/relief/2.0",
    "brid":  "http://www.opengis.net/citygml/bridge/2.0",
    "tun":   "http://www.opengis.net/citygml/tunnel/2.0",
    "grp":   "http://www.opengis.net/citygml/cityobjectgroup/2.0",
    "app":   "http://www.opengis.net/citygml/appearance/2.0",
}

_GML_ID    = "{http://www.opengis.net/gml}id"
_GML_NS    = "http://www.opengis.net/gml"
_CG_NS     = "http://www.opengis.net/citygml/2.0"

# Supported top-level feature tags → (feature_type, ns_prefix_key)
SUPPORTED_TAGS: dict[str, tuple[str, str]] = {
    f"{{{_NS['bldg']}}}Building":                ("Building",                "bldg"),
    f"{{{_NS['brid']}}}Bridge":                  ("Bridge",                  "brid"),
    f"{{{_NS['tun']}}}Tunnel":                   ("Tunnel",                  "tun"),
    f"{{{_NS['tran']}}}Railway":                 ("Railway",                 "tran"),
    f"{{{_NS['tran']}}}Road":                    ("Road",                    "tran"),
    f"{{{_NS['tran']}}}Track":                   ("Track",                   "tran"),
    f"{{{_NS['tran']}}}Square":                  ("Square",                  "tran"),
    f"{{{_NS['tran']}}}TransportationComplex":   ("TransportationComplex",   "tran"),
    f"{{{_NS['frn']}}}CityFurniture":            ("CityFurniture",           "frn"),
    f"{{{_NS['dem']}}}ReliefFeature":            ("ReliefFeature",           "dem"),
    f"{{{_NS['wtr']}}}WaterBody":                ("WaterBody",               "wtr"),
    f"{{{_NS['veg']}}}SolitaryVegetationObject": ("SolitaryVegetationObject","veg"),
    f"{{{_NS['veg']}}}PlantCover":               ("PlantCover",              "veg"),
    f"{{{_NS['luse']}}}LandUse":                 ("LandUse",                 "luse"),
    f"{{{_NS['grp']}}}CityObjectGroup":          ("CityObjectGroup",         "grp"),
}

# Top-level features present in this dataset but not in MODULE_REGISTRY
UNSUPPORTED_TAGS: dict[str, dict] = {
    f"{{{_NS['gen']}}}GenericCityObject": {
        "feature_type": "GenericCityObject",
        "reason": (
            "gen:GenericCityObject is not in ingest MODULE_REGISTRY — "
            "the Generics module (gen:) is currently out of scope"
        ),
    },
}

# Constructs never ingested (by design) — not counted as failures
OUT_OF_SCOPE = [
    "app:Appearance / ParameterizedTexture / X3DMaterial — texture/appearance data",
    "bldg:BuildingInstallation, bldg:Door, bldg:Window — nested Building sub-features",
    "brid:BridgeConstructionElement, brid:BridgeInstallation — nested Bridge sub-features",
    "tun:TunnelInstallation — nested Tunnel sub-feature",
    "bldg:lod{N}MultiSurface on Building element directly (not via boundedBy) — "
    "only boundary-surface polygons and solid are ingested for buildings",
    "grp:CityObjectGroup member xlink:href references — group membership not stored in graph",
    "dem:TINRelief export format — reconstructed as gml:Polygon chain in gml:MultiSurface, "
    "not as original gml:TriangulatedSurface/gml:Triangle",
]


# ---------------------------------------------------------------------------
# Check record helpers
# ---------------------------------------------------------------------------

def _pass(name: str, detail: str = "") -> dict:
    return {"name": name, "status": "PASS", "detail": detail}

def _fail(name: str, detail: str = "") -> dict:
    return {"name": name, "status": "FAIL", "detail": detail}

def _warn(name: str, detail: str = "") -> dict:
    return {"name": name, "status": "WARN", "detail": detail}

def _gap(name: str, detail: str = "") -> dict:
    return {"name": name, "status": "GAP", "detail": detail}


# ---------------------------------------------------------------------------
# Source GML parser
# ---------------------------------------------------------------------------

def _first_pos_list(el: etree._Element) -> str | None:
    """
    First ring coordinate string at any depth in el. Supports both encodings:
    a single gml:posList, or a sequence of gml:pos elements inside the first
    gml:LinearRing (IFC→CityGML converters such as FZK-Haus LoD2–4 use gml:pos).
    gml:pos values are concatenated the same way ingest stores them, so the
    verbatim comparison is meaningful for either encoding.
    """
    for pl in el.iter(f"{{{_GML_NS}}}posList"):
        if pl.text and pl.text.strip():
            return pl.text.strip()
    # No posList — fall back to the first LinearRing's gml:pos children.
    for ring in el.iter(f"{{{_GML_NS}}}LinearRing"):
        pos_els = ring.findall(f"{{{_GML_NS}}}pos")
        coords = " ".join(p.text.strip() for p in pos_els if p.text and p.text.strip())
        if coords:
            return coords
    return None

def _poly_count_direct(el: etree._Element) -> int:
    """Count gml:Polygon elements that are direct geometry (not in sub-feature tags)."""
    return len(list(el.iter(f"{{{_GML_NS}}}Polygon")))

def _geom_names(el: etree._Element, ns_key: str) -> list[str]:
    """Return list of lod*{Solid,MultiSurface,Geometry,ImplicitRepresentation} child elements found."""
    ns_uri = _NS.get(ns_key, "")
    found = []
    for lod in ("0", "1", "2", "3", "4"):
        for suffix in ("Solid", "MultiSurface", "Geometry", "ImplicitRepresentation"):
            if el.find(f".//{{{ns_uri}}}lod{lod}{suffix}") is not None:
                found.append(f"lod{lod}{suffix}")
    return found

def _bsurf_counts(b_el: etree._Element, ns_key: str) -> dict[str, int]:
    """Count boundary surface element types under {ns_key}:boundedBy."""
    ns_uri = _NS[ns_key]
    counts: dict[str, int] = {}
    for bb in b_el.findall(f"{{{ns_uri}}}boundedBy"):
        for child in bb:
            ln = etree.QName(child.tag).localname
            counts[ln] = counts.get(ln, 0) + 1
    return counts

def _first_implicit_polygon(el: etree._Element, ns_key: str) -> tuple[str | None, str | None]:
    """Return (poly_id, pos_list) for the first inline polygon in an implicit geometry prototype."""
    ns_uri = _NS[ns_key]
    CG_URI = "http://www.opengis.net/citygml/2.0"
    for lod in ("1", "2", "3", "4"):
        impl_prop = el.find(f"{{{ns_uri}}}lod{lod}ImplicitRepresentation")
        if impl_prop is None:
            continue
        impl_el = impl_prop.find(f"{{{CG_URI}}}ImplicitGeometry")
        if impl_el is None:
            continue
        rel_geom = impl_el.find(f"{{{CG_URI}}}relativeGMLGeometry")
        if rel_geom is None:
            continue
        for poly in rel_geom.iter(f"{{{_GML_NS}}}Polygon"):
            pid = poly.get(_GML_ID)
            pl  = _first_pos_list(poly)
            if pid and pl:
                return pid, pl
    return None, None


def _implicit_spot_data(el: etree._Element, ns_key: str) -> dict:
    """Return {'lod', 'matrix_str', 'ref_str'} from the first ImplicitGeometry found, or {}."""
    ns_uri = _NS[ns_key]
    CG_URI = "http://www.opengis.net/citygml/2.0"
    for lod in ("1", "2", "3", "4"):
        impl_prop = el.find(f"{{{ns_uri}}}lod{lod}ImplicitRepresentation")
        if impl_prop is None:
            continue
        impl_el = impl_prop.find(f"{{{CG_URI}}}ImplicitGeometry")
        if impl_el is None:
            continue
        mat_el  = impl_el.find(f"{{{CG_URI}}}transformationMatrix")
        mat_str = mat_el.text.strip() if (mat_el is not None and mat_el.text) else ""
        ref_el  = impl_el.find(
            f"{{{CG_URI}}}referencePoint/{{{_GML_NS}}}Point/{{{_GML_NS}}}pos"
        )
        ref_str = ref_el.text.strip() if (ref_el is not None and ref_el.text) else ""
        return {"lod": lod, "matrix_str": mat_str, "ref_str": ref_str}
    return {}


def _first_bsurf_polygon(b_el: etree._Element, ns_key: str) -> tuple[str | None, str | None]:
    """Return (polygon_gml_id, pos_list) for the first polygon inside any boundedBy surface."""
    ns_uri = _NS[ns_key]
    for bb in b_el.findall(f"{{{ns_uri}}}boundedBy"):
        for surf in bb:
            for poly in surf.iter(f"{{{_GML_NS}}}Polygon"):
                pid = poly.get(_GML_ID)
                pl  = _first_pos_list(poly)
                if pid and pl:
                    return pid, pl
    return None, None

def parse_gml(gml_path: Path) -> dict:
    """Parse a CityGML file and return a structured inventory."""
    print(f"Parsing: {gml_path} …")
    tree = etree.parse(str(gml_path))
    root = tree.getroot()

    env = root.find(f".//{{{_GML_NS}}}Envelope")
    srs = env.get("srsName", "") if env is not None else ""

    inv: dict = {
        "srs_name": srs,
        "features": {},     # feature_type -> {count, ids, geoms_per_id, poly_count_per_id,
                            #                  bsurf_counts_per_id, scalar_per_id,
                            #                  sample_pos_list, spot_poly_id, spot_pos_list}
        "unsupported": {},  # feature_type -> {count, ids, reason}
        "total_polygons":  sum(1 for _ in root.iter(f"{{{_GML_NS}}}Polygon")),
        "total_triangles": sum(1 for _ in root.iter(f"{{{_GML_NS}}}Triangle")),
    }

    for com in root.findall(f"{{{_CG_NS}}}cityObjectMember"):
        for feat in com:
            fid = feat.get(_GML_ID, "")
            if not fid:
                continue

            if feat.tag in SUPPORTED_TAGS:
                ft, ns_key = SUPPORTED_TAGS[feat.tag]
                bucket = inv["features"].setdefault(ft, {
                    "count": 0, "ids": [], "geoms_per_id": {}, "poly_count_per_id": {},
                    "bsurf_counts_per_id": {}, "scalar_per_id": {},
                    "sample_pos_list": None, "spot_poly_id": None, "spot_pos_list": None,
                    "sample_matrix": None, "sample_ref": None,
                })
                bucket["count"] += 1
                bucket["ids"].append(fid)
                bucket["geoms_per_id"][fid]    = _geom_names(feat, ns_key)
                bucket["poly_count_per_id"][fid] = _poly_count_direct(feat)
                # list of per-OCCURRENCE counts: a gml:id may legitimately appear on
                # more than one distinct feature (e.g. Helsinki duplicates 61 BIDs);
                # ingest splits those into separate nodes, so the check must compare
                # the whole group, not assume id uniqueness.
                bucket["bsurf_counts_per_id"].setdefault(fid, []).append(
                    _bsurf_counts(feat, ns_key))

                # Scalar attributes
                scalars: dict[str, str] = {}
                for attr_name, xpath in [
                    ("function",    f"{{{_NS[ns_key]}}}function"),
                    ("class",       f"{{{_NS[ns_key]}}}class"),
                    ("usage",       f"{{{_NS[ns_key]}}}usage"),
                    ("roofType",    f"{{{_NS['bldg']}}}roofType"),
                ]:
                    el = feat.find(xpath)
                    if el is not None and el.text:
                        scalars[attr_name] = el.text.strip()
                bucket["scalar_per_id"][fid] = scalars

                # Sample pos_list (first one found anywhere in element)
                if bucket["sample_pos_list"] is None:
                    bucket["sample_pos_list"] = _first_pos_list(feat)

                # Spot check: boundary surface polygon first, then implicit prototype
                if bucket["spot_poly_id"] is None:
                    pid, pl = _first_bsurf_polygon(feat, ns_key)
                    if not (pid and pl):
                        pid, pl = _first_implicit_polygon(feat, ns_key)
                    if pid and pl:
                        bucket["spot_poly_id"]  = pid
                        bucket["spot_pos_list"] = pl

                # Implicit geometry spot data (matrix + ref for graph/export checks)
                if bucket["sample_matrix"] is None:
                    impl = _implicit_spot_data(feat, ns_key)
                    if impl:
                        if impl.get("matrix_str"):
                            bucket["sample_matrix"] = impl["matrix_str"]
                        if impl.get("ref_str"):
                            bucket["sample_ref"] = impl["ref_str"]

            elif feat.tag in UNSUPPORTED_TAGS:
                info = UNSUPPORTED_TAGS[feat.tag]
                ft   = info["feature_type"]
                entry = inv["unsupported"].setdefault(ft, {
                    "count": 0, "ids": [], "reason": info["reason"]
                })
                entry["count"] += 1
                entry["ids"].append(fid)

    for ft, b in inv["features"].items():
        print(f"  {ft}: {b['count']}")
    return inv


# ---------------------------------------------------------------------------
# Graph checks (after ingest)
# ---------------------------------------------------------------------------

def check_graph(sess, source_inv: dict) -> list[dict]:
    checks: list[dict] = []

    # ── 1. Dataset node present ─────────────────────────────────────────────
    row = sess.run(
        "MATCH (d:Dataset {source: $src}) RETURN d LIMIT 1", src=SOURCE_NAME
    ).single()
    if row:
        checks.append(_pass("dataset_node_exists",
                            f"Dataset node source='{SOURCE_NAME}' found"))
    else:
        checks.append(_fail("dataset_node_exists",
                            f"No Dataset node with source='{SOURCE_NAME}' — "
                            "has ingest been run?"))
        return checks  # all downstream checks will fail anyway

    ds = dict(row["d"])
    checks.append(_pass("dataset_crs",
                        f"CRS: {ds.get('crs', 'n/a')}  LOD: {ds.get('lod', 'n/a')}"))

    # ── 2. Building nodes ────────────────────────────────────────────────────
    src_buildings = source_inv["features"].get("Building", {})
    if src_buildings:
        expected_ids = set(src_buildings["ids"])
        rows = sess.run(
            "MATCH (b:Building {source: $src}) RETURN b.id AS id", src=SOURCE_NAME
        ).data()
        found_ids = {r["id"] for r in rows}
        missing   = sorted(expected_ids - found_ids)
        extra     = sorted(found_ids - expected_ids)
        name = "building_id_completeness"
        if not missing:
            checks.append(_pass(name,
                f"{len(found_ids)}/{len(expected_ids)} Building nodes present"))
        else:
            checks.append(_fail(name, f"Missing: {missing}  Extra: {extra}"))

        # Boundary surface count per building. Grouped by gml:id: a duplicated
        # source id yields several occurrence records AND several graph nodes
        # (ingest splits distinct features sharing an id, keeping the original on
        # source_gml_id) — compare the two groups as sorted count multisets.
        for bid in dict.fromkeys(src_buildings["ids"]):        # unique, source order
            occurrences = src_buildings["bsurf_counts_per_id"].get(bid, [])
            expected_totals = sorted(sum(o.values()) for o in occurrences)
            rows2 = sess.run(
                "MATCH (b:Building) WHERE b.id = $id OR b.source_gml_id = $id "
                "OPTIONAL MATCH (b)-[:HAS_BOUNDARY]->(s:BoundarySurface) "
                "RETURN b.id AS nid, count(s) AS n", id=bid
            ).data()
            found_totals = sorted(r["n"] for r in rows2)
            if found_totals == expected_totals:
                checks.append(_pass(f"building_bsurf_count[{bid}]",
                                    f"{found_totals} boundary surfaces "
                                    f"({len(occurrences)} occurrence(s))"))
            else:
                checks.append(_fail(f"building_bsurf_count[{bid}]",
                                    f"expected {expected_totals} "
                                    f"({len(occurrences)} source occurrence(s)), "
                                    f"found {found_totals}"))

            # Polygon count via boundary surfaces (informational)
            row3 = sess.run(
                "MATCH (b:Building) WHERE b.id = $id OR b.source_gml_id = $id "
                "MATCH (b)-[:HAS_BOUNDARY]->(:BoundarySurface)"
                "-[:HAS_POLYGON]->(p:GeometryPolygon) RETURN count(p) AS n", id=bid
            ).single()
            graph_polys = row3["n"] if row3 else 0
            checks.append(_pass(f"building_poly_count[{bid}]",
                                f"{graph_polys} polygons via boundary surfaces in graph"))

        # Verbatim coordinate spot-check
        spot_poly_id  = src_buildings.get("spot_poly_id")
        spot_pos_src  = src_buildings.get("spot_pos_list")
        if spot_poly_id and spot_pos_src:
            row4 = sess.run(
                "MATCH (p:GeometryPolygon {id: $pid})-[:HAS_EXTERIOR_RING]->(r:GeometryRing) "
                "RETURN r.pos_list AS pl", pid=spot_poly_id
            ).single()
            if row4 and row4["pl"]:
                if row4["pl"].strip() == spot_pos_src:
                    checks.append(_pass("building_pos_list_verbatim",
                                        f"polygon {spot_poly_id}: pos_list preserved verbatim"))
                else:
                    checks.append(_fail("building_pos_list_verbatim",
                                        f"polygon {spot_poly_id}: pos_list mismatch\n"
                                        f"  src: {spot_pos_src[:80]}…\n"
                                        f"  db:  {row4['pl'][:80]}…"))
            else:
                checks.append(_fail("building_pos_list_verbatim",
                                    f"polygon {spot_poly_id} ring not found in graph"))

    # ── 3. CityObject nodes ──────────────────────────────────────────────────
    city_object_types = [ft for ft in source_inv["features"] if ft != "Building"]
    for ft in city_object_types:
        bucket   = source_inv["features"][ft]
        exp_ids  = set(bucket["ids"])
        exp_n    = bucket["count"]

        rows = sess.run(
            "MATCH (n:CityObject {source: $src, feature_type: $ft}) RETURN n.id AS id",
            src=SOURCE_NAME, ft=ft,
        ).data()
        found_ids = {r["id"] for r in rows}
        missing   = sorted(exp_ids - found_ids)

        if not missing:
            checks.append(_pass(f"co_ids[{ft}]",
                                f"{len(found_ids)}/{exp_n} {ft} nodes present"))
        else:
            checks.append(_fail(f"co_ids[{ft}]",
                                f"Missing {len(missing)}/{exp_n}: {missing[:5]}"
                                f"{'…' if len(missing) > 5 else ''}"))

        # Geometry links — only check features that have source geometry
        features_with_geom = [
            fid for fid, gs in bucket["geoms_per_id"].items() if gs
        ]
        if features_with_geom:
            rows2 = sess.run(
                "MATCH (n:CityObject {source: $src, feature_type: $ft}) "
                "WHERE (n)-[:HAS_LOD_SOLID]->() OR (n)-[:HAS_LOD_SURFACE]->() "
                "   OR (n)-[:HAS_BOUNDARY]->() "
                "RETURN n.id AS id",
                src=SOURCE_NAME, ft=ft,
            ).data()
            with_geom_graph = {r["id"] for r in rows2}
            missing_geom = [fid for fid in features_with_geom
                            if fid not in with_geom_graph]
            if not missing_geom:
                checks.append(_pass(f"co_geometry[{ft}]",
                                    f"all {len(features_with_geom)} {ft} features "
                                    f"with source geometry have geometry links in graph"))
            else:
                checks.append(_fail(f"co_geometry[{ft}]",
                                    f"{len(missing_geom)} {ft} features have source geometry "
                                    f"but no geometry links in graph: {missing_geom[:3]}"))
        else:
            # Known case: SolitaryVegetationObject with no explicit geometry
            checks.append(_warn(f"co_geometry[{ft}]",
                                f"no explicit lod*Geometry/MultiSurface/Solid found in source "
                                f"for any {ft} — geometry check skipped"))

    # ── 4. Polygon ring verbatim check for CityObjects ───────────────────────
    for ft in city_object_types:
        bucket = source_inv["features"][ft]
        spot_pid = bucket.get("spot_poly_id")
        spot_src = bucket.get("spot_pos_list")
        if not (spot_pid and spot_src):
            continue
        row5 = sess.run(
            "MATCH (p:GeometryPolygon {id: $pid})-[:HAS_EXTERIOR_RING]->(r:GeometryRing) "
            "RETURN r.pos_list AS pl", pid=spot_pid
        ).single()
        if row5 and row5["pl"]:
            if row5["pl"].strip() == spot_src:
                checks.append(_pass(f"co_pos_list_verbatim[{ft}]",
                                    f"polygon {spot_pid}: pos_list preserved verbatim"))
            else:
                checks.append(_fail(f"co_pos_list_verbatim[{ft}]",
                                    f"polygon {spot_pid}: pos_list mismatch"))
        # No pos_list in graph is expected if the feature has no boundary-surface polygons

    # ── 5. Implicit geometry checks (SolitaryVegetationObject) ──────────────
    svo_bucket = source_inv["features"].get("SolitaryVegetationObject", {})
    impl_fids  = [
        fid for fid, gs in svo_bucket.get("geoms_per_id", {}).items()
        if any("ImplicitRepresentation" in g for g in gs)
    ]
    if impl_fids:
        rows_impl = sess.run(
            "UNWIND $ids AS fid "
            "MATCH (n:CityObject {id: fid})-[:HAS_LOD_SURFACE]->"
            "(ms:GeometryMultiSurface {is_implicit: true}) "
            "RETURN n.id AS id, ms.transformation_matrix AS matrix, "
            "ms.reference_point AS ref",
            ids=impl_fids,
        ).data()
        found_impl_ids = {r["id"] for r in rows_impl}
        missing_impl   = [fid for fid in impl_fids if fid not in found_impl_ids]
        if not missing_impl:
            checks.append(_pass(
                "implicit_geometry_ingested[SolitaryVegetationObject]",
                f"all {len(impl_fids)} trees with lod*ImplicitRepresentation "
                f"have GeometryMultiSurface{{is_implicit:true}} in graph",
            ))
        else:
            checks.append(_fail(
                "implicit_geometry_ingested[SolitaryVegetationObject]",
                f"{len(missing_impl)}/{len(impl_fids)} trees missing implicit MS: "
                f"{missing_impl[:3]}{'…' if len(missing_impl) > 3 else ''}",
            ))

        sample_row = next(
            (r for r in rows_impl if r.get("matrix") and r.get("ref")), None
        )
        if sample_row:
            checks.append(_pass(
                "implicit_matrix_stored[SolitaryVegetationObject]",
                f"transformation_matrix and reference_point stored on implicit MS; "
                f"ref={sample_row['ref']}",
            ))
        else:
            checks.append(_fail(
                "implicit_matrix_stored[SolitaryVegetationObject]",
                "no implicit GeometryMultiSurface has both transformation_matrix "
                "and reference_point set",
            ))

        impl_spot_pid = svo_bucket.get("spot_poly_id")
        impl_spot_src = svo_bucket.get("spot_pos_list")
        if impl_spot_pid and impl_spot_src:
            row_impl_pl = sess.run(
                "MATCH (p:GeometryPolygon {id: $pid})-[:HAS_EXTERIOR_RING]->"
                "(r:GeometryRing) RETURN r.pos_list AS pl",
                pid=impl_spot_pid,
            ).single()
            if row_impl_pl and row_impl_pl["pl"]:
                if row_impl_pl["pl"].strip() == impl_spot_src:
                    checks.append(_pass(
                        "implicit_prototype_pos_list_verbatim[SolitaryVegetationObject]",
                        f"polygon {impl_spot_pid}: local-space prototype pos_list "
                        f"preserved verbatim",
                    ))
                else:
                    checks.append(_fail(
                        "implicit_prototype_pos_list_verbatim[SolitaryVegetationObject]",
                        f"polygon {impl_spot_pid}: pos_list mismatch\n"
                        f"  src: {impl_spot_src[:80]}…\n"
                        f"  db:  {row_impl_pl['pl'][:80]}…",
                    ))
            else:
                checks.append(_fail(
                    "implicit_prototype_pos_list_verbatim[SolitaryVegetationObject]",
                    f"polygon {impl_spot_pid} ring not found in graph",
                ))

        # Check that xlink:href prototype references were resolved to polygon members
        rows_href = sess.run(
            "UNWIND $ids AS fid "
            "MATCH (n:CityObject {id: fid})-[:HAS_LOD_SURFACE]->"
            "(ms:GeometryMultiSurface {is_implicit: true}) "
            "WHERE ms.prototype_href IS NOT NULL "
            "OPTIONAL MATCH (ms)-[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon) "
            "RETURN n.id AS id, ms.id AS ms_id, count(p) AS poly_count",
            ids=impl_fids,
        ).data()
        href_rows = [r for r in rows_href if r.get("poly_count", 0) > 0]
        href_total = [r for r in rows_href]
        if href_total:
            if len(href_rows) == len(href_total):
                checks.append(_pass(
                    "implicit_xhref_prototype_resolved[SolitaryVegetationObject]",
                    f"all {len(href_total)} xlink:href implicit MS nodes have "
                    f"HAS_SURFACE_MEMBER polygon members after prototype resolution",
                ))
            else:
                unresolved = [r["id"] for r in href_total if r.get("poly_count", 0) == 0]
                checks.append(_fail(
                    "implicit_xhref_prototype_resolved[SolitaryVegetationObject]",
                    f"{len(href_total) - len(href_rows)}/{len(href_total)} xlink:href "
                    f"implicit MS have no polygon members after prototype resolution: "
                    f"{unresolved[:3]}{'…' if len(unresolved) > 3 else ''}",
                ))

    # ── 6. Spatial R-tree layer and CityObject bbox_wkt ─────────────────────
    try:
        layers = {
            r["name"] for r in sess.run("CALL spatial.layers() YIELD name").data()
        }
        if "features" in layers:
            checks.append(_pass("spatial_layer_exists",
                                "spatial WKT layer 'features' exists"))
        else:
            checks.append(_fail("spatial_layer_exists",
                                f"layer 'features' not found; present: {sorted(layers)}"))
    except Exception as exc:
        checks.append(_warn("spatial_layer_exists",
                            f"spatial.layers() unavailable ({exc})"))

    total_cos = sum(
        b["count"] for ft, b in source_inv["features"].items() if ft != "Building"
    )
    # Buildings carry their own bbox/location checks above; these two only apply
    # to non-Building CityObjects. A building-only file has none, so there is
    # nothing to store — that is a PASS (no loss), not a failure.
    if total_cos == 0:
        checks.append(_pass(
            "cityobject_bbox_wkt_stored",
            "no non-Building CityObjects in source — nothing to store",
        ))
        checks.append(_pass(
            "cityobject_location_stored",
            "no non-Building CityObjects in source — nothing to store",
        ))
    else:
        row_bwkt = sess.run(
            "MATCH (n:CityObject {source: $src}) WHERE n.bbox_wkt IS NOT NULL "
            "RETURN count(n) AS n",
            src=SOURCE_NAME,
        ).single()
        co_with_bwkt = row_bwkt["n"] if row_bwkt else 0
        if co_with_bwkt > 0:
            checks.append(_pass(
                "cityobject_bbox_wkt_stored",
                f"{co_with_bwkt}/{total_cos} CityObject nodes have bbox_wkt stored",
            ))
        else:
            checks.append(_fail(
                "cityobject_bbox_wkt_stored",
                f"no CityObject nodes have bbox_wkt (source has {total_cos} city objects)",
            ))

        row_loc = sess.run(
            "MATCH (n:CityObject {source: $src}) WHERE n.location IS NOT NULL "
            "RETURN count(n) AS n",
            src=SOURCE_NAME,
        ).single()
        co_with_loc = row_loc["n"] if row_loc else 0
        if co_with_loc > 0:
            checks.append(_pass(
                "cityobject_location_stored",
                f"{co_with_loc}/{total_cos} CityObject nodes have location point stored",
            ))
        else:
            checks.append(_fail(
                "cityobject_location_stored",
                f"no CityObject nodes have location point (source has {total_cos} city objects)",
            ))

    # ── 7. Unsupported features ──────────────────────────────────────────────
    for ft, info in source_inv["unsupported"].items():
        checks.append(_gap(f"unsupported_feature_type[{ft}]",
                           f"{info['count']} {ft} features in source — {info['reason']}"))

    return checks


# ---------------------------------------------------------------------------
# Export and comparison
# ---------------------------------------------------------------------------

def run_export(export_path: Path) -> bool:
    """Invoke export_citygml.export(); return True on success."""
    print(f"Running export → {export_path} …")
    try:
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_citygml.export(export_path)
        return True
    except Exception as exc:
        print(f"  ERROR during export: {exc}")
        return False


def check_export(source_inv: dict, export_path: Path) -> list[dict]:
    checks: list[dict] = []

    if not export_path.exists():
        checks.append(_fail("export_file_exists", f"{export_path} not created"))
        return checks
    checks.append(_pass("export_file_exists",
                        f"{export_path.stat().st_size // 1024:,} KB"))

    # Parse export GML
    try:
        export_inv = parse_gml(export_path)
    except Exception as exc:
        checks.append(_fail("export_parses_as_xml", str(exc)))
        return checks
    checks.append(_pass("export_parses_as_xml", "valid XML"))

    # Feature type counts
    for ft, src_bucket in source_inv["features"].items():
        exp_n   = src_bucket["count"]
        exp_inv = export_inv["features"].get(ft, {})
        got_n   = exp_inv.get("count", 0)
        if got_n == exp_n:
            checks.append(_pass(f"export_count[{ft}]", f"{got_n}/{exp_n}"))
        else:
            checks.append(_fail(f"export_count[{ft}]",
                                f"expected {exp_n}, got {got_n}"))

    # Feature ID completeness
    for ft, src_bucket in source_inv["features"].items():
        src_ids = set(src_bucket["ids"])
        exp_ids = set(export_inv["features"].get(ft, {}).get("ids", []))
        missing = sorted(src_ids - exp_ids)
        extra   = sorted(exp_ids - src_ids)
        if not missing:
            checks.append(_pass(f"export_ids[{ft}]",
                                f"all {len(src_ids)} IDs present"))
        else:
            checks.append(_fail(f"export_ids[{ft}]",
                                f"missing {len(missing)} IDs: {missing[:5]}"
                                f"{'…' if len(missing) > 5 else ''}"
                                + (f" | extra: {extra[:3]}" if extra else "")))

    # Scalar attribute round-trip (function / roofType)
    for ft, src_bucket in source_inv["features"].items():
        for fid, src_scalars in src_bucket["scalar_per_id"].items():
            if not src_scalars:
                continue
            exp_scalars = (export_inv["features"]
                           .get(ft, {})
                           .get("scalar_per_id", {})
                           .get(fid, {}))
            mismatches = {k: (v, exp_scalars.get(k))
                         for k, v in src_scalars.items()
                         if exp_scalars.get(k) != v}
            if not mismatches:
                checks.append(_pass(f"export_scalars[{ft}/{fid[:20]}]",
                                    f"{src_scalars}"))
            else:
                checks.append(_fail(f"export_scalars[{ft}/{fid[:20]}]",
                                    f"mismatch: {mismatches}"))

    # Coordinate verbatim spot-check
    for ft, src_bucket in source_inv["features"].items():
        spot_pid = src_bucket.get("spot_poly_id")
        spot_src = src_bucket.get("spot_pos_list")
        if not (spot_pid and spot_src):
            continue

        # Find the same polygon in the exported GML by gml:id
        exp_pos = _find_pos_by_poly_id(export_path, spot_pid)
        if exp_pos is None:
            checks.append(_fail(f"export_pos_list_verbatim[{ft}]",
                                f"polygon {spot_pid} not found in export"))
        elif exp_pos == spot_src:
            checks.append(_pass(f"export_pos_list_verbatim[{ft}]",
                                f"polygon {spot_pid}: pos_list verbatim preserved"))
        else:
            checks.append(_fail(f"export_pos_list_verbatim[{ft}]",
                                f"polygon {spot_pid}: pos_list mismatch\n"
                                f"  src: {spot_src[:120]}…\n"
                                f"  exp: {exp_pos[:120]}…"))

    # Implicit geometry round-trip (SolitaryVegetationObject)
    checks.extend(_check_export_implicit(source_inv, export_path))

    # Unsupported feature types absent from export (correct behaviour)
    for ft in source_inv["unsupported"]:
        if ft not in export_inv["features"]:
            checks.append(_pass(f"export_unsupported_absent[{ft}]",
                                f"{ft} correctly absent from export (unsupported)"))
        else:
            checks.append(_warn(f"export_unsupported_absent[{ft}]",
                                f"{ft} unexpectedly present in export"))

    return checks


def _check_export_implicit(source_inv: dict, export_path: Path) -> list[dict]:
    """Verify implicit geometry round-trip in exported GML for SolitaryVegetationObject."""
    checks: list[dict] = []
    svo_src = source_inv["features"].get("SolitaryVegetationObject", {})
    impl_fids = [
        fid for fid, gs in svo_src.get("geoms_per_id", {}).items()
        if any("ImplicitRepresentation" in g for g in gs)
    ]
    if not impl_fids:
        return checks

    # Use tag-filtered iterparse on ImplicitGeometry so children are still present
    # when the end event fires.  Clearing with el.clear() on every element in an
    # unfiltered loop removes child nodes before their parent fires — use the tag=
    # filter to avoid that.
    CG_CORE = "http://www.opengis.net/citygml/2.0"
    impl_core_tag = f"{{{CG_CORE}}}ImplicitGeometry"
    mat_tag  = f"{{{CG_CORE}}}transformationMatrix"
    ref_tag  = f"{{{CG_CORE}}}referencePoint"
    pt_tag   = f"{{{_GML_NS}}}Point"
    pos_tag  = f"{{{_GML_NS}}}pos"

    export_impl_count = 0
    export_matrices: list[str] = []
    export_refs: list[str] = []

    for _, el in etree.iterparse(str(export_path), events=("end",), tag=impl_core_tag):
        export_impl_count += 1
        mat_el = el.find(mat_tag)
        if mat_el is not None and mat_el.text:
            export_matrices.append(mat_el.text.strip())
        ref_el = el.find(f"{ref_tag}/{pt_tag}/{pos_tag}")
        if ref_el is not None and ref_el.text:
            export_refs.append(ref_el.text.strip())
        el.clear()

    src_count = len(impl_fids)
    if export_impl_count >= src_count:
        checks.append(_pass(
            "export_implicit_count[SolitaryVegetationObject]",
            f"{export_impl_count} lod*ImplicitRepresentation elements in export "
            f"(source: {src_count} trees with implicit geometry)",
        ))
    else:
        checks.append(_fail(
            "export_implicit_count[SolitaryVegetationObject]",
            f"expected >= {src_count} lod*ImplicitRepresentation, found {export_impl_count}",
        ))

    sample_matrix = svo_src.get("sample_matrix")
    sample_ref    = svo_src.get("sample_ref")

    if sample_matrix and export_matrices:
        if sample_matrix in export_matrices:
            checks.append(_pass(
                "export_implicit_matrix_verbatim[SolitaryVegetationObject]",
                "transformationMatrix preserved verbatim in export",
            ))
        else:
            checks.append(_fail(
                "export_implicit_matrix_verbatim[SolitaryVegetationObject]",
                f"source matrix not found in export matrices\n"
                f"  src: {sample_matrix[:80]}…",
            ))

    if sample_ref and export_refs:
        if sample_ref in export_refs:
            checks.append(_pass(
                "export_implicit_ref_verbatim[SolitaryVegetationObject]",
                f"referencePoint pos preserved verbatim: {sample_ref}",
            ))
        else:
            checks.append(_fail(
                "export_implicit_ref_verbatim[SolitaryVegetationObject]",
                f"source referencePoint not found in export refs\n"
                f"  src: {sample_ref}",
            ))

    return checks


def _find_pos_by_poly_id(gml_path: Path, poly_id: str) -> str | None:
    """Scan a GML file for a gml:Polygon with the given id; return its first posList."""
    poly_tag = f"{{{_GML_NS}}}Polygon"
    pos_tag  = f"{{{_GML_NS}}}posList"
    for _, el in etree.iterparse(str(gml_path), events=("end",), tag=poly_tag):
        if el.get(_GML_ID) == poly_id:
            for pl in el.iter(pos_tag):
                if pl.text and pl.text.strip():
                    return pl.text.strip()
        el.clear()
    return None


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_results(
    source_inv: dict,
    checks: list[dict],
    export_path: Path | None,
    out_dir: Path,
) -> Path:
    ts   = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    name = f"eval_{GML_PATH.stem}_{ts}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    out  = out_dir / name

    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "GAP": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    # Strip very large poly_count_per_id dicts from the serialised inventory
    # to keep the JSON readable — keep only feature counts and IDs.
    compact_inv: dict = {}
    for ft, b in source_inv.get("features", {}).items():
        compact_inv[ft] = {
            "count":   b["count"],
            "ids":     b["ids"],
            "geoms_per_id": b["geoms_per_id"],
            "poly_count_per_id": b["poly_count_per_id"],
            "bsurf_counts_per_id": b["bsurf_counts_per_id"],
            "scalar_per_id": b["scalar_per_id"],
            "spot_poly_id": b.get("spot_poly_id"),
            "sample_matrix": b.get("sample_matrix"),
            "sample_ref": b.get("sample_ref"),
        }

    report = {
        "timestamp":    ts,
        "source_file":  str(GML_PATH),
        "export_file":  str(export_path) if export_path else None,
        "summary":      {**counts, "total": len(checks)},
        "source_inventory": {
            "srs_name":        source_inv["srs_name"],
            "total_polygons":  source_inv["total_polygons"],
            "total_triangles": source_inv["total_triangles"],
            "features":        compact_inv,
            "unsupported":     source_inv.get("unsupported", {}),
        },
        "checks":       checks,
        "out_of_scope": OUT_OF_SCOPE,
    }

    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def print_summary(checks: list[dict], result_path: Path):
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "GAP": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    sep = "-" * 70
    print(f"\n{sep}")
    print(f"  RESULTS:  PASS={counts['PASS']}  FAIL={counts['FAIL']}"
          f"  WARN={counts['WARN']}  GAP={counts['GAP']}")
    print(sep)

    for c in checks:
        icon = {"PASS": "OK", "FAIL": "!!", "WARN": "??", "GAP": "--"}.get(c["status"], "??")
        print(f"  {icon} [{c['status']:<4}] {c['name']}")
        if c["status"] in ("FAIL", "WARN", "GAP") and c["detail"]:
            for line in c["detail"].splitlines():
                print(f"           {line}")

    print(sep)
    print(f"  Results: {result_path}")
    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gml",        default=str(DEFAULT_GML),
                        help="Path to source GML file (any ingested CityGML 2.0 file)")
    parser.add_argument("--export-out", default=None,
                        help="Path for exported GML "
                             "(default: output/citygml/<gml-stem>_export.gml)")
    parser.add_argument("--skip-export", action="store_true",
                        help="Skip export and comparison phase")
    args = parser.parse_args()

    gml_path    = Path(args.gml)
    export_path = (Path(args.export_out) if args.export_out
                   else Path("output/citygml") / f"{gml_path.stem}_export.gml")

    if not gml_path.exists():
        print(f"ERROR: GML file not found: {gml_path}", file=sys.stderr)
        sys.exit(1)

    # Resolve the railway-agnostic globals from the chosen file. ingest stores
    # Dataset.source / <feature>.source as the GML basename (gml_path.name), so
    # the graph lookups and the recorded source path follow the --gml argument.
    global GML_PATH, SOURCE_NAME
    GML_PATH    = gml_path
    SOURCE_NAME = gml_path.name

    # ── Phase 1: parse source ───────────────────────────────────────────────
    print("\n=== Phase 1: Source GML inventory ===")
    source_inv = parse_gml(gml_path)

    # ── Phase 2: graph checks ───────────────────────────────────────────────
    print("\n=== Phase 2: Graph checks (post-ingest) ===")
    try:
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=NEO4J_AUTH,
            notifications_disabled_classifications={"DEPRECATION", "UNRECOGNIZED"},
        )
        driver.verify_connectivity()
    except Exception as exc:
        print(f"ERROR: Cannot connect to Neo4j at {NEO4J_URI}: {exc}", file=sys.stderr)
        sys.exit(1)

    all_checks: list[dict] = []
    with driver.session(database="neo4j") as sess:
        all_checks.extend(check_graph(sess, source_inv))

    driver.close()

    # ── Phase 3: export + comparison ────────────────────────────────────────
    if not args.skip_export:
        print("\n=== Phase 3: Export round-trip ===")
        ok = run_export(export_path)
        if ok:
            all_checks.extend(check_export(source_inv, export_path))
        else:
            all_checks.append(_fail("export_succeeded", "export raised an exception"))

    # ── Write results ────────────────────────────────────────────────────────
    result_path = write_results(
        source_inv, all_checks,
        export_path if not args.skip_export else None,
        RESULTS_DIR,
    )
    print_summary(all_checks, result_path)


if __name__ == "__main__":
    main()
