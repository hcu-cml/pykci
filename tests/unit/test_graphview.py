"""pykci.graphview and the two visualisers built on it (pykci.build_graph_pointcloud,
pykci.visualize_graph_3d): groups, captions, colours, which nodes are drawn and where."""

import pytest

from pykci import build_graph_pointcloud, graphview, visualize_graph_3d
from pykci.graphview import (DEFAULT_COLOR, LABEL_COLORS, PIXEL_SIZE, SKIP_PROPS, fetch_all_edges, fetch_all_nodes,
                             fetch_dataset, node_group, node_label, xy_jitter)

NODES = [
    # (labels, properties, caption, group, group in the point cloud)
    (["Dataset"], {"name": "Hamburg LoD2 2025 with a long name", "id": "ds"}, "Hamburg LoD2 2025 with a", "Dataset", "Dataset"),
    (["City", "Entity"], {"id": "hamburg"}, "hamburg", "City", "City"),
    (["District"], {"name": "Altona-Altstadt-Nord-West-XYZ", "id": "d1"}, "Altona-Altstadt-Nord", "District", "District"),
    (["BuildingFunction"], {"code": "31001_1000", "id": "fn_1"}, "31001_1000", "BuildingFunction", "BuildingFunction"),
    (["BuildingFunction"], {"name": "Residential building, detached", "code": "31001_1000", "id": "fn_1"},
     "Residential building", "BuildingFunction", "BuildingFunction"),
    (["RoofType"], {"id": "rt_3100"}, "rt_3100", "RoofType", "RoofType"),
    (["Building", "Entity"], {"id": "DEHHALKA4IB000CvXYZ123456789"}, "DEHHALKA4IB000CvXYZ1", "Building", "Building"),
    (["BoundarySurface", "Entity"], {"surface_type": "OuterCeilingSurface", "id": "bs1"},
     "OuterCeilingSurf", "OuterCeilingSurface", "OuterCeilingSurface"),
    (["BoundarySurface"], {"id": "a_very_long_boundary_surface_id"}, "a_very_long_boun", "BoundarySurface", "BoundarySurface"),
    (["GeometryRing"], {"id": "ring_ext_poly_0123456789"}, "ring_ext_pol", "GeometryRing", "GeometryRing"),
    (["CityObject", "Road", "Entity"], {"feature_type": "Road", "id": "road_1"}, "road_1", "Road", "Road"),
    # the enrichment layers and the generic Entity label are only known to the point cloud
    (["OsmFeature", "OsmBuilding", "Entity"], {"id": "osm:way/1"}, "osm:way/1", "OsmFeature", "OsmBuilding"),
    (["CityObject", "Entity"], {"id": "x"}, "x", "Entity", "CityObject"),
    (["Entity"], {}, "", "Entity", "CityObject"),
    ([], {}, "", "CityObject", "CityObject"),
]


@pytest.mark.parametrize("labels, props, caption", [n[:3] for n in NODES])
def test_caption(labels, props, caption):
    assert node_label(labels, props) == caption


@pytest.mark.parametrize("labels, props, group", [(n[0], n[1], n[3]) for n in NODES])
def test_group(labels, props, group):
    assert node_group(labels, props) == group


@pytest.mark.parametrize("labels, props, group", [(n[0], n[1], n[4]) for n in NODES])
def test_group_in_the_point_cloud(labels, props, group):
    assert node_group(labels, props, order=build_graph_pointcloud._GROUP_LABELS,
                      generic=("CityObject", "Entity")) == group


def test_jitter_is_small_and_the_same_on_every_run():
    assert xy_jitter("4:abc:17") == pytest.approx((0.47204406264427895, 0.909719731764913))
    assert xy_jitter("x", 5.0) == pytest.approx((-0.5599521057443236, -0.5033114136886112))
    dx, dy = xy_jitter("anything", 2.0)
    assert (dx * dx + dy * dy) ** 0.5 <= 2.0


def test_colour_and_size_tables():
    assert len(LABEL_COLORS) == 31 and len(PIXEL_SIZE) == 17
    assert list(LABEL_COLORS)[:4] == ["Building", "Dataset", "District", "City"]   # the legend order
    assert LABEL_COLORS["Building"] == "#FF7043" and DEFAULT_COLOR == "#90A4AE"
    assert PIXEL_SIZE["Building"] == 14 and PIXEL_SIZE["GeometryRing"] == 4
    assert SKIP_PROPS == {"pos_list", "embedding", "embedding_ollama", "bbox_wkt", "location"}


