# Statistical + anomaly control (`eval/stats_control.py`)

A per-city, **before/after** quality gate for pykci. It profiles a city's source
data *before* ingestion, profiles the resulting Neo4j graph *after* ingestion,
cross-checks the two, and writes one control document per city. The same numbers
double as reproducible summary statistics for reporting.

It exists because some source datasets contain anomalies that silently corrupt
the graph. The motivating case: **swissBUILDINGS3D (Zurich)** stamps the
placeholder `gml:id="ID_"` on many buildings. Because `ingest_citygml.py` does
`MERGE (b:Building {id: row.id})` with **no uniqueness constraint**
([ingest_citygml.py:800-805](../ingest_citygml.py#L800-L805)), every building
sharing that id **collapses into one node**, hiding hundreds of real buildings
inside a single super-node and distorting every per-building statistic. This
tool catches that class of problem before it reaches Neo4j, and confirms it
afterwards.

It is complementary to, not a replacement for, the existing gates:
[roundtrip_census.py](roundtrip_census.py) (lossless CityGML round-trip) and
[osm_ingestion_census.py](osm_ingestion_census.py) (lossless OSM fusion). Those
prove *nothing was lost*; this tool asks *is the input sane, and does the graph
look right*.

## The three phases

```
py eval/stats_control.py precheck  --city NAME --citygml <file|dir ...> \
      [--roofmats F.gml] [--lod3 F.gml] [--osm F.osm.pbf]
py eval/stats_control.py postcheck --city NAME [--dataset NAME] [--deep] [--orphans]
py eval/stats_control.py report    --city NAME
```

| Phase | Reads | Writes | Purpose |
| ----- | ----- | ------ | ------- |
| `precheck` | Input files only (no DB) | `eval/results/stats_control_precheck_<city>_<ts>.json` | Statistical profile + anomalies of the source CityGML (and its roofmats / LoD3 variants) and the OSM `.osm.pbf`. **Run before ingesting a new city.** |
| `postcheck` | Neo4j graph | `eval/results/stats_control_postcheck_<city>_<ts>.json` | Statistical profile + anomalies of the ingested graph for that dataset. **Run after ingestion (and after each enrichment stage).** |
| `report` | Latest pre + post JSON for the city | `output/stats/stats_control_<city>.md` (+ a timestamped report JSON) | Fuses both phases into one severity-ranked document with a "Decisions needed" section, cross-source consistency checks, and a paper-ready statistics block. |

`--render <report.json>` rebuilds the markdown from a saved JSON with no DB or
file access (useful for regenerating the document after editing the renderer).

### Suggested per-city workflow

```
# 1. before ingest
py eval/stats_control.py precheck --city tokyo --citygml input/citygml/tokyo --osm input/osm/kanto-260703.osm.pbf
#    -> review CRITICAL anomalies; decide id policy BEFORE ingesting
# 2. ingest (ingest_citygml.py, then ingest_osm.py, enrich_lod3_facades.py, ...)
# 3. after ingest
py eval/stats_control.py postcheck --city tokyo --deep
# 4. fuse
py eval/stats_control.py report --city tokyo
#    -> output/stats/stats_control_tokyo.md
```

## What it checks

### Input CityGML (`precheck`)

Streamed with `lxml.iterparse(tag="{*}Building")` so CityGML 1.0 / 2.0 / 3.0 all
parse through one code path (matching
[tools/extract_citygml_building_footprints.py](../tools/extract_citygml_building_footprints.py));
memory is bounded by clearing each building after use.

| Code | Severity | What it detects |
| ---- | -------- | --------------- |
| `DUPLICATE_GML_ID` | critical | A `gml:id` shared by >1 building. Quantifies how many buildings collapse on MERGE. **Decision.** |
| `PLACEHOLDER_GML_ID` | critical | Structurally-placeholder ids (`ID_`, `GML_`, empty, bare prefixes). The swissBUILDINGS3D failure mode. **Decision.** |
| `MISSING_GML_ID` | critical | Building element with no `gml:id` (ingest drops it). **Decision.** |
| `NO_GEOMETRY` | high | Building with no `LinearRing` geometry. |
| `NONFINITE_COORD` | critical | NaN/Inf in coordinates. **Decision.** |
| `DEGENERATE_RING` | high | A ring with < 4 points. |
| `UNCLOSED_RING` | medium | A ring whose first vertex differs from its last. |
| `CRS_RANGE_MISMATCH` | high | Declared srsName says geographic but coords look projected (or vice versa). **Decision.** |
| `MIXED_SRS` | medium | More than one srsName across the inputs. |
| `VERTEX_COUNT_OUTLIER` | low | Buildings far above the vertex-count distribution (merged multipart / collapsed super-buildings). |
| `SPATIAL_OUTLIER` | medium | Buildings > 12 MAD from the dataset centroid (coordinate typos / wrong-tile contamination). |
| `HEIGHT_OUTLIER` | medium | `measuredHeight` negative or > 1000 m. |

Aggregate statistics collected: building count, distinct gml:ids, per-file
counts, srsName set + dominant EPSG, height / vertex-count / footprint
distributions (min/max/mean/median/p90/p99/stdev), function and roofType
histograms, and (for the `--roofmats` variant) `predictedroofmaterial` coverage
and a per-class histogram. Roof-material classes are the five predicted upstream
and embedded as the `PredictedRoofMaterial` generic attribute:
`metal, concrete, tar_paper, glass, roof_tiles`.

### Input OSM (`precheck --osm`)

Pre-ingest, the `.osm.pbf` is summarized with `ogrinfo -so` per layer
(`points / lines / multipolygons / multilinestrings / other_relations`); no bbox
is needed. If `ogrinfo` is not on PATH the tool records `OGRINFO_UNAVAILABLE`
(info) and moves on. **Deep, lossless OSM verification is
[osm_ingestion_census.py](osm_ingestion_census.py)** and is deliberately not
duplicated here.

### Graph (`postcheck`)

Fast checks (default) use indexed label/relationship counts and one pass over the
building fan-out; deep checks are opt-in because the graph can hold tens of
millions of nodes.

| Code | Severity | What it detects |
| ---- | -------- | --------------- |
| `ID_COLLAPSE` | critical | Building nodes with anomalous fan-out (boundaries far above the distribution, or > 20 building parts): the signature of many source buildings merged onto one `gml:id`. **Decision.** |
| `MISSING_SOURCE` | medium | Building / OsmFeature nodes without a `source` (breaks the provenance principle). |
| `OSM_DUPLICATE_ID` | high | The same `(osm_type, osm_id)` on more than one OsmFeature. (A node and a way sharing a numeric id is *not* flagged; that is normal OSM.) |
| `OSM_HIGH_ORPHAN_RATE` | low | > 90% of OSM features unmatched. Informational: expected for city-wide extracts (roads, natural areas, POIs outside footprints). |
| `ORPHAN_NODES` | high | Nodes with no relationships (needs `--orphans`; full-graph anti-join). |
| `BUILDING_NO_GEOMETRY` | high | Building with neither a solid nor a boundary surface (needs `--deep`). |

Statistics collected: full node-label and relationship-type inventory with
counts, total nodes/edges, boundaries-per-building and parts-per-building
distributions, `measured_height` distribution, OSM kind breakdown +
matched/orphan split + orphan reasons + enriched-building count + POI count,
`predictedroofmaterial` coverage + histogram, LoD3 enriched-building count +
`HAS_LOD3_FACADE` edge count, and (with `--deep`) the **nodes-per-building
subtree distribution** (min/avg/median/p90/p99/max) computed via
`apoc.path.subgraphNodes`, blacklisting upward edges and the OSM layer so each
building's own CityGML subtree is counted.

Flags:
- `--deep` adds nodes-per-building (APOC, ~seconds for 100k buildings) and the
  no-geometry check (bounded by building count).
- `--orphans` adds the full-graph orphan anti-join (can take minutes on
  multi-million-node graphs).
- `--dataset NAME` selects the dataset when more than one is present; otherwise
  the single dataset is used automatically.
- `--source-like STR` restricts building checks to `source CONTAINS STR`.

### Cross-source (`report`)

| Code | Severity | What it detects |
| ---- | -------- | --------------- |
| `BUILDING_LOSS` | critical | `distinct input gml:ids − graph Building nodes` > 0: exactly how many buildings the id-collapse cost. **Decision.** |
| `BUILDING_COUNT_MISMATCH` | medium | Graph has more buildings than distinct input ids (residual from a prior ingest, or cross-dataset id reuse). |

## Severity model and "Decisions needed"

Every anomaly is `{code, severity, scope, count, message, examples,
recommendation, needs_decision}`. `report` sorts by severity, then surfaces the
`needs_decision=true` items in a dedicated **Decisions needed** table at the top
of the document. Those are the ones that change what you do next (mainly: the
id-uniqueness policy for a source that ships duplicate/placeholder ids). The
recommended fix for the id-collapse class is to synthesize a stable, unique id
at ingest for the affected source (for example `source-file + gml:id + ordinal`,
or a geometry hash) instead of MERGE-ing on the raw `gml:id`.

## Output conventions

Follows the house style ([osm_ingestion_census.py](osm_ingestion_census.py),
[bench/measure_disk_footprint.py](../bench/measure_disk_footprint.py)):
timestamped machine JSON under `eval/results/`, the human-facing markdown under
`output/stats/` keyed by city (`stats_control_<city>.md`, overwritten per run,
with the timestamp inside). Filenames slug `Dataset.name`; timestamps are
`datetime.now().strftime("%Y%m%d_%H%M%S")`.

The document ends with a **Paper-ready summary statistics** JSON block (building
counts, node/edge totals, nodes-per-building, height distribution, OSM totals,
roofmat coverage, anomaly counts by severity) intended to be lifted directly
into a table.

## Dependencies

- `lxml` (CityGML precheck), `neo4j` (postcheck). Both already required by the
  core pipeline.
- `ogrinfo` (GDAL) on PATH for the optional OSM layer counts; gracefully skipped
  if absent.
- APOC in Neo4j for the `--deep` nodes-per-building stat; gracefully reported as
  unavailable if the procedure is missing.

Run with `py` from the project root (Neo4j must be running for `postcheck`).
