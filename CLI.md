# pykci command line

`pykci` (the module `pykci.cli`) is one entry point to the pipeline. It is meant for people
and for programs that drive pykci (CI jobs, containers, other applications).

```bash
pip install -e .                      # editable install → `pykci` command
pykci ingest-citygml input/citygml/hamburg --dataset-name hamburg
pykci ingest-osm input/osm/hamburg-260629.osm.pbf
pykci build-3dtiles --tiled --workers 8 --out output/3dtiles/hamburg
pykci export-citygml --ids DEHH_123,DEHH_456 --out selection.gml

python pykci_cli.py info              # same thing without installing
```

The scripts still run directly from a checkout (`py ingest_citygml.py …`) exactly as before,
without installing: the files at the repository root forward to the package (`_pykci_shim.py`).
Installed, every step is also `python -m pykci.<module>`.

## Commands

| Command | Runs | Purpose |
| --- | --- | --- |
| `ingest-citygml` | `pykci.ingest_citygml` | CityGML 2.0 / 3.0 → Neo4j (see CITYGML3.md) |
| `ingest-osm` | `pykci.ingest_osm` | OpenStreetMap enrichment (`.osm.pbf`) |
| `ingest-roofmats` | `pykci.ingest_roof_materials` | ML roof-material predictions (GeoJSON) |
| `export-citygml` | `pykci.export_citygml` | Neo4j → CityGML 2.0 / 3.0 (`--version 3`): whole dataset, `--ids`/`--ids-file` selection, or `--bbox` |
| `build-3dtiles` | `pykci.build_3dtiles` | Neo4j → 3D Tiles; `--tiled` for whole cities (see below) |
| `footprints` | `pykci.extract_citygml_building_footprints` | CityGML → GeoJSON footprints, no Neo4j |
| `upgrade-citygml` | `pykci.upgrade_citygml_10_to_20` | CityGML 1.0 → 2.0, no Neo4j |
| `validate-citygml` | `pykci.validate_citygml` | Offline XSD validation of CityGML 2.0 / 3.0 files (`--setup` once), no Neo4j |
| `inventory` | `eval/parse_citygml_inventory.py` | Per-feature inventory of a GML file |
| `stats` | `eval/stats_control.py` | `precheck` / `postcheck` / `report` |
| `osm-census` | `eval/osm_ingestion_census.py` | OSM completeness gate |
| `inspect` | built in | CityGML version, CRS, envelope, LoDs, feature counts, duplicate ids. **JSON**, streaming, no Neo4j |
| `info` | built in | Graph summary: datasets (CRS, feature extent incl. WGS84), counts, which provenance layers exist. **JSON** |

Pipeline commands pass their arguments through unchanged, so `pykci <command> --help`
prints the module's own help. The `eval/` commands are research scripts outside the
package: they need a source checkout or an editable install.

## As a library

Every step is a module of the package `pykci`, so other programs import what they need
instead of copying it:

```python
from pykci.ingest_citygml import ingest            # the ingest as a function
from pykci.provenance import layer_of              # the provenance layer of a value
from pykci.crs import horiz_epsg_from_srs          # srsName → EPSG
from pykci.runtime import connect                  # a Neo4j driver for the configured connection
```

The command table can be extended for one call: `main()` takes further commands and the
program name its help shows.

```python
from pykci import cli

def main(argv=None):
    return cli.main(argv, commands={"my-command": ("module", "my_package.my_command")}, prog="my-cli")
```

Each command must pass the checks of `tests/contract/test_commands.py`; see TESTING.md.

## Options for programs

| Global option | Environment variable | Default |
| --- | --- | --- |
| `--neo4j-uri` | `NEO4J_URI` (also accepts `NEO4J_URL`) | `bolt://localhost:7687` |
| `--neo4j-user` | `NEO4J_USER` | `neo4j` |
| `--neo4j-password` | `NEO4J_PASSWORD` | `neo4jneo4j` |
| `--progress` | `PYKCI_PROGRESS=1` | off |

The environment variables work for the plain scripts too (`pykci.runtime`).

**Progress events.** With `--progress`, the scripts print one line per event on stdout:

```text
PYKCI_PROGRESS {"stage": "ingest.parse", "t": 1760000000.0, "done": 12000, "unit": "features", "file": "hamburg.gml"}
```

Parse lines starting with `PYKCI_PROGRESS ` as JSON; every other line is the normal
human log. Stages: `ingest.parse|parsed|spatial_index|file|done`,
`osm.extract|write|match|pois|done`, `export.start|features|done`,
`tiles.index|schema|leaf|overview|done`, `inspect.file|read`. Events carry `done` and,
when known, `total`. `ingest_citygml.py` now exits with status 1 when a file fails,
so callers do not have to parse the log.

## Tiled 3D Tiles with semantic attributes

`build_3dtiles.py --tiled` writes a 3D Tiles 1.1 quadtree for a whole city:

- **Leaf tiles** hold the full geometry of at most `--max-features` top-level
  features (default 1500). A `Building` includes all its `BuildingPart`s. Each
  geometry owner is drawn from one representation only: a reconstructed LoD3 facade
  (`HAS_LOD3_FACADE`, from `enrich_lod3_facades.py`), else boundary surfaces, else
  the highest-LoD solid, else the highest-LoD multi-surface. Polygons shared by xlink
  are de-duplicated. LoD3 windows and doors get their own materials;
  `--no-lod3` draws the authoritative LoD2 shell instead.
- **Coarser tiles** hold LoD1 blocks (footprint extruded to the building height) of
  the `--overview-features` most prominent buildings below them, so the whole city
  is visible from afar.
- **Attributes.** Every feature carries `EXT_mesh_features` ids and an
  `EXT_structural_metadata` property table with all its scalar attributes, so a
  viewer can show a clicked building without a database. The tiler adds
  `feature_type`, `district`, `function_name`, `roof_type_name`, `address_*`
  (parsed from the authoritative `bldg:address`), `geometric_height`,
  `footprint_area`, `geometry_lod` (the LoD drawn; 3 for a LoD3 facade), `longitude`
  and `latitude`.
- **Provenance.** Each schema property records its provenance layer (`authoritative`,
  `crowd_sourced`, `ml_predicted`, `reconstructed`, `derived`; see
  `pykci.provenance`). `feature_schema.json` next to `tileset.json` repeats the
  schema for clients.
- **Speed.** `--workers N` builds leaf tiles in parallel processes.

The single-GLB output without `--tiled` is unchanged.
