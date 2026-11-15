# Testing pykci

The test suite has to pass before a change is merged, and it has to grow with the code: a new
command, module, label or relationship fails a test until it is tested and documented. The
checks live in `pykci.testing`.

```bash
pip install -e ".[test]"
pytest -m "not neo4j"          # everything that needs no database (about a minute)
pytest                         # with a test database: the whole suite
pytest -m "not slow"           # leave out what takes longer than a few seconds
```

## What is tested where

| Folder | Needs | What |
| --- | --- | --- |
| `tests/unit` | nothing | Functions with inputs and expected outputs: provenance rules, CRS names, geometry, addresses, glTF writing, the file tools (footprints, 1.0 to 2.0 upgrade, XSD validation), the test library itself |
| `tests/contract` | nothing | Rules of the repository, see below |
| `tests/integration` | a throwaway Neo4j | The pipeline as a user runs it: ingest of every fixture compared with its recorded graph, re-ingest, export and round trip, 3D Tiles, the viewers, the ML layer, OpenStreetMap fusion, LoD3 facades, and the codec harness `eval/citygml3_tests.py` |
| `tests/data` | | Recorded results (snapshots) the tests compare with |

## The rules (`tests/contract`)

| Test | Rule | It fails when |
| --- | --- | --- |
| `test_commands` | Every command of `pykci.cli.COMMANDS` is a module of the package (or a research script in `eval/`), answers `--help`, and is listed in `CLI.md` and in the docstring of `pykci/cli.py` | a command is added without its documentation, or its module does not import |
| `test_entry_points` | pykci runs on its own: installed (`pykci`, `python -m pykci.<module>`) and from a plain checkout without installing (`python ingest_citygml.py …`); the old module names still import | a script stops working standalone, or code is added at the repository root instead of in `src/pykci` |
| `test_package` | Importing a module only defines names (no output, no files, no connection), the small modules other programs read stay free of the heavy dependencies, and no name is used without being defined | a module does work at import time, `pykci.provenance` starts importing lxml, or code was moved and a caller lost its import |
| `test_connection` | The database is reached through `pykci.runtime` only | a module names a Bolt address or a password, or opens its own driver |
| `test_no_repeated_code` | No function body or table exists twice | code is copied instead of imported |
| `test_schema_is_documented` | Every node label and relationship type in the Cypher of the package is described in `GRAPH.md` or the document of its feature | the schema is extended without writing it down |
| `test_every_module_is_tested` | Every module of the package is named by a test, and every recorded result is compared by one | a module is added without a test, or a test is deleted and its snapshot left behind |
| `test_docker_images` | The images in `docker/` copy files that exist and install what the code they run imports | the ingest gains a dependency, or a script needs a file the image does not copy |

## The test database

Integration tests ingest, export and delete, so they never use the connection of the
pipeline. The database is named by its own variables, port 7687 is refused, and a database
that holds nodes is only accepted when the tests created it (it then carries a
`(:PykciTestDatabase)` node). See `src/pykci/testing/database.py`.

```bash
docker create --name pykci-test-neo4j -p 127.0.0.1:17687:7687 \
    -e NEO4J_AUTH=neo4j/pykci-test-only \
    -e "NEO4J_dbms_security_procedures_unrestricted=apoc.*,spatial.*" \
    -e "NEO4J_dbms_security_procedures_allowlist=apoc.*,spatial.*" neo4j:2025.10.1
docker cp docker/plugins/apoc-2025.10.1-core.jar pykci-test-neo4j:/var/lib/neo4j/plugins/
docker cp docker/plugins/neo4j-spatial-server-plugin-2025.10.0-with-dependencies.jar \
    pykci-test-neo4j:/var/lib/neo4j/plugins/
docker start pykci-test-neo4j

export PYKCI_TEST_NEO4J_URI=bolt://localhost:17687
export PYKCI_TEST_NEO4J_PASSWORD=pykci-test-only
pytest
```

Without `PYKCI_TEST_NEO4J_URI` the integration tests are skipped. A run that must not pass by
skipping them (continuous integration, a release) sets `PYKCI_TEST_REQUIRE_NEO4J=1`: a missing
database is then a failure.

The XSD checks (the validator tests and the codec harness) need the OGC schema bundle once:
`pykci validate-citygml --setup`. They are skipped until it is there.

The OpenStreetMap tests need GDAL's `ogr2ogr`. They look for it on `PATH`, in the file named by
`OGR2OGR`, and in the usual install folders (QGIS, OSGeo4W, conda), and are skipped when there
is none. An `ogr2ogr` that cannot find its `osmconf.ini` needs `GDAL_DATA`; the tests set it
when the file lies next to the binary.

## Snapshots: the schema as a recorded result

`tests/data/graph/<case>.json` is the census of the graph after ingesting one fixture: nodes
per label set, relationships per type, property keys per label and per relationship type,
indexes, R-tree layers. `tests/integration/test_graph_snapshots.py` compares each ingest with
it. The snapshots were recorded with the code before pykci became a package.

A change of what the ingest writes shows up as a failing case with the difference spelled out.
When the change is intended:

```bash
PYKCI_UPDATE_SNAPSHOTS=1 pytest tests/integration/test_graph_snapshots.py
git diff tests/data            # the schema change, to be reviewed with the code
```

and describe it in `GRAPH.md`. `tests/data/tiles/` holds the attribute schema of the tiled 3D
Tiles in the same way.

## Adding something

| You add | The suite asks for |
| --- | --- |
| a function | a unit test next to the tests of its module |
| a module (`src/pykci/<name>.py`) | a test that imports or runs `pykci.<name>` |
| a command | its entry in `pykci.cli.COMMANDS`, its row in `CLI.md`, its line in the docstring of `pykci/cli.py`; `--help` must work |
| an input format or an exporter | a module and a command as above, a small fixture, an integration test that runs it on the fixture; for an ingest also a graph snapshot |
| a label, relationship or property | the snapshot update and the description in `GRAPH.md` |
| code that another module already has | nothing: import it. If two modules need it, move it to a shared module (`pykci.crs`, `pykci.geometry`, `pykci.graphdb`, `pykci.gltf`, …) |

## Known defects the suite records

Marked `xfail(strict=True)`, so the run fails when one is fixed and its entry can go:

- OpenStreetMap fusion (`test_osm_fusion.py`): tags that GDAL's default `osmconf.ini` ignores
  (`source`, `ele`, `note`, …) never reach the graph, although OSM_INTEGRATION.md says no tag is
  dropped; a furniture point without a CityFurniture target gets the reason `no_overlap` instead
  of the documented `no_pykci_type`; the documented `HAS_OSM_MEMBER` relationship is never written.
- LoD3 facades (`test_lod3_facades.py`): a file whose only defect is the LoD tag gets no
  repaired copy; rings written as `gml:pos` and polygons inside a `gml:CompositeSurface` are
  skipped without a message.
