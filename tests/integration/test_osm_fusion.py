"""The OpenStreetMap fusion as a user runs it: pykci.ingest_osm on a hand-made extract around
the one building of FZK-Haus LoD2 (tests/data/fixtures/osm/fzk_haus.osm; its header says
where every feature lies and why). The assertions follow OSM_INTEGRATION.md and GRAPH.md: one
OsmFeature per OSM object, the match by footprint overlap, the crowd-sourced layer on the
building, and a re-run with other options that leaves nothing behind.

The command reads the extract through GDAL's ogr2ogr, which is not a Python package. The
tests look for it (OGR2OGR, PATH, the usual install folders) and are skipped without it.
tests/data/graph/osm_fusion.json was recorded with the code before pykci became a package.
"""

import glob
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pykci import provenance
from pykci.testing import database, snapshot
from pykci.testing.plugin import run

DATASET = "fzk_haus_lod2_kit_iai_khh_b36_v1"
SOURCE = "osm:fzk_haus"                             # osm:<file name without its suffix>

# the features of the extract inside the default clip window: id -> (osm_kind, label of the kind)
FEATURES = {
    "osm:way/1001": ("building", "OsmBuilding"),
    "osm:way/1002": ("building", "OsmBuilding"),
    "osm:relation/1001": ("building", "OsmBuilding"),   # the number of way 1001: ids are per type
    "osm:way/1004": ("other", None),
    "osm:way/1005": ("road", "OsmRoad"),
    "osm:way/1007": ("landuse", "OsmLandUse"),
    "osm:way/1008": ("water", "OsmWater"),
    "osm:way/1009": ("vegetation", "OsmVegetation"),
    "osm:node/2001": ("poi", "OsmPOI"),
    "osm:node/2002": ("poi", "OsmPOI"),
    "osm:node/2003": ("furniture", "OsmFurniture"),
}
# the ones without a counterpart in the city model, and why (OSM_INTEGRATION.md, section 9)
UNMATCHED = {
    "osm:way/1002": "no_overlap",                   # overlaps the house, but below the threshold
    "osm:relation/1001": "no_overlap",
    "osm:way/1004": "no_pykci_type",
    "osm:way/1005": "no_pykci_type",
    "osm:way/1007": "no_pykci_type",
    "osm:way/1008": "no_pykci_type",
    "osm:way/1009": "no_pykci_type",
    "osm:node/2002": "no_containing_building",
    "osm:node/2003": "no_pykci_type",               # a tree: points are not matched, and there is no CityFurniture
}

# where GDAL is when it is not on PATH: QGIS and OSGeo4W on Windows, Homebrew, QGIS on macOS
GDAL_FOLDERS = ("C:/Program Files/QGIS*/bin", "C:/OSGeo4W*/bin", "C:/Program Files/GDAL",
                "/usr/local/bin", "/opt/homebrew/bin", "/Applications/QGIS*.app/Contents/MacOS/bin")
# its data folder (osmconf.ini, which the OSM driver needs), relative to the folder of ogr2ogr
GDAL_DATA_FOLDERS = ("../apps/gdal/share/gdal", "../share/gdal", "gdal-data", "../Resources/gdal")


def _find_ogr2ogr() -> Path | None:
    """ogr2ogr of GDAL: the file OGR2OGR names, else the one on PATH, else the one in the bin
    folder of GDAL_HOME, OSGEO4W_ROOT or CONDA_PREFIX, else in a usual install folder."""
    named = os.environ.get("OGR2OGR") or shutil.which("ogr2ogr")
    if named:
        return Path(named)
    exe = "ogr2ogr.exe" if os.name == "nt" else "ogr2ogr"
    folders = [os.path.join(os.environ[var], sub) for var in ("GDAL_HOME", "OSGEO4W_ROOT", "CONDA_PREFIX")
               if os.environ.get(var) for sub in ("bin", "Library/bin")]
    for pattern in GDAL_FOLDERS:
        folders += sorted(glob.glob(pattern), reverse=True)     # the newest version first
    return next((Path(f, exe) for f in folders if Path(f, exe).is_file()), None)


@pytest.fixture(scope="module")
def extract(data_dir) -> Path:
    return data_dir / "fixtures" / "osm" / "fzk_haus.osm"


