"""
The graph as a picture: what the graph visualisers share.

build_graph_pointcloud (3D Tiles point cloud of the nodes) and visualize_graph_3d (one
CesiumJS page) draw the same thing in two formats: every node of the knowledge graph at a
position in the city, coloured by what it is. This module holds what does not depend on the
format: which nodes are drawn and where (fetch_all_nodes), the edges between them, the
group a node is coloured by, its caption, and the colour and size tables.

A raw node is {eid, labels, props, cx, cy, cz, jitter, alt_extra}: cx/cy/cz in the CRS of
the dataset, `jitter` for nodes that would otherwise sit on top of each other, `alt_extra`
the height in metres by which its kind is lifted (lookup nodes float above the roofs).
"""

import hashlib
import math
import time

from pykci.geometry import centroid_of_pos_lists

# properties too large to ship with a node
SKIP_PROPS = frozenset({"pos_list", "embedding", "embedding_ollama", "bbox_wkt", "location"})

LABEL_COLORS = {
    # semantic / thematic nodes
    "Building":                 "#FF7043",   # deep orange
    "Dataset":                  "#FFD54F",   # amber: the top-level container
    "District":                 "#AED581",   # light green
    "City":                     "#81D4FA",   # sky blue
    "BuildingFunction":         "#CE93D8",   # soft purple
    "RoofType":                 "#F48FB1",   # pink
    # BoundarySurface types
    "RoofSurface":              "#FF8A65",   # terracotta
    "WallSurface":              "#CFD8DC",   # light blue-grey
    "GroundSurface":            "#BCAAA4",   # warm taupe
    "ClosureSurface":           "#B0BEC5",
    "OuterCeilingSurface":      "#90A4AE",
    "OuterFloorSurface":        "#A1887F",
    # geometry leaf nodes (small, technical)
    "GeometrySolid":            "#80CBC4",   # teal
    "GeometryPolygon":          "#4DB6AC",   # slightly darker teal
    "GeometryRing":             "#26A69A",   # deep teal (smallest, deepest)
    "GeometryLineString":       "#80DEEA",   # cyan
    "TerrainIntersection":      "#A5D6A7",   # light green
    # CityObject feature types
    "Road":                     "#78909C",
    "Railway":                  "#CE93D8",
    "Track":                    "#AB47BC",
    "Square":                   "#FFCC80",
    "TransportationComplex":    "#90A4AE",
    "SolitaryVegetationObject": "#81C784",
    "PlantCover":               "#66BB6A",
    "WaterBody":                "#4FC3F7",
    "LandUse":                  "#DCE775",
    "CityFurniture":            "#CE93D8",
    "ReliefFeature":            "#BCAAA4",
    "Bridge":                   "#A1887F",
    "Tunnel":                   "#78909C",
    "CityObjectGroup":          "#80CBC4",
}
DEFAULT_COLOR = "#90A4AE"

# pixel size per node group (Building is largest; geometry leaves are smallest)
PIXEL_SIZE = {
    "Building": 14, "Dataset": 13, "City": 13, "District": 12,
    "BuildingFunction": 9, "RoofType": 9,
    "RoofSurface": 7, "WallSurface": 7, "GroundSurface": 7,
    "ClosureSurface": 6, "OuterCeilingSurface": 6, "OuterFloorSurface": 6,
    "GeometrySolid": 8, "TerrainIntersection": 6,
    "GeometryPolygon": 5, "GeometryLineString": 5,
    "GeometryRing": 4,
}

# labels that name a node's group, most telling first
GROUP_LABELS = ("Dataset", "City", "District", "Building",
                "BuildingFunction", "RoofType",
                "GeometrySolid", "GeometryMultiSurface",
                "GeometryPolygon", "GeometryRing",
                "GeometryLineString", "TerrainIntersection")


