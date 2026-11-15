"""pykci.ingest_osm without a database: tags and kinds, the node an OSM feature becomes, its
geometry rows, footprint matching, and what the index steps ask of the graph."""

import json

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from pykci import ingest_osm as osm


# ---- tags and kinds -------------------------------------------------------------------------------

def test_other_tags_are_parsed_from_gdals_hstore_text():
    raw = '"addr:street"=>"Jungfernstieg","note"=>"says \\"hi\\"","empty"=>""'
    assert osm._parse_other_tags(raw) == {"addr:street": "Jungfernstieg", "note": 'says "hi"', "empty": ""}
    assert osm._parse_other_tags(None) == {}


def test_all_tags_merge_columns_and_other_tags_without_ids_and_empty_values():
    props = {"osm_id": "1", "osm_way_id": None, "name": "Rathaus", "amenity": "",
             "other_tags": '"building"=>"civic"'}
    assert osm._all_tags(props) == {"name": "Rathaus", "building": "civic"}


@pytest.mark.parametrize("tags, kind", [
    ({"building": "yes", "amenity": "cafe"}, osm.KIND_BUILDING),          # a building first, whatever else
    ({"building:part": "yes"}, osm.KIND_BUILDING),
    ({"natural": "water"}, osm.KIND_WATER),
    ({"waterway": "riverbank"}, osm.KIND_WATER),
    ({"leisure": "park"}, osm.KIND_VEGETATION),
    ({"landuse": "forest"}, osm.KIND_VEGETATION),
    ({"landuse": "residential"}, osm.KIND_LANDUSE),
    ({"highway": "residential"}, osm.KIND_ROAD),
    ({"railway": "rail"}, osm.KIND_ROAD),
    ({"natural": "tree"}, osm.KIND_FURNITURE),
    ({"barrier": "bollard"}, osm.KIND_FURNITURE),
    ({"shop": "bakery"}, osm.KIND_POI),
    ({"name": "something"}, osm.KIND_OTHER),
])
def test_kind_of_a_feature(tags, kind):
    assert osm.classify_kind(tags, "Polygon") == kind
    assert kind in osm.KIND_LABEL


# ---- the OsmFeature node --------------------------------------------------------------------------

def _feature(geom_type, **props):
    return {"properties": props, "geometry": {"type": geom_type}}


def test_a_closed_way_polygon_is_a_way_and_a_relation_polygon_a_relation():
    way = osm.build_osm_props(_feature("Polygon", osm_way_id="42", osm_id=None), osm.KIND_BUILDING, "osm:x")
    assert (way["id"], way["osm_type"], way["osm_id"]) == ("osm:way/42", "way", "42")
    rel = osm.build_osm_props(_feature("MultiPolygon", osm_way_id=None, osm_id="5713700"), osm.KIND_WATER, "osm:x")
    assert (rel["id"], rel["osm_type"]) == ("osm:relation/5713700", "relation")
    line = osm.build_osm_props(_feature("LineString", osm_id="7"), osm.KIND_ROAD, "osm:x")
    assert line["id"] == "osm:way/7"
    point = osm.build_osm_props(_feature("Point", osm_id="-9"), osm.KIND_POI, "osm:x")
    assert point["id"] == "osm:node/9"                               # the legacy negative id is not kept


def test_tags_become_namespaced_properties_and_the_rest_is_kept_as_json():
    node = osm.build_osm_props(_feature(
        "Polygon", osm_way_id="42", name="Rathaus", building="civic", osm_version="3",
        other_tags='"building:levels"=>"4","addr:street"=>"Rathausmarkt","addr:housenumber"=>"1","wikidata"=>"Q1"'),
        osm.KIND_BUILDING, "osm:hamburg")
    assert node == {
        "id": "osm:way/42", "osm_id": "42", "osm_type": "way", "osm_kind": "building", "source": "osm:hamburg",
        "name": "Rathaus", "osm_building": "civic", "osm_building_levels": "4",
        "addr_street": "Rathausmarkt", "addr_housenumber": "1", "osm_version": "3",
        # every tag that has no key of its own, the metadata columns included
        "other_tags": json.dumps({"osm_version": "3", "wikidata": "Q1"}),
    }


# ---- geometry rows --------------------------------------------------------------------------------

def test_bbox_fields_of_a_geometry():
    assert osm.geom_bbox_fields(box(0, 0, 4, 2)) == {
        "bbox_min_x": 0.0, "bbox_min_y": 0.0, "bbox_max_x": 4.0, "bbox_max_y": 2.0,
        "center_x": 2.0, "center_y": 1.0,
        "bbox_wkt": "POLYGON((0.0 0.0, 4.0 0.0, 4.0 2.0, 0.0 2.0, 0.0 0.0))",
    }
    assert osm.geom_bbox_fields(Point(3, 4))["center_x"] == 3.0