@pytest.fixture(scope="module")
def gdal(extract, tmp_path_factory):
    """ogr2ogr in front of PATH for the commands of this module (and GDAL_DATA when that GDAL
    does not find its own data, as QGIS on Windows outside its shell), checked on the extract."""
    exe = _find_ogr2ogr()
    if exe is None or not exe.is_file():
        pytest.skip(f"no ogr2ogr (GDAL){f' at {exe}' if exe else ''}: put it on PATH or name it with OGR2OGR")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("PATH", str(exe.parent) + os.pathsep + os.environ.get("PATH", ""))
        data = next((d for d in ((exe.parent / rel).resolve() for rel in GDAL_DATA_FOLDERS)
                     if (d / "osmconf.ini").is_file()), None)
        if data and not os.environ.get("GDAL_DATA"):
            env.setenv("GDAL_DATA", str(data))
        probe = run([exe, "-f", "GeoJSON", tmp_path_factory.mktemp("gdal") / "points.geojson", extract, "points"])
        if probe.returncode:
            pytest.skip(f"{exe} does not read OSM files: {probe.stderr.strip()[-300:]}")
        yield exe


def _rows(driver, query, **params) -> list[dict]:
    return [r.data() for r in driver.execute_query(query, **params).records]


def _building(driver) -> dict:
    (row,) = _rows(driver, "MATCH (b:Building) RETURN properties(b) AS p")
    return row["p"]


def _features(driver) -> dict:
    """id -> properties of every OsmFeature in the graph."""
    return {r["p"]["id"]: r["p"] for r in _rows(driver, "MATCH (o:OsmFeature) RETURN properties(o) AS p")}


def _matches(driver) -> dict:
    """OsmFeature id -> properties of the ENRICHED_BY relationship that leads to it."""
    return {r["osm"]: r["p"] for r in _rows(
        driver, "MATCH (:Building)-[r:ENRICHED_BY]->(o:OsmFeature) RETURN o.id AS osm, properties(r) AS p")}


@pytest.fixture(scope="module")
def house(neo4j_driver, run_module, fzk_lod2) -> dict:
    """FZK-Haus LoD2 alone in the test database: the properties of its building before any
    OSM run."""
    database.wipe(neo4j_driver)
    run_module("pykci.ingest_citygml", fzk_lod2)
    return _building(neo4j_driver)


@pytest.fixture(scope="module")
def fuse(gdal, house, extract, run_module, tmp_path_factory):
    """fuse(*options) runs `pykci.ingest_osm <extract> <options>` and returns the summary it
    wrote. The run is skipped when the graph already reflects these options (every run first
    removes what the last one wrote), unless again=True."""
    cwd = tmp_path_factory.mktemp("osm")            # the command writes output/<file>_osm_summary.md here
    state = {"options": None}

    def _fuse(*options, again=False):
        if again or state["options"] != options:
            state["options"] = None
            run_module("pykci.ingest_osm", extract, *options, cwd=cwd)
            text = (cwd / "output" / "fzk_haus_osm_summary.md").read_text(encoding="utf-8")
            state.update(options=options, summary=json.loads(text[text.index("{"):text.rindex("}") + 1]))
        return state["summary"]
    return _fuse


# ---- what the fusion writes ---------------------------------------------------------------------

def test_fusion_writes_the_recorded_graph(fuse, neo4j_driver, data_dir):
    fuse()
    snapshot.check(data_dir / "graph" / "osm_fusion.json", database.census(neo4j_driver))


def test_every_object_in_the_window_is_one_osm_feature(fuse, neo4j_driver):
    """Not in the graph: the building far away (way 1010), the bench beyond the buffer (node
    2004), the fence (way 1006, a line outside the transport and water network) and the
    untagged outer ring of the relation (way 1003)."""
    fuse()
    rows = _rows(neo4j_driver, "MATCH (o:OsmFeature) RETURN o.id AS id, labels(o) AS labels, properties(o) AS p")
    assert {r["id"]: (r["p"]["osm_kind"], *sorted(set(r["labels"]) - {"OsmFeature", "Entity"})) for r in rows} \
        == {i: (kind, *([label] if label else [])) for i, (kind, label) in FEATURES.items()}
    for r in rows:
        assert "Entity" in r["labels"] and r["p"]["source"] == SOURCE
        assert r["id"] == f"osm:{r['p']['osm_type']}/{r['p']['osm_id']}"
    # each one belongs to the dataset of the city model
    assert _rows(neo4j_driver, "MATCH (o:OsmFeature)-[:PART_OF]->(d:Dataset) "
                               "RETURN d.id AS dataset, count(o) AS n") == [{"dataset": DATASET, "n": len(FEATURES)}]


