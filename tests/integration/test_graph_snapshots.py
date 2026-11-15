"""What the ingest writes into the graph, compared with the recorded graph of each fixture.

Each case ingests one of the small models of the repository into the empty test database
and compares the census of the graph (nodes per label set, relationships per type, property
keys per label and per relationship type, indexes, R-tree layers) with tests/data/graph/<case>.json.
The snapshots were recorded with the code before pykci became a package, so they also prove
that the restructuring changed nothing.

Any change of the schema shows up here: a new label, property, relationship or index fails
the case until the snapshot is recorded again (PYKCI_UPDATE_SNAPSHOTS=1) and reviewed. See
tests/data/README.md.
"""

import pytest

from pykci.testing import database, snapshot

# pykci.ingest_citygml is the module under test: every case runs it as a command
FZK = "input/citygml/FZK-Haus/FZK-Haus-{}-KIT-IAI-KHH-B36-V1.gml"
FIX = "eval/fixtures/"

# case → the ingest calls that build it: (file relative to the repository, extra arguments…)
CASES = {
    # CityGML 2.0, one building at every level of detail (LoD4: rooms, furniture, openings)
    "fzk_lod0": [(FZK.format("LoD0"),)],
    "fzk_lod1": [(FZK.format("LoD1"),)],
    "fzk_lod2": [(FZK.format("LoD2"),)],
    "fzk_lod3": [(FZK.format("LoD3"),)],
    "fzk_lod4": [(FZK.format("LoD4"),)],
    # CityGML 2.0 elements beyond buildings (the generic collectors)
    "new_elements": [(FIX + "new_elements_test.gml",)],
    # CityGML 3.0: every element type, dynamizers, and two tiles merged into one dataset
    "citygml3_kitchen_sink": [(FIX + "citygml3/kitchen_sink.gml",)],
    "citygml3_dynamizer": [(FIX + "citygml3/dynamizer.gml", "--rtree-layer", "transport")],
    "citygml3_tiles_merged": [
        (FIX + "citygml3/tile_b.gml", "--dataset-name", "transport_test", "--rtree-layer", "transport"),
        (FIX + "citygml3/tile_a.gml", "--dataset-name", "transport_test", "--rtree-layer", "transport"),
    ],
}
SLOW_CASES = {
    # a LoD3 railway scene: 131,721 nodes, the implicit geometries and the transportation module
    "railway_lod3": [("input/citygml/Railway_Scene_LoD3/Railway_Scene_LoD3.gml",)],
}


def _ingest(run_module, repo, calls):
    for path, *extra in calls:
        run_module("pykci.ingest_citygml", repo / path, *extra)


@pytest.mark.parametrize("name", list(CASES))
def test_ingest_writes_the_recorded_graph(name, empty_graph, run_module, repo, data_dir):
    _ingest(run_module, repo, CASES[name])
    snapshot.check(data_dir / "graph" / f"{name}.json", database.census(empty_graph))


@pytest.mark.parametrize("name", list(CASES))
def test_second_ingest_changes_nothing(name, empty_graph, run_module, repo):
    _ingest(run_module, repo, CASES[name])
    first = database.census(empty_graph)
    _ingest(run_module, repo, CASES[name])
    assert database.census(empty_graph) == first


@pytest.mark.slow
@pytest.mark.parametrize("name", list(SLOW_CASES))
def test_ingest_writes_the_recorded_graph_large(name, empty_graph, run_module, repo, data_dir):
    _ingest(run_module, repo, SLOW_CASES[name])
    first = database.census(empty_graph)
    snapshot.check(data_dir / "graph" / f"{name}.json", first)
    _ingest(run_module, repo, SLOW_CASES[name])
    assert database.census(empty_graph) == first


def test_every_case_has_its_snapshot(data_dir):
    recorded = {p.stem for p in (data_dir / "graph").glob("*.json")}
    assert set(CASES) | set(SLOW_CASES) <= recorded