def test_the_point_cloud_adds_the_enrichment_layers_to_the_shared_tables():
    extra = ["OsmBuilding", "OsmPOI", "Lod3Facade", "Opening"]
    assert list(build_graph_pointcloud.LABEL_COLORS) == [*LABEL_COLORS, *extra]
    assert list(build_graph_pointcloud._PIXEL_SIZE) == [*PIXEL_SIZE, *extra]
    assert build_graph_pointcloud.LABEL_COLORS["OsmBuilding"] == "#43A047"
    assert not set(extra) & set(LABEL_COLORS)


# ---- which nodes are drawn: a graph that answers from canned rows (fake_graph, conftest.py) ---------

def _row(eid, labels, **extra):
    return {"eid": eid, "labels": labels, "props": {"id": eid}, **extra}


GRAPH = {
    "n:Building OR n:CityObject": [_row("b1", ["Building"], cx=10.0, cy=20.0, gz=2.0, mh=8.0, bz2=12.0),
                                   _row("c1", ["CityObject"], cx=1.0, cy=2.0, gz=5.0, mh=0.0, bz2=9.0)],
    "(bs:BoundarySurface)": [_row("s1", ["BoundarySurface"], pls=["0 0 0 2 2 2"]),
                             _row("s0", ["BoundarySurface"], pls=[])],             # no ring: not drawn
    "(s:GeometrySolid)": [_row("so1", ["GeometrySolid"], pls=["0 0 0 4 4 4"])],
    "(ms:GeometryMultiSurface)": [_row("ms1", ["GeometryMultiSurface"], pls=["1 1 1"])],
    "(p:GeometryPolygon)": [_row("p1", ["GeometryPolygon"], pls=["3 3 3"])],
    "MATCH (rng:GeometryRing) WHERE": [_row("r1", ["GeometryRing"], pos_str="5 5 5 7 7 7")],
    "(ls:GeometryLineString) WHERE": [_row("l1", ["GeometryLineString"], pos_str="9 9 9")],
    "(ti:TerrainIntersection)": [_row("t1", ["TerrainIntersection"], pls=["8 8 8"])],
    "(o:OsmFeature)": [_row("o1", ["OsmFeature", "OsmBuilding"], cx=11.0, cy=21.0),
                       _row("o2", ["OsmFeature", "OsmPOI"], cx=12.0, cy=22.0)],
    "(n:BuildingFunction)": [_row("f1", ["BuildingFunction"], cx=10.0, cy=20.0, cz=12.0)],
    "(n:RoofType)": [_row("rt1", ["RoofType"], cx=10.0, cy=20.0, cz=None),
                     _row("rt0", ["RoofType"], cx=None, cy=None, cz=None)],          # no building: not drawn
    "(n:District)": [_row("d1", ["District"], cx=10.0, cy=20.0, cz=12.0)],
    "(n:City)": [_row("ci1", ["City"], cx=10.0, cy=20.0, cz=12.0)],
    "(n:Dataset)": [_row("ds1", ["Dataset"], cx=10.0, cy=20.0, cz=12.0)],
}


def _placed(nodes):
    return {n["eid"]: (n["cx"], n["cy"], n["cz"], n["jitter"], n["alt_extra"]) for n in nodes}


def test_every_kind_of_node_is_placed(fake_graph):
    nodes = fetch_all_nodes(fake_graph(GRAPH))
    assert [n["eid"] for n in nodes] == ["b1", "c1", "s1", "so1", "ms1", "p1", "r1", "l1", "t1",
                                         "f1", "rt1", "d1", "ci1", "ds1"]
    assert set(nodes[0]) == {"eid", "labels", "props", "cx", "cy", "cz", "jitter", "alt_extra"}
    assert _placed(nodes) == {
        "b1": (10.0, 20.0, 6.0, False, 0.0),          # half the measured height above the ground
        "c1": (1.0, 2.0, 7.0, False, 0.0),            # no height: half way up its bounding box
        "s1": (1.0, 1.0, 1.0, True, 0.0),
        "so1": (2.0, 2.0, 2.0, True, 6.0),
        "ms1": (1.0, 1.0, 1.0, True, 4.0),
        "p1": (3.0, 3.0, 3.0, True, 3.0),
        "r1": (6.0, 6.0, 6.0, True, -3.0),
        "l1": (9.0, 9.0, 9.0, True, 0.0),
        "t1": (8.0, 8.0, 8.0, False, 0.0),
        "f1": (10.0, 20.0, 12.0, False, 8.0),
        "rt1": (10.0, 20.0, 0.0, False, 5.0),
        "d1": (10.0, 20.0, 12.0, False, 80.0),
        "ci1": (10.0, 20.0, 12.0, False, 150.0),
        "ds1": (10.0, 20.0, 12.0, False, 220.0),
    }


def test_lookup_values_can_be_spread_like_geometry_nodes(fake_graph):
    placed = _placed(fetch_all_nodes(fake_graph(GRAPH), jitter_lookups=True))
    assert placed["f1"][3] is True and placed["rt1"][3] is True
    assert placed["d1"][3] is False and placed["ci1"][3] is False and placed["ds1"][3] is False