def _stored(props: dict, key: str):
    """The value an OsmFeature holds for an OSM tag: in other_tags, or under its own key."""
    rest = json.loads(props.get("other_tags", "{}"))
    if key in rest:
        return rest[key]
    return props.get("name" if key == "name" else key.replace("addr:", "addr_") if key.startswith("addr:")
                     else "osm_" + re.sub(r"[^a-z0-9_]", "_", key.lower()))


def _tags(extract) -> dict:
    """OsmFeature id -> tags, read from the extract."""
    return {f"osm:{el.tag}/{el.get('id')}": {t.get("k"): t.get("v") for t in el.findall("tag")}
            for el in ET.parse(extract).getroot() if el.tag in ("node", "way", "relation")}


def test_tags_become_properties_and_the_rest_is_kept(fuse, neo4j_driver, extract):
    fuse()
    features = _features(neo4j_driver)
    main = features["osm:way/1001"]
    assert {k: v for k, v in main.items() if k == "name" or k.startswith(("osm_", "addr_"))} == {
        "osm_id": "1001", "osm_type": "way", "osm_kind": "building",
        "name": "FZK-Haus", "osm_building": "house", "osm_height": "6.5", "osm_building_levels": "2",
        "osm_building_levels_underground": "0", "osm_roof_shape": "gabled", "osm_roof_material": "roof_tiles",
        "osm_start_date": "2020", "osm_operator": "KIT",
        "addr_street": "Spöcker Straße", "addr_housenumber": "4711", "addr_postcode": "76344",
        "addr_city": "Eggenstein-Leopoldshafen", "addr_country": "DE",
    }
    # the quote and the backslash come through the text form GDAL hands the other tags over in
    assert json.loads(main["other_tags"]) == {"wheelchair": "yes", "description": 'Haus "Typ B" \\ Musterhaus',
                                              "source": "survey"}
    # every tag of every feature can be read back from its node
    tags = _tags(extract)
    for osm_id in FEATURES:
        for key, value in tags[osm_id].items():
            assert _stored(features[osm_id], key) == value, (osm_id, key)


def _points(pos_list: str) -> list[tuple]:
    """The positions of a 2D pos_list, rounded to 10 cm (the extract is exact to 1 cm)."""
    values = [round(float(v), 1) for v in pos_list.split()]
    return list(zip(values[0::2], values[1::2]))


def _rectangle(x0, y0, x1, y1) -> set:
    return {(x0, y0), (x1, y0), (x1, y1), (x0, y1)}


