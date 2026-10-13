# pykci command line

`pykci_cli.py` is one entry point to the pipeline scripts. It is meant for people
and for programs that drive pykci (the pykci web client, CI jobs, containers).

```bash
pip install -e .                      # editable install → `pykci` command
pykci ingest-citygml input/citygml/hamburg --dataset-name hamburg
pykci ingest-osm input/osm/hamburg-260629.osm.pbf
pykci build-3dtiles --tiled --workers 8 --out output/3dtiles/hamburg
pykci export-citygml --ids DEHH_123,DEHH_456 --out selection.gml

python pykci_cli.py info              # same thing without installing
```

The scripts still run directly (`py ingest_citygml.py …`) exactly as before.

## Commands

| Command | Runs | Purpose |
| --- | --- | --- |
| `ingest-citygml` | `ingest_citygml.py` | CityGML 2.0 → Neo4j |
| `ingest-osm` | `ingest_osm.py` | OpenStreetMap enrichment (`.osm.pbf`) |
| `ingest-roofmats` | `ingest_roof_materials.py` | ML roof-material predictions (GeoJSON) |
| `export-citygml` | `export_citygml.py` | Neo4j → CityGML: whole dataset, `--ids`/`--ids-file` selection, or `--bbox` |
| `build-3dtiles` | `build_3dtiles.py` | Neo4j → 3D Tiles; `--tiled` for whole cities (see below) |
| `footprints` | `tools/extract_citygml_building_footprints.py` | CityGML → GeoJSON footprints, no Neo4j |
| `upgrade-citygml` | `tools/upgrade_citygml_10_to_20.py` | CityGML 1.0 → 2.0, no Neo4j |
| `inventory` | `eval/parse_citygml_inventory.py` | Per-feature inventory of a GML file |
| `stats` | `eval/stats_control.py` | `precheck` / `postcheck` / `report` |
| `osm-census` | `eval/osm_ingestion_census.py` | OSM completeness gate |
| `inspect` | built in | CityGML version, CRS, envelope, LoDs, feature counts, duplicate ids. **JSON**, streaming, no Neo4j |
| `info` | built in | Graph summary: datasets (CRS, feature extent incl. WGS84), counts, which provenance layers exist. **JSON** |

Pipeline commands pass their arguments through unchanged, so `pykci <command> --help`
prints the script's own help. `tools/` and `eval/` commands need a source checkout or
an editable install.

## Options for programs

| Global option | Environment variable | Default |
| --- | --- | --- |
| `--neo4j-uri` | `NEO4J_URI` (also accepts `NEO4J_URL`) | `bolt://localhost:7687` |
| `--neo4j-user` | `NEO4J_USER` | `neo4j` |
| `--neo4j-password` | `NEO4J_PASSWORD` | `neo4jneo4j` |
| `--progress` | `PYKCI_PROGRESS=1` | off |

The environment variables work for the plain scripts too (`pykci_runtime.py`).

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
  `pykci_provenance.py`). `feature_schema.json` next to `tileset.json` repeats the
  schema for clients.
- **Speed.** `--workers N` builds leaf tiles in parallel processes.

The single-GLB output without `--tiled` is unchanged.
