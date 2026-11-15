"""The pipeline after the ingest, on one small graph (FZK-Haus LoD2, one building): the
graph summary, CityGML export and its round trip, 3D Tiles, the viewers. Every step is
run the way a user runs it, as a command against the test database."""

import json
import xml.etree.ElementTree as ET

import pytest

from pykci import provenance
from pykci.testing import database, glb, snapshot

DATASET = "fzk_haus_lod2_kit_iai_khh_b36_v1"
SOURCE = "FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml"


@pytest.fixture(scope="module")
def graph(neo4j_driver, run_module, fzk_lod2):
    """The test database holding FZK-Haus LoD2; the tests of this module only read it."""
    database.wipe(neo4j_driver)
    run_module("pykci.ingest_citygml", fzk_lod2)
    return neo4j_driver


@pytest.fixture(scope="module")
def building_id(graph) -> str:
    with graph.session() as sess:
        return sess.run("MATCH (b:Building) RETURN b.id AS id").single()["id"]


# ---- pykci.cli: info and inspect ------------------------------------------------------------

def test_info_describes_the_graph(graph, run_module):
    info = json.loads(run_module("pykci.cli", "info").stdout)
    assert info["counts"]["buildings"] == 1 and info["counts"]["osm_features"] == 0
    assert info["layers"] == {"authoritative": True, "crowd_sourced": False,
                              "ml_predicted": False, "reconstructed": False}
    assert info["spatial_layers"] == ["features"]
    (dataset,) = info["datasets"]
    assert dataset["id"] == DATASET and dataset["source"] == SOURCE and dataset["buildings"] == 1
    west, south, east, north = dataset["extent_wgs84"]
    assert 8.4 < west <= east < 8.5 and 49.0 < south <= north < 49.2     # north of Karlsruhe


def test_inspect_reads_a_file_without_the_database(fzk_lod2, tmp_path):
    from pykci.testing.plugin import run
    import sys

    result = run([sys.executable, "-m", "pykci.cli", "inspect", fzk_lod2], cwd=tmp_path)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["buildings"] == 1 and report["totals"] == {"Building": 1}
    (file,) = report["files"]
    assert file["citygml_version"] == "2.0" and file["duplicate_ids"] == 0 and file["errors"] == []
    assert 2 in file["lods"]


# ---- pykci.export_citygml --------------------------------------------------------------------

def _root(path):
    return ET.parse(path).getroot()


@pytest.mark.parametrize("version, namespace", [("2", "http://www.opengis.net/citygml/2.0"),
                                                ("3", "http://www.opengis.net/citygml/3.0")])
def test_export_writes_citygml(graph, run_module, building_id, tmp_path, version, namespace):
    out = tmp_path / f"export_v{version}.gml"
    run_module("pykci.export_citygml", "--dataset", DATASET, "--version", version, "--out", out)
    root = _root(out)
    assert root.tag == f"{{{namespace}}}CityModel"
    assert building_id in out.read_text(encoding="utf-8")


def test_export_by_ids(graph, run_module, building_id, tmp_path):
    out = tmp_path / "one.gml"
    run_module("pykci.export_citygml", "--ids", building_id, "--out", out)
    assert building_id in out.read_text(encoding="utf-8")


def test_export_by_source_file(graph, run_module, building_id, tmp_path):
    """The file the Dataset was ingested from, rebuilt from the graph: its features, the
    file's stem as the CityModel id, and the loss report next to it. (Two tiles of one Dataset,
    rebuilt and ingested again: eval/citygml3_tests.py T14, run by test_eval_harness.py.)"""
    out = tmp_path / "rebuilt.gml"
    run_module("pykci.export_citygml", "--dataset", DATASET, "--version", "3", "--source-file", SOURCE,
               "--out", out)
    root = _root(out)
    assert root.get("{http://www.opengis.net/gml/3.2}id") == SOURCE.removesuffix(".gml")
    assert building_id in out.read_text(encoding="utf-8")
    assert json.loads(out.with_suffix(".loss.json").read_text(encoding="utf-8"))["source_file"] == SOURCE
    # a file the Dataset does not hold gives an empty CityModel
    other = tmp_path / "other.gml"
    run_module("pykci.export_citygml", "--dataset", DATASET, "--version", "3", "--source-file", "other.gml",
               "--out", other)
    assert building_id not in other.read_text(encoding="utf-8")


def test_export_each_source_file_into_a_folder(graph, run_module, building_id, tmp_path):
    out = tmp_path / "tiles"
    run_module("pykci.export_citygml", "--dataset", DATASET, "--version", "3", "--each-source-file",
               "--out", out)
    assert sorted(p.name for p in out.iterdir()) == [SOURCE, SOURCE.replace(".gml", ".loss.json")]
    assert building_id in (out / SOURCE).read_text(encoding="utf-8")


