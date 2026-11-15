"""Reconstructed LoD3 facades as a user adds them: pykci.enrich_lod3_facades on a hand-made
facade export for the building of FZK-Haus LoD2 (tests/data/fixtures/lod3/fzk_haus_facade.gml;
its header says what it holds and which defects it carries). The assertions follow
LOD3_FACADE_INTEGRATION.md: the file is repaired in a copy, the facade is added as a layer of
its own (Lod3Facade nodes behind HAS_LOD3_FACADE, lod3_* values on the building), and the
city model stays as it was. pykci.build_3dtiles then draws the facade.

The FZK-Haus LoD3 model of the repository has the same gml:ids, but its rings are written
as gml:pos and its walls as composite surfaces, which the command does not read (see the
known defects below), so the fixture is written by hand.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pykci import provenance
from pykci.testing import database, snapshot

DATASET = "fzk_haus_lod2_kit_iai_khh_b36_v1"
CITY_MODEL = "FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml"     # Dataset.source: what build_3dtiles is given
SOURCE = "lod3:fzk_haus_facade"                         # lod3:<file name without its suffix>
ABSENT = "UUID_not-in-the-graph"                        # the second building of the file
NS = {"core": "http://www.opengis.net/citygml/2.0", "bldg": "http://www.opengis.net/citygml/building/2.0",
      "gml": "http://www.opengis.net/gml"}
GML_ID = "{http://www.opengis.net/gml}id"

P = "lod3:FZK_L3_"                                      # ids in the graph: lod3:<gml:id>
# the facade nodes: id -> label (each one is also a Lod3Facade, and an Entity unless it is a ring)
NODES = {
    P + "WALL_SOUTH": "BoundarySurface", P + "ROOF_SOUTH": "BoundarySurface",
    P + "WINDOW": "Opening", P + "DOOR": "Opening",
    P + "WINDOW_MS": "GeometryMultiSurface", P + "DOOR_MS": "GeometryMultiSurface",
    P + "WALL_SOUTH_POLY": "GeometryPolygon", P + "ROOF_SOUTH_POLY": "GeometryPolygon",
    P + "WINDOW_POLY": "GeometryPolygon", P + "DOOR_POLY": "GeometryPolygon",
    P + "WALL_SOUTH_RING": "GeometryRing", P + "WALL_SOUTH_HOLE_WINDOW": "GeometryRing",
    P + "WALL_SOUTH_HOLE_DOOR": "GeometryRing", P + "ROOF_SOUTH_RING": "GeometryRing",
    P + "WINDOW_RING": "GeometryRing", P + "DOOR_RING": "GeometryRing",
}
# what the building gets, all under lod3_ (LOD3_FACADE_INTEGRATION.md, section 5)
SENTINELS = {
    "lod3_enriched": True, "lod3_surface_count": 2, "lod3_opening_count": 2, "lod3_window_count": 1,
    "lod3_door_count": 1, "lod3_hole_count": 2, "lod3_source": SOURCE, "lod3_confidence": 0.9,
    "lod3_method": "lod3-facade-reconstruction", "lod3_method_version": "1.0",
    "lod3_source_type": "reconstructed",
}


def _query(driver, cypher, **params) -> list[dict]:
    records = driver.execute_query(cypher, **params).records
    return [record.data() for record in records]


def _city_model(driver) -> dict:
    """Everything in the graph that is not a facade node: the nodes with labels and
    properties (without the lod3_* values of the building), the relationships with theirs."""
    nodes = {f"{'|'.join(sorted(r['labels']))} {r['p']['id']}": {k: v for k, v in r["p"].items()
                                                                  if not k.startswith("lod3_")}
             for r in _query(driver, "MATCH (n) WHERE n.id IS NOT NULL AND NOT n:Lod3Facade "
                                     "RETURN labels(n) AS labels, properties(n) AS p")}
    relationships = sorted(
        (r["a"], r["t"], r["b"], json.dumps(r["p"], sort_keys=True))
        for r in _query(driver, "MATCH (a)-[r]->(b) WHERE a.id IS NOT NULL AND b.id IS NOT NULL "
                                "AND NOT a:Lod3Facade AND NOT b:Lod3Facade "
                                "RETURN a.id AS a, type(r) AS t, b.id AS b, properties(r) AS p"))
    return {"nodes": nodes, "relationships": relationships}


def _house(driver) -> dict:
    (row,) = _query(driver, "MATCH (b:Building) RETURN properties(b) AS p")
    return row["p"]


@pytest.fixture(scope="module")
def fixture_text(data_dir) -> str:
    return (data_dir / "fixtures" / "lod3" / "fzk_haus_facade.gml").read_text(encoding="utf-8")


@pytest.fixture
def copy(fixture_text, tmp_path):
    """copy(text) writes a facade file into the folder of the test: the command writes the
    repaired file next to its input and the summary below the working directory."""
    def _copy(text=fixture_text) -> Path:
        path = tmp_path / "fzk_haus_facade.gml"
        path.write_text(text, encoding="utf-8")
        return path
    return _copy


@pytest.fixture(scope="module")
def city_model(neo4j_driver, run_module, fzk_lod2) -> dict:
    """FZK-Haus LoD2 alone in the test database, and what the graph holds then."""
    database.wipe(neo4j_driver)
    run_module("pykci.ingest_citygml", fzk_lod2)
    return {"graph": _city_model(neo4j_driver), "house": _house(neo4j_driver)}


@pytest.fixture(scope="module")
def enrich(city_model, fixture_text, run_module, tmp_path_factory):
    """enrich(*options) runs `pykci.enrich_lod3_facades <facade> --no-validate <options>`
    (validate=True leaves --no-validate out) and returns the summary it wrote. The run is
    skipped when the graph already reflects these options (a run first removes what the
    last one wrote), unless again=True."""
    folder = tmp_path_factory.mktemp("lod3")
    facade = folder / "fzk_haus_facade.gml"
    facade.write_text(fixture_text, encoding="utf-8")
    state = {"options": None}

    def _enrich(*options, validate=False, again=False):
        options = (*options, *(() if validate else ("--no-validate",)))
        if again or state["options"] != options:
            state["options"] = None
            run_module("pykci.enrich_lod3_facades", facade, *options, cwd=folder)
            text = (folder / "output" / "fzk_haus_facade_lod3_summary.md").read_text(encoding="utf-8")
            state.update(options=options, summary=json.loads(text[text.index("{"):text.rindex("}") + 1]))
        return state["summary"]
    return _enrich


# ---- stage 1: the repaired copy (the database is not used) --------------------------------------

def _names(element) -> list[str]:
    """Local names of the children of an element."""
    return [child.tag.rsplit("}", 1)[1] for child in element]


def test_fix_only_repairs_a_copy_and_leaves_the_graph_alone(copy, city_model, run_module, neo4j_driver, tmp_path):
    facade = copy()
    original, before = facade.read_bytes(), database.census(neo4j_driver)
    out = run_module("pykci.enrich_lod3_facades", facade, "--fix-only", "--no-validate", cwd=tmp_path).stdout
    assert facade.read_bytes() == original                              # the evidence is not edited
    assert database.census(neo4j_driver) == before and not (tmp_path / "output").exists()
    assert "Found 3 issue(s)" in out and "XSD re-validation" not in out
    assert "Facade inventory: 2 building(s), 3 boundary surfaces, 2 openings, 7 rings." in out

    source, fixed = ET.parse(facade).getroot(), ET.parse(tmp_path / "fzk_haus_facade_fixed.gml").getroot()
    house, other = fixed.findall("core:cityObjectMember/bldg:Building", NS)
    # (b) and (a): surfaces with openings are LoD3, and so is the shell, which may then follow them
    assert _names(house)[-3:] == ["boundedBy", "boundedBy", "lod3Solid"]
    assert {"lod3MultiSurface", "opening"} <= {e.tag.rsplit("}", 1)[1] for e in house.iter()}
    assert not [e for e in house.iter() if e.tag.rsplit("}", 1)[1] in ("lod2Solid", "lod2MultiSurface")]
    # (a) a building without openings stays LoD2: its solid moves in front of the surfaces
    assert _names(other) == ["lod2Solid", "boundedBy"] and other.find(".//bldg:lod2MultiSurface", NS) is not None
    # (c) the envelope is the extent of the positions
    assert [fixed.findtext(f"gml:boundedBy/gml:Envelope/gml:{corner}", namespaces=NS)
            for corner in ("lowerCorner", "upperCorner")] == ["457842.000 5439083.000 111.800",
                                                              "457870.000 5439088.000 118.318"]
    # nothing else changed: the same positions in the same order
    assert [p.text for p in fixed.iter("{http://www.opengis.net/gml}posList")] \
        == [p.text for p in source.iter("{http://www.opengis.net/gml}posList")]
    # a repaired file has nothing left to repair
    again = run_module("pykci.enrich_lod3_facades", tmp_path / "fzk_haus_facade_fixed.gml", "--fix-only",
                       "--no-validate", cwd=tmp_path).stdout
    assert "No schema/modelling issues detected" in again
    assert not (tmp_path / "fzk_haus_facade_fixed_fixed.gml").exists()


def test_a_validation_that_cannot_run_is_reported(copy, run_module, tmp_path):
    # the default --xsd-dir is relative to the working directory: no schemas here
    out = run_module("pykci.enrich_lod3_facades", copy(), "--fix-only", cwd=tmp_path).stdout
    assert "XSD re-validation of fixed file: skipped (schema profile not found" in out
    assert (tmp_path / "fzk_haus_facade_fixed.gml").is_file()


# ---- stage 2: the facade in the graph ---------------------------------------------------------------

def test_enrichment_writes_the_recorded_graph(enrich, neo4j_driver, data_dir):
    enrich()
    snapshot.check(data_dir / "graph" / "lod3_facade.json", database.census(neo4j_driver))


def test_facade_nodes_are_a_layer_of_their_own(enrich, neo4j_driver, fixture_text):
    enrich()
    nodes = {r["p"]["id"]: r for r in _query(neo4j_driver, "MATCH (n:Lod3Facade) RETURN labels(n) AS labels, "
                                                           "properties(n) AS p")}
    assert {i: sorted(r["labels"]) for i, r in nodes.items()} == {
        i: sorted({label, "Lod3Facade"} | ({"Entity"} if label != "GeometryRing" else set()))
        for i, label in NODES.items()}
    assert {r["p"]["source"] for r in nodes.values()} == {SOURCE}

    def props(suffix):
        return {k: v for k, v in nodes[P + suffix]["p"].items() if k not in ("bbox", "gtype", "bbox_wkt")}
    assert props("WALL_SOUTH") == {"id": P + "WALL_SOUTH", "surface_type": "WallSurface", "lod": "3",
                                   "multi_surface_id": "FZK_L3_WALL_SOUTH_MS", "source": SOURCE,
                                   "source_type": "reconstructed"}
    assert props("ROOF_SOUTH")["surface_type"] == "RoofSurface"
    assert props("WINDOW") == {"id": P + "WINDOW", "opening_type": "Window", "source": SOURCE,
                               "source_type": "reconstructed"}
    assert props("DOOR")["opening_type"] == "Door"
    # every ring holds the positions as the file writes them, the holes as interior rings
    rings = {P + ring.get(GML_ID).removeprefix("FZK_L3_"): ring.findtext("gml:posList", namespaces=NS)
             for ring in ET.fromstring(fixture_text.encode()).iter("{http://www.opengis.net/gml}LinearRing")
             if ring.get(GML_ID).startswith("FZK_L3_")}
    assert len(rings) == 6
    for ring_id, positions in rings.items():
        assert props(ring_id.removeprefix(P)) == {
            "id": ring_id, "pos_list": positions, "srs_dimension": 3, "source": SOURCE,
            "ring_type": "interior" if "HOLE" in ring_id else "exterior"}


def test_facade_hangs_on_the_building_by_an_edge_of_its_own(enrich, neo4j_driver, city_model):
    enrich()
    facade = {"source": SOURCE, "source_type": "reconstructed", "method": "lod3-facade-reconstruction",
              "method_version": "1.0", "confidence": 0.9, "lod": "3"}
    member = {"order": 0, "via_xlink": False, "orientation": "+"}
    assert sorted(((r["a"], r["t"], r["b"], r["p"]) for r in _query(
        neo4j_driver, "MATCH (a)-[r]->(b:Lod3Facade) WHERE a.id IS NOT NULL "
                      "RETURN a.id AS a, type(r) AS t, b.id AS b, properties(r) AS p")),
        key=lambda e: e[:3]) == sorted([
        (city_model["house"]["id"], "HAS_LOD3_FACADE", P + "WALL_SOUTH", {"order": 0, **facade}),
        (city_model["house"]["id"], "HAS_LOD3_FACADE", P + "ROOF_SOUTH", {"order": 1, **facade}),
        # below the surface: the relationships of the CityGML geometry
        (P + "WALL_SOUTH", "HAS_POLYGON", P + "WALL_SOUTH_POLY", member),
        (P + "ROOF_SOUTH", "HAS_POLYGON", P + "ROOF_SOUTH_POLY", member),
        (P + "WALL_SOUTH_POLY", "HAS_EXTERIOR_RING", P + "WALL_SOUTH_RING", {}),
        (P + "WALL_SOUTH_POLY", "HAS_INTERIOR_RING", P + "WALL_SOUTH_HOLE_WINDOW", {"ring_index": 0}),
        (P + "WALL_SOUTH_POLY", "HAS_INTERIOR_RING", P + "WALL_SOUTH_HOLE_DOOR", {"ring_index": 1}),
        (P + "ROOF_SOUTH_POLY", "HAS_EXTERIOR_RING", P + "ROOF_SOUTH_RING", {}),
        (P + "WALL_SOUTH", "HAS_OPENING", P + "WINDOW", {"order": 0}),
        (P + "WALL_SOUTH", "HAS_OPENING", P + "DOOR", {"order": 1}),
        (P + "WINDOW", "HAS_LOD_SURFACE", P + "WINDOW_MS", {"lod": "3"}),
        (P + "DOOR", "HAS_LOD_SURFACE", P + "DOOR_MS", {"lod": "3"}),
        (P + "WINDOW_MS", "HAS_SURFACE_MEMBER", P + "WINDOW_POLY", member),
        (P + "DOOR_MS", "HAS_SURFACE_MEMBER", P + "DOOR_POLY", member),
        (P + "WINDOW_POLY", "HAS_EXTERIOR_RING", P + "WINDOW_RING", {}),
        (P + "DOOR_POLY", "HAS_EXTERIOR_RING", P + "DOOR_RING", {}),
    ], key=lambda e: e[:3])


def test_values_on_the_building_are_a_reconstructed_layer(enrich, neo4j_driver, city_model):
    enrich()
    house = _house(neo4j_driver)
    added = {k: v for k, v in house.items() if k not in city_model["house"]}
    assert added == SENTINELS
    for key in added:
        assert provenance.property_provenance(key) == provenance.RECONSTRUCTED
        assert provenance.layer_of(key, house) == provenance.RECONSTRUCTED


def test_the_city_model_is_untouched(enrich, neo4j_driver, city_model):
    """Every node and relationship of the LoD2 model as before: its surfaces and rings, and
    the values of the building (the facade file states another height, function and storeys)."""
    enrich()
    assert _city_model(neo4j_driver) == city_model["graph"]
    house = _house(neo4j_driver)
    assert (house["measured_height"], house["function_code"], house["storeys_above_ground"]) == (6.52, "1000", 2)
    assert _query(neo4j_driver, "MATCH (:Building)-[:HAS_BOUNDARY]->(s) RETURN count(s) AS n, "
                                "count(CASE WHEN s:Lod3Facade THEN 1 END) AS facade") == [{"n": 7, "facade": 0}]


def test_facades_have_their_own_rtree_layer(enrich, neo4j_driver, city_model):
    enrich()
    def found(layer):
        return sorted(r["id"] for r in _query(
            neo4j_driver, "CALL spatial.intersects($layer, $wkt) YIELD node RETURN node.id AS id", layer=layer,
            wkt="POLYGON((457700 5438900, 458000 5438900, 458000 5439300, 457700 5439300, 457700 5438900))"))
    assert found("lod3_features") == sorted([P + "ROOF_SOUTH", P + "WALL_SOUTH"])
    assert found("features") == [city_model["house"]["id"]]


def test_a_building_missing_from_the_graph_is_reported_not_created(enrich, neo4j_driver):
    summary = enrich()
    assert summary["enrichment"] == {"buildings_matched": 1, "buildings_not_found": 1, "coverage_rate": 0.5,
                                     "not_found_ids": [ABSENT], "spatial_indexed_surfaces": 2}
    assert summary["facade_elements"] == {
        "buildings_in_lod3_file": 2, "boundary_surfaces_written": 2, "openings_written": 2, "windows": 1,
        "doors": 1, "wall_holes": 2, "geometry_polygons_written": 4, "geometry_rings_written": 6}
    assert _query(neo4j_driver, "MATCH (n) WHERE n.id = $absent OR n.id CONTAINS 'OTHER_' RETURN count(n) AS n",
                  absent=ABSENT) == [{"n": 0}]
    # the summary also says what was repaired, and that --no-validate left the check out
    assert summary["fix"]["issue_count"] == 3 and summary["fix"]["xsd_validation_after_fix"] == {"ran": False}
    assert (summary["source"], summary["source_type"], summary["confidence"]) == (SOURCE, "reconstructed", 0.9)


@pytest.mark.slow
def test_the_repaired_file_is_validated_before_it_is_used(enrich, repo, neo4j_driver, data_dir):
    """Without --no-validate the repaired file is checked against the CityGML 2.0 schemas of
    the repository and the verdict is part of the summary. The schemas import GML 3.1.1 from
    the OGC server, so the check needs the network."""
    summary = enrich("--xsd-dir", repo / "citygml-model" / "xsd_citygml2", validate=True)
    verdict = summary["fix"]["xsd_validation_after_fix"]
    if not verdict["ran"]:
        pytest.skip(f"the XSD validation could not run: {verdict['note']}")
    assert verdict == {"ran": True, "valid": True, "errors": 0, "note": ""}
    snapshot.check(data_dir / "graph" / "lod3_facade.json", database.census(neo4j_driver))


def test_info_reports_the_reconstructed_layer(enrich, run_module):
    enrich()
    info = json.loads(run_module("pykci.cli", "info").stdout)
    assert info["layers"] == {"authoritative": True, "crowd_sourced": False,
                              "ml_predicted": False, "reconstructed": True}
    assert info["spatial_layers"] == ["features", "lod3_features"]


@pytest.mark.slow
def test_tiles_draw_the_facade(enrich, run_module, tmp_path):
    enrich()

    def build(*options):
        out = tmp_path / ("tiles" + "".join(options))
        run_module("pykci.build_3dtiles", "--source", CITY_MODEL, "--tiled", *options, "--out", out)
        return out, json.loads((out / "tileset.json").read_text(encoding="utf-8"))["extras"]["pykci"]

    out, meta = build()
    assert (meta["features"], meta["lod3_features"]) == (1, 1)
    # the lod3_* values are attributes of the feature, labelled with their layer
    schema = json.loads((out / "feature_schema.json").read_text(encoding="utf-8"))
    assert {p["id"] for p in schema["properties"] if p["provenance"] == provenance.RECONSTRUCTED} == set(SENTINELS)
    # --no-lod3 keeps the LoD2 shell
    assert build("--no-lod3")[1]["lod3_features"] == 0


# ---- running it again, and with other options ---------------------------------------------------

@pytest.mark.slow
def test_second_run_changes_nothing(enrich, neo4j_driver):
    enrich()
    first, house = database.census(neo4j_driver), _house(neo4j_driver)
    enrich(again=True)
    assert database.census(neo4j_driver) == first and _house(neo4j_driver) == house


@pytest.mark.slow
def test_confidence_is_given_on_the_command_line(enrich, neo4j_driver, data_dir):
    summary = enrich("--confidence", "0.55")
    assert summary["confidence"] == 0.55 and _house(neo4j_driver)["lod3_confidence"] == 0.55
    assert _query(neo4j_driver, "MATCH ()-[r:HAS_LOD3_FACADE]->() RETURN collect(DISTINCT r.confidence) AS c") \
        == [{"c": [0.55]}]
    # nothing else differs from the run with the default
    snapshot.check(data_dir / "graph" / "lod3_facade.json", database.census(neo4j_driver))


@pytest.mark.slow
def test_dataset_is_found_or_named(enrich, copy, run_module, neo4j_driver, tmp_path):
    summary = enrich("--dataset", DATASET)
    assert summary["dataset"] == CITY_MODEL and summary["enrichment"]["buildings_matched"] == 1
    # an unknown name stops the command before it changes the graph
    before = database.census(neo4j_driver)
    failed = run_module("pykci.enrich_lod3_facades", copy(), "--no-validate", "--dataset", "no-such-dataset",
                        cwd=tmp_path, ok=False)
    assert failed.returncode != 0 and "Dataset 'no-such-dataset' not found" in failed.stderr
    assert database.census(neo4j_driver) == before


# ---- known defects (also in the code before the package) ---------------------------------------

@pytest.mark.xfail(strict=True, reason="surfaces with openings that are tagged lod2MultiSurface are renamed in memory "
                   "but not counted as an issue: when that is the only defect, no repaired file is written "
                   "(LOD3_FACADE_INTEGRATION.md, section 3, defect b)")
def test_a_file_whose_only_defect_is_the_lod_tag_is_repaired_too(copy, fixture_text, run_module, tmp_path):
    text = re.sub(r"\s*<bldg:lod2Solid>.*?</bldg:lod2Solid>", "", fixture_text, flags=re.S)
    text = text.replace("457000 5439000 0", "457842 5439083 111.8").replace("457003 5439014 3", "457870 5439088 118.4")
    run_module("pykci.enrich_lod3_facades", copy(text), "--fix-only", "--no-validate", cwd=tmp_path)
    fixed = tmp_path / "fzk_haus_facade_fixed.gml"
    assert fixed.is_file() and "lod3MultiSurface>" in fixed.read_text(encoding="utf-8")


def _door_ring_as_gml_pos(text: str) -> str:
    """The ring of the door written as one gml:pos per position instead of a gml:posList."""
    door = "457850 5439083 111.9 457850 5439083 113.9 457849 5439083 113.9 457849 5439083 111.9 457850 5439083 111.9"
    values = door.split()
    return text.replace(f'<gml:posList srsDimension="3">{door}</gml:posList>', "".join(
        f"<gml:pos>{' '.join(values[i:i + 3])}</gml:pos>" for i in range(0, len(values), 3)))


def _roof_in_a_composite_surface(text: str) -> str:
    """The polygon of the roof as member of a gml:CompositeSurface inside the multi-surface."""
    return re.sub(r'<gml:Polygon gml:id="FZK_L3_ROOF_SOUTH_POLY">.*?</gml:Polygon>',
                  lambda polygon: "<gml:CompositeSurface><gml:surfaceMember>" + polygon.group(0)
                                  + "</gml:surfaceMember></gml:CompositeSurface>", text, flags=re.S)


@pytest.mark.xfail(strict=True, reason="only a gml:Polygon directly below the multi-surface and only gml:posList are "
                   "read: a ring of gml:pos elements and a polygon inside a gml:CompositeSurface (the FZK-Haus "
                   "LoD3 model of the repository writes its rings and walls so) are skipped without a message")
@pytest.mark.parametrize("rewrite", [_door_ring_as_gml_pos, _roof_in_a_composite_surface])
def test_other_valid_ways_to_write_the_geometry_are_read(rewrite, copy, fixture_text, run_module, tmp_path):
    text = rewrite(fixture_text)
    assert text != fixture_text
    out = run_module("pykci.enrich_lod3_facades", copy(text), "--fix-only", "--no-validate", cwd=tmp_path).stdout
    assert "Facade inventory: 2 building(s), 3 boundary surfaces, 2 openings, 7 rings." in out


@pytest.mark.xfail(strict=True, reason="a polygon, ring or multi-surface without gml:id gets a node id made of the "
                   "memory address of a temporary object (id()): it differs from run to run and two elements "
                   "of one run can get the same one, which merges them into one node")
def test_geometry_without_gml_id_gets_the_same_node_id_in_every_run(fixture_text):
    """No database: the ids are read off the rows the command would write. The elements of
    both readings are kept alive, so that an equal id cannot come from a reused address."""
    from lxml import etree

    from pykci import enrich_lod3_facades as lod3

    text = re.sub(r'(<gml:(?:Polygon|LinearRing|MultiSurface)) gml:id="[^"]*"', r"\1", fixture_text).encode()
    alive = []

    def node_ids():
        tree = etree.ElementTree(etree.fromstring(text))
        alive.append(list(tree.iter()))
        batch, _ = lod3.parse_facades(tree, SOURCE)
        return sorted(row["id"] for rows in ("polys", "rings", "multisurfaces") for row in batch.d[rows])

    assert node_ids() == node_ids()