def test_polygon_rows_follow_the_citygml_geometry_schema():
    holed = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(2, 2), (4, 2), (4, 4), (2, 4)]])
    polys, rings, foot, ext, inner = osm.ring_rows(MultiPolygon([holed, box(20, 0, 21, 1)]), "osm:way/1", "osm:x")
    assert [p["id"] for p in polys] == ["osm:way/1_poly0", "osm:way/1_poly1"]
    assert foot == [{"src": "osm:way/1", "tgt": "osm:way/1_poly0", "order": 0},
                    {"src": "osm:way/1", "tgt": "osm:way/1_poly1", "order": 1}]
    assert [(r["id"], r["ring_type"], r["srs_dimension"]) for r in rings] == [
        ("osm:way/1_poly0_ext", "exterior", 2), ("osm:way/1_poly0_int0", "interior", 2),
        ("osm:way/1_poly1_ext", "exterior", 2)]
    assert rings[0]["pos_list"] == "0.0 0.0 10.0 0.0 10.0 10.0 0.0 10.0 0.0 0.0"       # x y pairs, closed
    assert ext == [{"src": "osm:way/1_poly0", "tgt": "osm:way/1_poly0_ext"},
                   {"src": "osm:way/1_poly1", "tgt": "osm:way/1_poly1_ext"}]
    assert inner == [{"src": "osm:way/1_poly0", "tgt": "osm:way/1_poly0_int0", "ring_index": 0}]


def test_line_rows():
    lines, rels = osm.line_rows(LineString([(0, 0), (5, 5)]), "osm:way/2", "osm:x")
    assert lines == [{"id": "osm:way/2_line0", "pos_list": "0.0 0.0 5.0 5.0", "srs_dimension": 2, "source": "osm:x"}]
    assert rels == [{"src": "osm:way/2", "tgt": "osm:way/2_line0", "order": 0}]


# ---- matching OSM features to footprints of the city model --------------------------------------------

def test_overlapping_footprints_match_with_ratio_and_jaccard():
    edges, comps = osm.match_areas({"o1": box(0, 0, 10, 10)}, {"b1": box(0, 0, 10, 5), "b2": box(100, 100, 110, 110)},
                                   tau=0.3)
    assert edges == [{"osm": "o1", "pykci": "b1", "overlap_ratio": 1.0, "jaccard": 0.5, "inter_area": 50.0}]
    assert comps == [({"o1"}, {"b1"})]


def test_one_osm_building_over_two_footprints_is_one_component():
    edges, comps = osm.match_areas({"o1": box(0, 0, 10, 10)}, {"b1": box(0, 0, 5, 10), "b2": box(5, 0, 10, 10)}, tau=0.3)
    assert {(e["osm"], e["pykci"]) for e in edges} == {("o1", "b1"), ("o1", "b2")}
    assert comps == [({"o1"}, {"b1", "b2"})]
    assert osm.classify_case(1, 2) == "1:n"


def test_a_small_overlap_below_the_threshold_is_no_match():
    assert osm.match_areas({"o1": box(0, 0, 10, 10)}, {"b1": box(9, 9, 19, 19)}, tau=0.3) == ([], [])
    assert osm.match_areas({}, {"b1": box(0, 0, 1, 1)}, tau=0.3) == ([], [])


def test_a_line_matches_by_the_share_of_its_length_inside():
    edges, _ = osm.match_areas({"road": LineString([(0, 5), (20, 5)])}, {"area": box(0, 0, 10, 10)}, tau=0.3)
    assert edges == [{"osm": "road", "pykci": "area", "overlap_ratio": 0.5, "jaccard": 0.5, "inter_area": 10.0}]


@pytest.mark.parametrize("n_osm, n_pykci, case", [(1, 1, "1:1"), (1, 3, "1:n"), (2, 1, "n:1"), (2, 2, "n:m")])
def test_match_cases(n_osm, n_pykci, case):
    assert osm.classify_case(n_osm, n_pykci) == case


# ---- what the index steps ask of the graph (fake_graph, conftest.py) -----------------------------------

def test_indexes_of_the_osm_features(fake_graph):
    sess = fake_graph()
    osm.ensure_indexes(sess)
    assert sess.queries == [
        "CREATE INDEX osmfeature_id_idx IF NOT EXISTS FOR (n:OsmFeature) ON (n.id)",
        "CREATE INDEX osmfeature_osm_id_idx IF NOT EXISTS FOR (n:OsmFeature) ON (n.osm_id)",
        "CREATE INDEX osmfeature_osm_kind_idx IF NOT EXISTS FOR (n:OsmFeature) ON (n.osm_kind)",
        "CREATE POINT INDEX osmfeature_location_idx IF NOT EXISTS FOR (n:OsmFeature) ON (n.location)",
    ]


def test_city_objects_get_a_bbox_and_enter_the_rtree_after_fusion(fake_graph):
    sess = fake_graph({
        "c.bbox_wkt IS NULL AND c.rtree_layer IS NULL": [{"id": "road_1"}],
        "OPTIONAL MATCH": [{"id": "road_1", "pl": ["0 0 0 10 5 2"]}],
        "c.bbox_wkt IS NOT NULL": [{"id": "road_1"}, {"id": "road_2"}],
        "spatial.addNodes": [{"n": 2}],
    })
    assert osm.index_cityobjects_post_fusion(sess, layer="features") == 2
    asked = sess.queries
    assert "c.rtree_layer IS NULL" in asked[0]                  # features of another layer are left alone
    assert "SET n.bbox_wkt=row.wkt" in asked[2]
    assert sess.calls[2][1]["rows"][0]["wkt"] == "POLYGON((0.0 0.0, 10.0 0.0, 10.0 5.0, 0.0 5.0, 0.0 0.0))"
    assert "NOT ()-[:RTREE_REFERENCE]->(c)" in asked[3]         # only what is not indexed yet
    assert sess.calls[4][1] == {"ids": ["road_1", "road_2"], "layer": "features"}
