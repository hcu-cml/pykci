"""The graph schema in the code and in the documents is the same schema.

Every node label and relationship type that a Cypher statement of the package names must
appear in the graph documentation. Extending the schema (a new label, a new relationship)
therefore fails this test until GRAPH.md, or the document of the feature, describes it.

Labels the ingest builds from tables at run time are not names in the source; the schema
snapshot of the integration tests (tests/integration/test_schema_snapshot.py) covers those."""

import pytest

from pykci.testing import cypher, docs
from pykci.testing.clones import python_files

# GRAPH.md is the reference; the feature documents describe what their step adds to it
DOCUMENTS = ("GRAPH.md", "CITYGML3.md", "OSM_INTEGRATION.md", "LOD3_FACADE_INTEGRATION.md",
             "ROOF_MATERIAL_INGESTION.md")


@pytest.fixture(scope="module")
def documented(repo) -> set[str]:
    return docs.words(repo / name for name in DOCUMENTS)


@pytest.fixture(scope="module")
def in_code(repo) -> dict:
    return cypher.scan(python_files(repo / "src" / "pykci", exclude=("testing/",)))


def test_scanner_finds_the_core_schema(in_code):
    """Guards the test itself: an empty scan would let everything pass."""
    assert {"Building", "Dataset", "BoundarySurface", "GeometryPolygon", "OsmFeature"} <= set(in_code["labels"])
    assert {"HAS_BOUNDARY", "PART_OF", "ENRICHED_BY", "HAS_POLYGON"} <= set(in_code["relationships"])


@pytest.mark.parametrize("kind", ["labels", "relationships"])
def test_schema_in_the_code_is_documented(in_code, documented, kind):
    missing = docs.undocumented(in_code[kind], documented)
    assert not missing, f"{kind} in the Cypher of the package that no graph document mentions: {missing}"


def test_osm_labels_in_the_documents_are_the_ones_the_fusion_writes(repo):
    """pykci.ingest_osm sets the label of an OSM feature from its KIND_LABEL table at run time,
    which the Cypher scan cannot see. The table and the documents must name the same labels:
    none undocumented, and none documented that nothing writes."""
    from pykci.ingest_osm import KIND_LABEL

    written = {label for label, _, _ in KIND_LABEL.values()}
    named = {word for word in docs.words([repo / "OSM_INTEGRATION.md", repo / "GRAPH.md"])
             if word.startswith("Osm") and word[3:4].isupper()}
    assert named == written