def test_geometry_is_stored_in_the_crs_of_the_dataset(fuse, neo4j_driver):
    """The extract is WGS84; the graph holds EPSG:25832 like the city model, in the geometry
    nodes of CityGML (polygon and rings, a hole as interior ring, a line as line string)."""
    fuse()
    rings = {r["id"]: r for r in _rows(
        neo4j_driver, "MATCH (o:OsmFeature)-[f:HAS_FOOTPRINT]->(p:GeometryPolygon)-[e]->(r:GeometryRing) "
                      "RETURN r.id AS id, type(e) AS edge, properties(e) AS edge_props, f.order AS polygon_order, "
                      "r.pos_list AS pos_list, r.ring_type AS ring_type, r.srs_dimension AS dim, r.source AS source")}
    outline = rings["osm:way/1001_poly0_ext"]
    assert set(_points(outline["pos_list"])) == _rectangle(457842.5, 5439083.3, 457854.5, 5439093.3)
    assert _points(outline["pos_list"])[0] == _points(outline["pos_list"])[-1]         # closed
    assert (outline["edge"], outline["ring_type"], outline["dim"], outline["polygon_order"], outline["source"]) \
        == ("HAS_EXTERIOR_RING", "exterior", 2, 0, SOURCE)
    # the relation is one polygon with its courtyard as a hole
    yard = rings["osm:relation/1001_poly0_ext"]
    assert set(_points(yard["pos_list"])) == _rectangle(457833, 5439094, 457840, 5439101)
    hole = rings["osm:relation/1001_poly0_int0"]
    assert set(_points(hole["pos_list"])) == _rectangle(457835, 5439096, 457838, 5439099)
    assert (hole["edge"], hole["edge_props"], hole["ring_type"]) == ("HAS_INTERIOR_RING", {"ring_index": 0}, "interior")
    assert len(rings) == 8                                              # seven polygons, one hole
    (street,) = _rows(neo4j_driver, "MATCH (o:OsmFeature)-[l:HAS_LOD_SURFACE]->(g:GeometryLineString) "
                                    "RETURN o.id AS osm, g.id AS id, l.order AS line_order, g.pos_list AS pos_list")
    assert (street["osm"], street["id"], street["line_order"]) == ("osm:way/1005", "osm:way/1005_line0", 0)
    assert _points(street["pos_list"]) == [(457834.0, 5439076.0), (457848.0, 5439076.0), (457862.0, 5439076.0)]
    # the box, the centre and the point of a feature, as on Building and CityObject
    features = _features(neo4j_driver)
    main, cafe = features["osm:way/1001"], features["osm:node/2001"]
    assert [main[k] for k in ("bbox_min_x", "bbox_min_y", "bbox_max_x", "bbox_max_y", "center_x", "center_y")] \
        == pytest.approx([457842.5, 5439083.3, 457854.5, 5439093.3, 457848.5, 5439088.3], abs=0.02)
    assert (cafe["center_x"], cafe["center_y"]) == pytest.approx((457845, 5439086), abs=0.02)
    assert (cafe["location"].x, cafe["location"].y) == (cafe["center_x"], cafe["center_y"])


def test_osm_features_have_their_own_rtree_layer(fuse, neo4j_driver, house):
    fuse()
    def found(layer, wkt):
        return sorted(r["id"] for r in _rows(
            neo4j_driver, "CALL spatial.intersects($layer, $wkt) YIELD node RETURN node.id AS id",
            layer=layer, wkt=wkt))
    around = "POLYGON((457700 5438900, 458000 5438900, 458000 5439300, 457700 5439300, 457700 5438900))"
    assert found("osm_features", around) == sorted(FEATURES)
    assert found("features", around) == [house["id"]]                   # the layer of the city model has no OSM
    # by position: what reaches into the bounding box of the house
    assert found("osm_features", house["bbox_wkt"]) == ["osm:node/2001", "osm:way/1001", "osm:way/1002"]


def test_the_overlapping_building_is_matched_and_the_touching_one_is_not(fuse, neo4j_driver, house):
    fuse()
    (match,) = _rows(neo4j_driver, "MATCH (b:Building)-[r:ENRICHED_BY]->(o:OsmFeature) "
                                   "RETURN b.id AS building, o.id AS osm, properties(r) AS p")
    assert (match["building"], match["osm"]) == (house["id"], "osm:way/1001")
    # 111.55 m2 shared by two footprints of 120 m2: ratio over the smaller one, Jaccard over the union
    assert match["p"] == {"overlap_ratio": pytest.approx(0.930, abs=0.003), "jaccard": pytest.approx(0.868, abs=0.003),
                          "intersection_area": pytest.approx(111.55, abs=0.3), "match_type": "1:1", "is_primary": True}
    features = _features(neo4j_driver)
    assert features["osm:way/1001"]["matched"] is True and "unmatched_reason" not in features["osm:way/1001"]


def test_scalars_on_the_building_are_a_crowd_sourced_layer(fuse, neo4j_driver, house):
    fuse()
    after = _building(neo4j_driver)
    added = {k: v for k, v in after.items() if k not in house}
    assert added.pop("osm_best_jaccard") == pytest.approx(0.8687, abs=0.003)
    assert added == {
        # what holds for the whole building; the house number, the year and the tags without a
        # key of their own stay on the OsmFeature
        "osm_name": "FZK-Haus", "osm_building": "house", "osm_height": "6.5", "osm_building_levels": "2",
        "osm_roof_shape": "gabled", "osm_roof_material": "roof_tiles", "osm_operator": "KIT",
        "addr_street": "Spöcker Straße", "addr_postcode": "76344", "addr_city": "Eggenstein-Leopoldshafen",
        "osm_match_type": "1:1", "osm_match_count": 1,
    }
    for key in (*added, "osm_best_jaccard"):
        assert provenance.property_provenance(key) == provenance.CROWD_SOURCED
        assert provenance.layer_of(key, after) == provenance.CROWD_SOURCED
    # no value of the city model changed: the name is still the gml:id, the height the measured one
    assert {k: after[k] for k in house} == house
    assert after["name"] == house["id"] and (after["measured_height"], after["storeys_above_ground"]) == (6.52, 2)