def test_osm_enrichment_floats_above_the_buildings_when_asked(fake_graph):
    driver = fake_graph(GRAPH)
    placed = _placed(fetch_all_nodes(driver, include_osm_enrichment=True))
    assert placed["o1"] == (11.0, 21.0, 0.0, False, 24.0)      # a matched OSM building
    assert placed["o2"] == (12.0, 22.0, 0.0, True, 10.0)       # a POI
    assert "o1" not in _placed(fetch_all_nodes(fake_graph(GRAPH)))


def test_osm_geometry_can_be_left_out(fake_graph):
    plain, filtered = fake_graph(GRAPH), fake_graph(GRAPH)
    fetch_all_nodes(plain)
    fetch_all_nodes(filtered, exclude_osm_geometry=True)
    osm = "STARTS WITH 'osm'"
    assert not any(osm in q for q in plain.queries)
    assert sum(osm in q for q in filtered.queries) == 3        # polygons, rings, line strings


def test_edges_skip_the_rtree_internals(fake_graph):
    driver = fake_graph({"MATCH (a)-[r]->(b)": [{"from_eid": "b1", "to_eid": "s1", "rel_type": "HAS_BOUNDARY"}]})
    assert fetch_all_edges(driver) == [{"from_eid": "b1", "to_eid": "s1", "rel_type": "HAS_BOUNDARY"}]
    (query,) = driver.queries
    assert "NOT (a:SpatialLayer OR b:SpatialLayer)" in query and "RTREE_REFERENCE" in query


def test_dataset_crs_and_sample_point(fake_graph):
    assert fetch_dataset(fake_graph({"MATCH (d:Dataset)": [{"crs": "EPSG:25832", "x": 1, "y": 2, "z": None}]})) == (
        "EPSG:25832", 1.0, 2.0, 0.0)
    assert fetch_dataset(fake_graph({})) == ("EPSG:25832", 0.0, 0.0, 0.0)       # an empty graph


# ---- raw nodes → what each visualiser draws ---------------------------------------------------------

RAW = [
    {"eid": "b1", "labels": ["Building", "Entity"], "props": {"id": "b1", "pos_list": "…"},
     "cx": 565590.94, "cy": 5933912.17, "cz": 10.0, "jitter": False, "alt_extra": 0.0},
    {"eid": "o1", "labels": ["OsmFeature", "OsmBuilding", "Entity"], "props": {"id": "osm:way/1"},
     "cx": 565600.0, "cy": 5933920.0, "cz": 0.0, "jitter": True, "alt_extra": 24.0},
]
EDGES = [{"from_eid": "b1", "to_eid": "o1", "rel_type": "ENRICHED_BY"},
         {"from_eid": "b1", "to_eid": "gone", "rel_type": "HAS_BOUNDARY"}]


def test_point_cloud_nodes_and_edges():
    nodes, eid_map = build_graph_pointcloud.project_nodes(RAW, "EPSG:25832", geoid_m=40.0)
    building, osm = nodes
    assert (building["lon"], building["lat"]) == pytest.approx((9.99, 53.55), abs=1e-5)
    assert building["alt"] == 50.0 and osm["alt"] == 64.0             # height + geoid + lift of the kind
    assert "pos_list" not in building["props"]
    assert (building["group"], building["color"], building["pixelSize"], building["priority"]) == (
        "Building", "#FF7043", 14, 1)
    assert (building["r"], building["g"], building["b"]) == (255, 112, 67)
    assert (osm["group"], osm["color"], osm["pixelSize"]) == ("OsmBuilding", "#43A047", 12)
    assert eid_map == {"b1": 0, "o1": 1}
    # an edge to a node that is not drawn is dropped
    assert build_graph_pointcloud.build_edges(EDGES, eid_map) == [{"fi": 0, "ti": 1, "type": "ENRICHED_BY"}]


def test_cesium_page_nodes_and_edges():
    nodes, edges = visualize_graph_3d.build_payload(RAW, EDGES, "EPSG:25832", geoid_m=40.0)
    building, osm = nodes
    assert (building["lon"], building["lat"]) == pytest.approx((9.99, 53.55), abs=1e-5)
    assert building["alt"] == 50.0
    assert (building["label"], building["group"], building["color"], building["pixelSize"]) == (
        "b1", "Building", "#FF7043", 14)
    # this page has no enrichment layers: an OSM feature is grouped by its first label
    assert (osm["group"], osm["color"], osm["pixelSize"]) == ("OsmFeature", graphview.DEFAULT_COLOR, 7)
    assert edges == [{"fi": 0, "ti": 1, "type": "ENRICHED_BY"}]
