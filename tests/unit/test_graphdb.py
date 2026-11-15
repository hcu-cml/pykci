"""pykci.graphdb: ids, indexes, R-tree layers and bounding boxes, against a session that
records what it is asked (no database; `fake_graph` is in conftest.py)."""

import pytest

from pykci.graphdb import (backfill_bbox_from_geometry, create_indexes, create_point_index,
                           ensure_spatial_layer, slug)


# ---- slug ---------------------------------------------------------------------------------------

def test_slug_keeps_ascii_letters_digits_and_underscore():
    assert slug("a_b9") == "a_b9"
    assert slug("FZK-Haus LoD2 (KIT) äö") == "fzk_haus_lod2__kit____"


def test_the_ingest_modules_use_this_slug():
    from pykci import ingest_citygml, ingest_osm

    assert ingest_citygml._slug is slug and ingest_osm._slug is slug


# ---- indexes ------------------------------------------------------------------------------------

def test_range_indexes_are_named_after_label_and_property(fake_graph):
    sess = fake_graph()
    create_indexes(sess, [("Building", "id"), ("CityObject", "feature_type"),
                          ("BoundarySurface", "multi_surface_id")])
    assert sess.queries == [
        "CREATE INDEX building_id_idx IF NOT EXISTS FOR (n:Building) ON (n.id)",
        "CREATE INDEX cityobject_feature_type_idx IF NOT EXISTS FOR (n:CityObject) ON (n.feature_type)",
        "CREATE INDEX boundarysurface_multi_surface_id_idx IF NOT EXISTS "
        "FOR (n:BoundarySurface) ON (n.multi_surface_id)",
    ]


def test_a_failing_index_stops_unless_tolerant(fake_graph):
    failing = {"cityobject_id_idx": RuntimeError("an equivalent index exists")}
    with pytest.raises(RuntimeError):
        create_indexes(fake_graph(failing), [("CityObject", "id"), ("Building", "id")])
    sess = fake_graph(failing)
    create_indexes(sess, [("CityObject", "id"), ("Building", "id")], tolerant=True)
    assert len(sess.queries) == 2                       # the second one is still created


def test_point_index_on_location_never_fails(fake_graph):
    sess = fake_graph()
    create_point_index(sess, "OsmFeature")
    assert sess.queries == ["CREATE POINT INDEX osmfeature_location_idx IF NOT EXISTS "
                            "FOR (n:OsmFeature) ON (n.location)"]
    create_point_index(fake_graph({"POINT INDEX": RuntimeError("not supported")}), "Building")


# ---- R-tree layers ------------------------------------------------------------------------------

def test_a_missing_layer_is_created_on_bbox_wkt(fake_graph, capsys):
    sess = fake_graph({"spatial.layers": [{"name": "features"}]})
    assert ensure_spatial_layer(sess, "osm_features") is True
    assert sess.calls[-1] == ("CALL spatial.addWKTLayer($layer, 'bbox_wkt')", {"layer": "osm_features"})
    assert capsys.readouterr().out == ""                # silent unless asked to report


def test_an_existing_layer_is_left_alone(fake_graph, capsys):
    sess = fake_graph({"spatial.layers": [{"name": "features"}]})
    assert ensure_spatial_layer(sess, "features", report=True) is True
    assert len(sess.calls) == 1
    assert "already exists" in capsys.readouterr().out


def test_creation_is_reported_when_asked(fake_graph, capsys):
    assert ensure_spatial_layer(fake_graph(), "features", report=True) is True
    assert "Spatial layer 'features' created." in capsys.readouterr().out


def test_without_the_spatial_plugin_the_answer_is_false_with_a_warning(fake_graph, capsys):
    sess = fake_graph({"spatial.layers": RuntimeError("There is no procedure with the name `spatial.layers`")})
    assert ensure_spatial_layer(sess, "features") is False
    assert "WARNING: spatial layer setup failed" in capsys.readouterr().out


# ---- bounding boxes from stored geometry -----------------------------------------------------------

def test_backfill_writes_the_box_of_the_geometry_below_a_feature(fake_graph):
    sess = fake_graph({"OPTIONAL MATCH": [
        {"id": "road_1", "pl": ["0 0 1 10 5 2", "-3 2 7 4 8 0"]},
        {"id": "flat", "pl": ["5 0 0 5 9 3"]},              # no extent in x: no box
        {"id": "bare", "pl": []},                           # no geometry
    ]})
    assert backfill_bbox_from_geometry(sess, ["road_1", "flat", "bare"]) == 1
    read, write = sess.calls
    assert read[1] == {"ids": ["road_1", "flat", "bare"]}
    assert "SET n.bbox_wkt=row.wkt" in write[0]
    assert write[1]["rows"] == [{
        "id": "road_1",
        "wkt": "POLYGON((-3.0 0.0, 10.0 0.0, 10.0 8.0, -3.0 8.0, -3.0 0.0))",
        "minx": -3.0, "miny": 0.0, "minz": 0.0, "maxx": 10.0, "maxy": 8.0, "maxz": 7.0,
        "cx": 3.5, "cy": 4.0,
    }]


def test_backfill_reads_in_batches_and_writes_nothing_without_boxes(fake_graph):
    sess = fake_graph()
    assert backfill_bbox_from_geometry(sess, [f"f{i}" for i in range(5)], batch_size=2) == 0
    assert [p["ids"] for _, p in sess.calls] == [["f0", "f1"], ["f2", "f3"], ["f4"]]
    assert backfill_bbox_from_geometry(fake_graph(), []) == 0