def test_a_poi_inside_the_footprint_belongs_to_the_building(fuse, neo4j_driver, house):
    fuse()
    assert _rows(neo4j_driver, "MATCH (b:Building)-[r:HAS_POI]->(o:OsmFeature) "
                               "RETURN b.id AS building, o.id AS osm, properties(r) AS p") \
        == [{"building": house["id"], "osm": "osm:node/2001",
             "p": {"distance_m": pytest.approx(3.61, abs=0.02)}}]      # to the centre of the footprint
    assert _features(neo4j_driver)["osm:node/2001"]["matched"] is True
    assert "osm_amenity" not in _building(neo4j_driver)                # the POI stays a node of its own


def test_features_without_a_counterpart_stay_with_a_reason(fuse, neo4j_driver):
    fuse()
    features = _features(neo4j_driver)
    assert {i for i, p in features.items() if p["matched"]} == {"osm:way/1001", "osm:node/2001"}
    assert {i: p["unmatched_reason"] for i, p in features.items() if not p["matched"]} == UNMATCHED


def test_info_reports_the_crowd_sourced_layer(fuse, run_module):
    fuse()
    info = json.loads(run_module("pykci.cli", "info").stdout)
    assert info["layers"] == {"authoritative": True, "crowd_sourced": True,
                              "ml_predicted": False, "reconstructed": False}
    counts = info["counts"]
    assert (counts["buildings"], counts["osm_features"], counts["enriched_by"], counts["has_poi"]) == (1, 11, 1, 1)
    assert info["spatial_layers"] == ["features", "osm_features"]


def test_summary_of_the_run(fuse):
    summary = fuse()
    assert (summary["dataset"], summary["source"]) == ("FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml", SOURCE)
    assert summary["osm_features"] == 11 and summary["by_kind"] == {
        "building": 3, "other": 1, "landuse": 1, "water": 1, "vegetation": 1, "poi": 2, "furniture": 1, "road": 1}
    assert (summary["match_cases"], summary["matched"], summary["orphans"]) == ({"1:1": 1}, 2, 9)
    assert sum(summary["orphans_by_reason"].values()) == 9
    assert (summary["osm_buildings"], summary["pykci_buildings_enriched"], summary["pois_attached"]) == (3, 1, 1)


# ---- no tag is dropped, and every orphan has its reason -------------------------------------------

def test_no_tag_is_dropped(fuse, neo4j_driver, extract):
    """GDAL's default osmconf.ini leaves source, ele, note, fixme, created_by and other keys out
    before pykci.ingest_osm sees them (OSM_INTEGRATION.md, section 3: no OSM tag is dropped);
    the command passes its own (src/pykci/osmconf.ini)."""
    fuse()
    features, tags = _features(neo4j_driver), _tags(extract)
    assert _stored(features["osm:way/1001"], "source") == tags["osm:way/1001"]["source"]
    assert _stored(features["osm:node/2003"], "ele") == tags["osm:node/2003"]["ele"]


def test_a_tree_in_a_graph_without_city_furniture_has_no_type_to_match(fuse, neo4j_driver):
    """Points other than POIs are not matched; whether the graph holds their type decides the
    reason, as for areas and lines (it was no_overlap)."""
    fuse()
    assert _features(neo4j_driver)["osm:node/2003"]["unmatched_reason"] == "no_pykci_type"