def node_group(labels: list, props: dict, *, order=GROUP_LABELS, generic=("CityObject",)) -> str:
    """The group a node is coloured and sized by: the first label of `order` it carries, a
    boundary surface's surface_type, a city object's feature_type, else its first label
    that is not one of the `generic` ones."""
    for lbl in order:
        if lbl in labels:
            return lbl
    if "BoundarySurface" in labels:
        return props.get("surface_type", "BoundarySurface")
    ftype = props.get("feature_type", "")
    return ftype or next((l for l in labels if l not in generic), "CityObject")


def node_label(labels: list, props: dict) -> str:
    """The short caption of a node."""
    pid = props.get("id", "")
    if "Dataset" in labels:
        return props.get("name", pid)[:24]
    if "City" in labels:
        return props.get("name", pid)[:20]
    if "District" in labels:
        return props.get("name", pid)[:20]
    if "BuildingFunction" in labels:
        return props.get("name", props.get("code", pid))[:20]
    if "RoofType" in labels:
        return props.get("name", props.get("code", pid))[:20]
    if "Building" in labels:
        return pid[:20]
    if "BoundarySurface" in labels:
        return props.get("surface_type", pid)[:16]
    return pid[:12]


def xy_jitter(eid_str: str, radius_m: float = 2.0) -> tuple:
    """A small offset (dx, dy) within `radius_m`, the same for the same element id on
    every run."""
    digest = hashlib.md5(eid_str.encode()).hexdigest()
    angle  = int(digest[:8],  16) / 0xFFFFFFFF * 2 * math.pi
    r      = int(digest[8:16], 16) / 0xFFFFFFFF * radius_m
    return r * math.cos(angle), r * math.sin(angle)


def centroid_of_pos_str(pos_str: str):
    return centroid_of_pos_lists([pos_str]) if pos_str else (None, None, None)


# ── Neo4j queries ─────────────────────────────────────────────────────────────

def fetch_dataset(driver):
    """(crs, x, y, z): the CRS of the first Dataset and its lower corner as sample point."""
    with driver.session() as s:
        row = s.run("""
            MATCH (d:Dataset)
            RETURN d.crs AS crs, d.bbox_min_x AS x, d.bbox_min_y AS y, d.bbox_min_z AS z
            LIMIT 1
        """).single()
    crs = (row["crs"] if row else None) or "EPSG:25832"
    sx  = float(row["x"] or 0) if row else 0.0
    sy  = float(row["y"] or 0) if row else 0.0
    sz  = float(row["z"] or 0) if row else 0.0
    print(f"  CRS: {crs}", flush=True)
    return crs, sx, sy, sz


# nodes placed at the centroid of the exterior rings below them:
# (caption, variable, MATCH pattern down to the ring `rng`, jitter, alt_extra)
_RING_CENTROID_NODES = (
    ("BoundarySurface",
     "bs", "(bs:BoundarySurface)-[:HAS_POLYGON]->(:GeometryPolygon)", True, 0.0),
    ("GeometrySolid",
     "s", "(s:GeometrySolid)-[:HAS_SURFACE_MEMBER]->(:GeometryPolygon)", True, 6.0),
    ("GeometryMultiSurface",
     "ms", "(ms:GeometryMultiSurface)-[:HAS_SURFACE_MEMBER]->(:GeometryPolygon)", True, 4.0),
)

# nodes placed above the buildings that point at them:
# (caption, MATCH of the node `n` and its buildings `b`, lookup value shared by many
#  buildings, alt_extra)
_AGGREGATE_NODES = (
    ("BuildingFunction", "MATCH (b:Building)-[:HAS_FUNCTION]->(n:BuildingFunction)", True, 8.0),
    ("RoofType", "MATCH (b:Building)-[:HAS_ROOF_TYPE]->(n:RoofType)", True, 5.0),
    ("District", "MATCH (n:District)<-[:LOCATED_IN]-(b:Building)", False, 80.0),
    ("City", "MATCH (n:City)<-[:PART_OF]-(:District)<-[:LOCATED_IN]-(b:Building)", False, 150.0),
    ("Dataset", "MATCH (n:Dataset)-[:COVERS]->(:District)<-[:LOCATED_IN]-(b:Building)", False, 220.0),
)