@pytest.mark.parametrize("arguments, message", [
    (("--source-file", SOURCE, "--bbox", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"), "leave out --ids, --ids-file and --bbox"),
    (("--each-source-file", "--version", "3"), "--each-source-file needs --out <directory>"),
    (("--source-file", SOURCE, "--version", "2"), "export by source file needs the strict writer"),
])
def test_export_by_source_file_refusals(graph, run_module, tmp_path, arguments, message):
    result = run_module("pykci.export_citygml", "--dataset", DATASET, *arguments, cwd=tmp_path, ok=False)
    assert result.returncode != 0 and message in result.stderr
    assert not (tmp_path / "output").exists()


def test_export_round_trip_gives_the_same_graph(graph, run_module, neo4j_driver, tmp_path, data_dir):
    """Export, wipe, ingest the export: the same nodes and relationships as the recorded
    graph of the source file (the Dataset is named after the exported file, so only counts
    are compared)."""
    out = tmp_path / "roundtrip.gml"
    run_module("pykci.export_citygml", "--dataset", DATASET, "--version", "2", "--out", out)
    try:
        database.wipe(neo4j_driver)
        run_module("pykci.ingest_citygml", out)
        again = database.census(neo4j_driver)
    finally:                                    # the other tests of the module need the source graph back
        database.wipe(neo4j_driver)
        run_module("pykci.ingest_citygml", graph_source(data_dir))
    recorded = json.loads((data_dir / "graph" / "fzk_lod2.json").read_text(encoding="utf-8"))
    assert again["labels"] == recorded["labels"]
    assert again["relationships"] == recorded["relationships"]


def graph_source(data_dir):
    return data_dir.parents[1] / "input" / "citygml" / "FZK-Haus" / SOURCE


# ---- pykci.build_3dtiles ---------------------------------------------------------------------

def test_single_tileset(graph, run_module, tmp_path):
    out = tmp_path / "tiles"
    run_module("pykci.build_3dtiles", "--source", SOURCE, "--out", out)
    tileset = json.loads((out / "tileset.json").read_text(encoding="utf-8"))
    assert tileset["asset"]["version"] in ("1.0", "1.1")
    gltf, _ = glb.chunks((out / tileset["root"]["content"]["uri"]).read_bytes())
    assert gltf["meshes"] and gltf["accessors"]


def test_tiled_tileset_with_attributes(graph, run_module, building_id, tmp_path, data_dir):
    out = tmp_path / "tiled"
    run_module("pykci.build_3dtiles", "--source", SOURCE, "--tiled", "--out", out)
    tileset = json.loads((out / "tileset.json").read_text(encoding="utf-8"))
    assert tileset["asset"]["version"] == "1.1"
    meta = tileset["extras"]["pykci"]
    assert meta["dataset"] == SOURCE and meta["features"] == 1 and meta["horizontal_crs"] == "EPSG:25832"
    assert isinstance(meta["geoid_offset_m"], (int, float))
    assert meta["feature_schema"] == "feature_schema.json"
    gltf, _ = glb.chunks((out / "tiles" / "r.glb").read_bytes())
    assert {"EXT_mesh_features", "EXT_structural_metadata"} <= set(gltf["extensionsUsed"])
    # every attribute of the schema names its provenance layer, and the layers are pykci's
    schema = json.loads((out / "feature_schema.json").read_text(encoding="utf-8"))
    snapshot.check(data_dir / "tiles" / "fzk_lod2_feature_schema.json", schema)
    text = json.dumps(schema)
    assert all(layer in provenance.LAYERS for layer in _values(schema, "provenance")), text[:300]


def _values(node, key):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, str):
                yield v
            else:
                yield from _values(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _values(v, key)


# ---- the viewers ----------------------------------------------------------------------------

def test_graph_html(graph, run_module, building_id, tmp_path):
    out = tmp_path / "graph.html"
    run_module("pykci.export_graph_html", "--output", out)
    html = out.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html") and building_id in html


def test_graph_3d_viewer(graph, run_module, building_id, tmp_path):
    out = tmp_path / "graph_3d.html"
    run_module("pykci.visualize_graph_3d", "--output", out)
    assert building_id in out.read_text(encoding="utf-8")


def test_graph_point_cloud_tiles(graph, run_module, tmp_path):
    out = tmp_path / "graph_tiles"
    run_module("pykci.build_graph_pointcloud", "--output", out)
    tileset = json.loads((out / "tileset.json").read_text(encoding="utf-8"))
    assert tileset["root"]
    assert (out / "viewer.html").is_file() and (out / "serve.py").is_file()
    assert list(out.rglob("*.pnts")), "no point-cloud tile written"


def test_buildings_3d_viewer(graph, run_module, building_id, tmp_path):
    ids = tmp_path / "ids.csv"
    ids.write_text(f"gmlid\n{building_id}\n", encoding="utf-8")
    out = tmp_path / "buildings.html"
    run_module("pykci.visualize_buildings_3d", ids, "--output", out)
    assert building_id in out.read_text(encoding="utf-8")