def test_a_relation_is_one_feature_with_the_polygon_gdal_assembles(fuse, neo4j_driver):
    """Its members are not linked (no HAS_OSM_MEMBER): an inner way becomes a hole, and a way
    with tags of its own is a feature of its own."""
    fuse()
    assert _rows(neo4j_driver, "MATCH ()-[m:HAS_OSM_MEMBER]->() RETURN count(m) AS n") == [{"n": 0}]
    assert _rows(neo4j_driver, "MATCH (:OsmFeature {id: 'osm:relation/1001'})-[:HAS_FOOTPRINT]->(p) "
                               "-[:HAS_INTERIOR_RING]->(r) RETURN count(r) AS holes") == [{"holes": 1}]
    assert "osm:way/1004" in _features(neo4j_driver)


# ---- running it again, and with other options ---------------------------------------------------

@pytest.mark.slow
def test_second_run_changes_nothing(fuse, neo4j_driver):
    fuse()
    first, building = database.census(neo4j_driver), _building(neo4j_driver)
    fuse(again=True)
    assert database.census(neo4j_driver) == first and _building(neo4j_driver) == building


@pytest.mark.slow
def test_overlap_threshold_decides_which_buildings_match(fuse, neo4j_driver, house):
    # 0.05: the garage (ratio 0.083) matches too; two OSM buildings on one footprint are n:1
    fuse("--overlap-threshold", "0.05")
    matches = _matches(neo4j_driver)
    assert {i: (p["match_type"], p["is_primary"]) for i, p in matches.items()} \
        == {"osm:way/1001": ("n:1", True), "osm:way/1002": ("n:1", False)}
    assert matches["osm:way/1002"]["overlap_ratio"] == pytest.approx(0.083, abs=0.003)
    assert _features(neo4j_driver)["osm:way/1002"]["matched"] is True
    # the building takes its values from the OSM building it shares the larger area with
    building = _building(neo4j_driver)
    assert [building[k] for k in ("osm_match_type", "osm_match_count", "osm_name", "osm_height")] \
        == ["n:1", 2, "FZK-Haus", "6.5"]
    # 0.95 is above the best ratio (0.93): no match, and nothing of the run before stays
    fuse("--overlap-threshold", "0.95")
    assert _matches(neo4j_driver) == {}
    assert _building(neo4j_driver) == house
    assert _features(neo4j_driver)["osm:way/1001"]["unmatched_reason"] == "no_overlap"
    assert _features(neo4j_driver)["osm:node/2001"]["matched"] is True     # a POI needs no overlap


@pytest.mark.slow
def test_buffer_decides_what_is_read(fuse, neo4j_driver, data_dir):
    # 50 m: the bench 30 m east of the house is read, the building 136 m east is not; no new match
    summary = fuse("--buffer", "50")
    features = _features(neo4j_driver)
    assert set(features) == set(FEATURES) | {"osm:node/2004"}
    assert features["osm:node/2004"]["unmatched_reason"] == "no_containing_building"
    assert (summary["match_cases"], summary["pois_attached"]) == ({"1:1": 1}, 1)
    # 0 m: only what reaches into the bounding box of the building
    fuse("--buffer", "0")
    assert set(_features(neo4j_driver)) == {"osm:way/1001", "osm:way/1002", "osm:node/2001"}
    # the default again: the recorded graph, with nothing left of the wider window
    fuse()
    snapshot.check(data_dir / "graph" / "osm_fusion.json", database.census(neo4j_driver))


@pytest.mark.slow
def test_dataset_is_found_or_named(fuse, run_module, neo4j_driver, extract, tmp_path, data_dir):
    fuse("--dataset", DATASET)                      # named: the same graph as found
    recorded = database.census(neo4j_driver)
    snapshot.check(data_dir / "graph" / "osm_fusion.json", recorded)
    # an unknown name stops the command before it changes the graph
    failed = run_module("pykci.ingest_osm", extract, "--dataset", "no-such-dataset", cwd=tmp_path, ok=False)
    assert failed.returncode != 0 and "no matching Dataset" in failed.stderr
    assert database.census(neo4j_driver) == recorded


def test_without_ogr2ogr_the_command_stops_before_it_connects(run_module, extract, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))       # a PATH without GDAL
    failed = run_module("pykci.ingest_osm", extract, cwd=tmp_path, ok=False)
    assert failed.returncode != 0 and "ogr2ogr (GDAL) not found on PATH" in failed.stderr
    assert "Connecting" not in failed.stdout and not (tmp_path / "output").exists()
