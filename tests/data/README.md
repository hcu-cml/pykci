# Recorded results and test inputs

`graph/` and `tiles/` hold recorded results the tests compare with (`pykci.testing.snapshot`).
They are data, not code: never edit them by hand. `fixtures/` holds small hand-made inputs.

| Folder | Used by | What |
| --- | --- | --- |
| `graph/<case>.json` | `tests/integration/test_graph_snapshots.py` (the CityGML fixtures), `test_osm_fusion.py` (`osm_fusion`), `test_lod3_facades.py` (`lod3_facade`) | The graph after a pipeline step: nodes per label set, relationships per type, property keys per label and per relationship type, indexes, R-tree layers |
| `tiles/<case>.json` | `tests/integration/test_pipeline.py` | The attribute schema (`feature_schema.json`) of a tiled 3D Tiles build, with the provenance layer of every attribute |
| `fixtures/osm/` | `test_osm_fusion.py` | A hand-written OpenStreetMap extract around the FZK-Haus building |
| `fixtures/lod3/` | `test_lod3_facades.py` | A hand-written LoD3 facade file for the FZK-Haus building |

The CityGML snapshots and `osm_fusion` were recorded with the code before pykci became a
package, so they also show that the restructuring changed nothing.

To record a deliberate change, run the test with `PYKCI_UPDATE_SNAPSHOTS=1` against the test
database, then review `git diff tests/data` together with the code and the documentation that
describe the change (TESTING.md). A snapshot that no test names fails
`tests/contract/test_every_module_is_tested.py`.
