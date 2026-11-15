"""The ML-predicted layer: pykci.ingest_roof_materials attaches predictions to buildings by
gml:id, in its own namespace, without touching an authoritative value, and a re-run replaces
exactly that layer. pykci.export_roofmat_footprints draws the result."""

import json

import pytest

from pykci import provenance
from pykci.testing import database

DICTIONARY = "0;concrete\n1;metal\n2;glass\n3;roof_tiles\n4;tar_paper\n"


@pytest.fixture
def building(empty_graph, run_module, fzk_lod2):
    run_module("pykci.ingest_citygml", fzk_lod2)
    with empty_graph.session() as sess:
        return sess.run("MATCH (b:Building) RETURN b.id AS id").single()["id"]


def _predictions(tmp_path, building_id, materials, coverage, name="roofs"):
    (tmp_path / "roof_materials.csv").write_text(DICTIONARY, encoding="utf-8")
    path = tmp_path / f"{name}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None,
         "properties": {"gml_id": building_id, "predicted_roof_materials": materials, "material_cov": coverage}},
        {"type": "Feature", "geometry": None,
         "properties": {"gml_id": "no-such-building", "predicted_roof_materials": ["0"], "material_cov": [1.0]}},
    ]}), encoding="utf-8")
    return path


def _props(driver, building_id) -> dict:
    with driver.session() as sess:
        return dict(sess.run("MATCH (b:Building {id: $id}) RETURN properties(b) AS p", id=building_id).single()["p"])


def test_predictions_are_a_namespaced_layer(empty_graph, building, run_module, tmp_path):
    before = _props(empty_graph, building)
    run_module("pykci.ingest_roof_materials", _predictions(tmp_path, building, ["4", "1"], [0.4323, 0.5677]))
    after = _props(empty_graph, building)

    added = {k: v for k, v in after.items() if k not in before}
    assert added == {
        "predictedroofmaterial": "metal",                       # the material with the largest coverage
        "predicted_roof_material_0": "metal",
        "predicted_roof_material_0_coverage": 0.5677,
        "predicted_roof_material_1": "tar_paper",
        "predicted_roof_material_1_coverage": 0.4323,
        "predicted_roof_material_count": 2,
        "predicted_roof_material_source": "ml:roof_material:roofs",
    }
    assert {k: after[k] for k in before} == before              # no authoritative value was touched
    assert {provenance.property_provenance(k) for k in added} == {provenance.ML_PREDICTED}


def test_rerun_replaces_the_layer(empty_graph, building, run_module, tmp_path):
    run_module("pykci.ingest_roof_materials", _predictions(tmp_path, building, ["4", "1"], [0.4, 0.6]))
    first = database.census(empty_graph)
    run_module("pykci.ingest_roof_materials", _predictions(tmp_path, building, ["4", "1"], [0.4, 0.6]))
    assert database.census(empty_graph) == first
    # fewer materials than before: the keys of the second material are gone, not left stale
    run_module("pykci.ingest_roof_materials", _predictions(tmp_path, building, ["2"], [1.0]))
    props = _props(empty_graph, building)
    assert props["predictedroofmaterial"] == "glass" and props["predicted_roof_material_count"] == 1
    assert "predicted_roof_material_1" not in props and "predicted_roof_material_1_coverage" not in props


def test_info_reports_the_layer(empty_graph, building, run_module, tmp_path):
    assert json.loads(run_module("pykci.cli", "info").stdout)["layers"]["ml_predicted"] is False
    run_module("pykci.ingest_roof_materials", _predictions(tmp_path, building, ["3"], [1.0]))
    assert json.loads(run_module("pykci.cli", "info").stdout)["layers"]["ml_predicted"] is True


def test_footprint_map_shows_the_material(empty_graph, building, run_module, tmp_path):
    run_module("pykci.ingest_roof_materials", _predictions(tmp_path, building, ["3"], [1.0]))
    out = tmp_path / "roofmap.html"
    run_module("pykci.export_roofmat_footprints", "--output", out)
    html = out.read_text(encoding="utf-8")
    assert building in html and "roof_tiles" in html