def fetch_all_nodes(driver, *, exclude_osm_geometry: bool = False,
                    include_osm_enrichment: bool = False, jitter_lookups: bool = False) -> list:
    """Every drawn node as a raw node, with cx/cy/cz in the dataset CRS.

    `exclude_osm_geometry` leaves out the geometry chain of OpenStreetMap features (road
    line strings, footprint polygons and rings), `include_osm_enrichment` adds the OSM
    buildings and POIs matched to a building, floating above it. `jitter_lookups` spreads
    BuildingFunction and RoofType nodes like the geometry nodes."""
    print("Fetching graph nodes…", flush=True)
    t0_total = time.perf_counter()
    nodes = []

    def no_osm(var: str) -> str:
        return f" AND NOT coalesce({var}.source,'') STARTS WITH 'osm'" if exclude_osm_geometry else ""

    def add(r, cx, cy, cz, jitter, alt_extra):
        nodes.append({"eid": r["eid"], "labels": r["labels"], "props": r["props"],
                      "cx": cx, "cy": cy, "cz": cz, "jitter": jitter, "alt_extra": alt_extra})

    def report(count, caption, t0):
        print(f"  {count:>6} {caption:<20} ({time.perf_counter()-t0:.2f}s)", flush=True)

    with driver.session() as s:

        # Building + CityObject: they carry center_x / center_y
        t0 = time.perf_counter()
        rows = s.run("""
            MATCH (n) WHERE (n:Building OR n:CityObject)
              AND n.center_x IS NOT NULL AND n.center_y IS NOT NULL
            RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props,
                   toFloat(n.center_x) AS cx, toFloat(n.center_y) AS cy,
                   toFloat(coalesce(n.ground_z, n.bbox_min_z, 0.0)) AS gz,
                   toFloat(coalesce(n.measured_height, 0.0)) AS mh,
                   toFloat(coalesce(n.bbox_max_z, n.ground_z, 0.0)) AS bz2
        """).data()
        for r in rows:
            gz = r["gz"] or 0.0
            mh = r["mh"] or 0.0
            bz2 = r["bz2"] or 0.0
            add(r, r["cx"], r["cy"], gz + (mh / 2.0 if mh else (bz2 - gz) / 2.0), False, 0.0)
        report(len(rows), "Building/CityObject", t0)

        def centroid_nodes(caption, query, jitter, alt_extra, of_rows):
            t0 = time.perf_counter()
            ok = 0
            for r in s.run(query).data():
                cx, cy, cz = of_rows(r)
                if cx is None:
                    continue
                add(r, cx, cy, float(cz or 0), jitter, alt_extra)
                ok += 1
            report(ok, caption, t0)

        for caption, var, pattern, jitter, alt_extra in _RING_CENTROID_NODES:
            centroid_nodes(caption, f"""
                MATCH {pattern}
                      -[:HAS_EXTERIOR_RING]->(rng:GeometryRing)
                WITH {var}, collect(rng.pos_list) AS pls
                RETURN elementId({var}) AS eid, labels({var}) AS labels, properties({var}) AS props, pls
            """, jitter, alt_extra, lambda r: centroid_of_pos_lists(r["pls"]))

        # GeometryPolygon: exterior ring centroid, slightly above
        centroid_nodes("GeometryPolygon", f"""
            MATCH (p:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(rng:GeometryRing)
            WHERE true{no_osm('p')}
            WITH p, collect(rng.pos_list) AS pls
            RETURN elementId(p) AS eid, labels(p) AS labels, properties(p) AS props, pls
        """, True, 3.0, lambda r: centroid_of_pos_lists(r["pls"]))

        # GeometryRing: exact geometry position, slightly below
        centroid_nodes("GeometryRing", f"""
            MATCH (rng:GeometryRing) WHERE rng.pos_list IS NOT NULL{no_osm('rng')}
            RETURN elementId(rng) AS eid, labels(rng) AS labels, properties(rng) AS props,
                   rng.pos_list AS pos_str
        """, True, -3.0, lambda r: centroid_of_pos_str(r["pos_str"]))

        centroid_nodes("GeometryLineString", f"""
            MATCH (ls:GeometryLineString) WHERE ls.pos_list IS NOT NULL{no_osm('ls')}
            RETURN elementId(ls) AS eid, labels(ls) AS labels, properties(ls) AS props,
                   ls.pos_list AS pos_str
        """, True, 0.0, lambda r: centroid_of_pos_str(r["pos_str"]))

        # TerrainIntersection: centroid of its line strings
        centroid_nodes("TerrainIntersection", """
            MATCH (ti:TerrainIntersection)-[:HAS_LINE]->(ls:GeometryLineString)
            WITH ti, collect(ls.pos_list) AS pls
            RETURN elementId(ti) AS eid, labels(ti) AS labels, properties(ti) AS props, pls
        """, False, 0.0, lambda r: centroid_of_pos_lists(r["pls"]))

        # OSM enrichment layer: OSM buildings matched to pykci buildings and POIs attached to
        # buildings, floated above their footprints so the ENRICHED_BY / HAS_POI edges read
        # as crowd-sourced enrichment tethers.
        if include_osm_enrichment:
            t0 = time.perf_counter()
            rows = s.run("""
                MATCH (b:Building)-[:ENRICHED_BY|HAS_POI]->(o:OsmFeature)
                WHERE o.center_x IS NOT NULL AND o.center_y IS NOT NULL
                RETURN DISTINCT elementId(o) AS eid, labels(o) AS labels,
                       properties(o) AS props,
                       toFloat(o.center_x) AS cx, toFloat(o.center_y) AS cy
            """).data()
            for r in rows:
                is_poi = "OsmPOI" in r["labels"]
                add(r, r["cx"], r["cy"], 0.0, is_poi, 10.0 if is_poi else 24.0)
            report(len(rows), "OSM enrichment", t0)
            if not rows:
                print("  Note: no matched OsmFeature nodes found — run ingest_osm.py "
                      "first if OSM enrichment is expected.", flush=True)

        # above the centroid of their buildings, at the highest roof
        for caption, match, lookup, alt_extra in _AGGREGATE_NODES:
            t0 = time.perf_counter()
            rows = s.run(
                match + " WHERE b.center_x IS NOT NULL AND b.center_y IS NOT NULL"
                " WITH n, avg(toFloat(b.center_x)) AS cx, avg(toFloat(b.center_y)) AS cy,"
                " max(toFloat(coalesce(b.bbox_max_z, b.ground_z, 0.0))) AS top_z"
                " RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props,"
                " cx, cy, top_z AS cz"
            ).data()
            cnt = 0
            for r in rows:
                if r["cx"] is None:
                    continue
                add(r, float(r["cx"]), float(r["cy"]), float(r["cz"] or 0),
                    lookup and jitter_lookups, alt_extra)
                cnt += 1
            report(cnt, caption, t0)

    print(f"  ─── total: {len(nodes)} nodes  ({time.perf_counter()-t0_total:.2f}s)", flush=True)
    return nodes


def fetch_all_edges(driver) -> list:
    """All edges as {from_eid, to_eid, rel_type}, without the R-tree internals of
    Neo4j Spatial."""
    print("Fetching edges…", flush=True)
    t0 = time.perf_counter()
    with driver.session() as s:
        rows = s.run("""
            MATCH (a)-[r]->(b)
            WHERE NOT (a:SpatialLayer OR b:SpatialLayer)
              AND NOT type(r) IN ['RTREE_REFERENCE','RTREE_ROOT','RTREE_METADATA','RTREE_CHILD']
            RETURN elementId(a) AS from_eid, elementId(b) AS to_eid, type(r) AS rel_type
        """).data()
    print(f"  {len(rows)} edges  ({time.perf_counter()-t0:.2f}s)", flush=True)
    return rows
