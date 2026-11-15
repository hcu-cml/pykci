"""pykci.enrich_lod3_facades without a database: position lists, ids, the 2D box of a wall,
and what the index steps ask of the graph (fake_graph, conftest.py)."""

from lxml import etree

from pykci import enrich_lod3_facades as lod3


def test_position_lists_are_read_as_tuples_of_the_given_dimension():
    assert lod3._parse_poslist("1 2 3 4 5 6", 3) == [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
    assert lod3._parse_poslist("1 2 3 4", 2) == [(1.0, 2.0), (3.0, 4.0)]
    assert lod3._parse_poslist("1 2 3 x 5 6", 3) == [(1.0, 2.0, 3.0)]         # an unreadable position is skipped
    assert lod3._parse_poslist("", 3) == []


def test_local_name_of_an_element():
    assert lod3._local(etree.Element("{http://www.opengis.net/citygml/building/2.0}WallSurface")) == "WallSurface"
    assert lod3._local(etree.Element("plain")) == "plain"
    assert lod3._local(etree.Comment("a comment")) == ""


def test_node_ids_carry_the_source_prefix():
    assert lod3._pid("UUID_wall_1") == "lod3:UUID_wall_1"
    assert lod3.ID_PREFIX == "lod3:" and lod3.SPATIAL_LAYER == "lod3_features"


def test_box_of_a_surface_for_the_rtree():
    assert lod3._bbox_wkt([(0, 0, 1), (4, 0, 1), (4, 3, 5)]) == "POLYGON((0 0, 4 0, 4 3, 0 3, 0 0))"
    # a vertical wall is a line in 2D: padded so that it stays a polygon
    assert lod3._bbox_wkt([(0, 0, 0), (0, 5, 0), (0, 5, 3)]) == "POLYGON((0 0, 1e-06 0, 1e-06 5.000001, 0 5.000001, 0 0))"
    assert lod3._bbox_wkt([(0, 0, 0), (1, 1, 1)]) is None                    # fewer than three points


def test_an_id_index_for_every_label_the_facades_use(fake_graph):
    sess = fake_graph()
    lod3.ensure_indexes(sess)
    assert sess.queries == [f"CREATE INDEX {label.lower()}_id_idx IF NOT EXISTS FOR (n:{label}) ON (n.id)"
                            for label in ("BoundarySurface", "Opening", "GeometryPolygon", "GeometryRing",
                                          "GeometryMultiSurface", "Lod3Facade", "Entity", "Building")]


def test_facades_are_indexed_in_their_own_rtree_layer(fake_graph):
    sess = fake_graph({"spatial.layers": [{"name": "features"}], "spatial.addNodes": [{"c": 7}]})
    assert lod3.index_spatial(sess) == 7
    created, added = sess.calls[1], sess.calls[2]
    assert created == ("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", {"layer": "lod3_features"})
    assert "MATCH (n:Lod3Facade:BoundarySurface)" in added[0] and added[1] == {"l": "lod3_features"}


def test_without_the_spatial_plugin_nothing_is_indexed(fake_graph, capsys):
    assert lod3.index_spatial(fake_graph({"spatial.layers": RuntimeError("no such procedure")})) == 0
    assert "spatial layer setup failed" in capsys.readouterr().out


def test_the_dataset_is_found_by_id_or_name(fake_graph):
    rows = [{"id": "hh", "name": "Hamburg"}]
    assert lod3.resolve_dataset(fake_graph({"MATCH (d:Dataset)": rows}), None) == rows[0]
    assert lod3.resolve_dataset(fake_graph({"MATCH (d:Dataset)": rows}), "Hamburg") == rows[0]
