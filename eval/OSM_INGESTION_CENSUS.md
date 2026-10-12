# OSM Ingestion Census — completeness and placement gate

> Companion to [`osm_ingestion_census.py`](osm_ingestion_census.py). Concept and
> matching method of the enrichment itself: [OSM_INTEGRATION.md](../OSM_INTEGRATION.md).
> CityGML counterpart of this gate: [`roundtrip_census.py`](roundtrip_census.py).

## Why this exists

The SIGSPATIAL '26 pykci paper proved, via the round-trip census, that the
CityGML mapping covers **all thematic modules and LoDs without loss**. The KDD
AuthentiCity paper makes the corresponding claim for the crowd-sourced layer:
the OSM ingestion (`ingest_osm.py`) ingests **all admitted OSM data** and stores
every feature **in the correct place** — either attached to its corresponding
CityGML feature node, or as a net-new standalone node when no anchor exists.
That claim needs its own gate: fusion statistics (match rates, orphan counts)
describe *what was matched*, not whether *anything was silently dropped or
corrupted on the way into the graph*. This census closes that gap.

## The contract being verified

`ingest_osm.py` promises that for every OSM feature admitted from the source
`.osm.pbf` (see *Scope* below):

1. exactly one `(:OsmFeature)` node exists under the stable id
   `osm:<node|way|relation>/<osm_id>`;
2. **every tag** survives, key and value, either as a promoted property
   (`osm_*`, `name`, `addr_*`) or inside the `other_tags` JSON — the same
   lossless long-tail treatment `gen:*Attribute` gets on the CityGML side;
3. the geometry is stored **verbatim in the dataset CRS** on the reused CityGML
   geometry chain (`GeometryPolygon`/`GeometryRing.pos_list`,
   `GeometryLineString.pos_list`), points as a `location`;
4. the feature is **placed**: it either carries `matched=true` and at least one
   incoming `ENRICHED_BY` (footprint match) or `HAS_POI` (containment) edge
   from a `Building`/`CityObject` anchor, or it carries `matched=false` plus a
   machine-readable `unmatched_reason` (`no_pykci_type`, `no_overlap`,
   `no_containing_building`) — attached or standalone, never dropped;
5. it is registered in the dedicated `osm_features` R-tree, linked
   `PART_OF -> Dataset`, labelled `Entity` + its dynamic `Osm*` kind label, and
   carries provenance (`source`, `osm_id`, `osm_type`, `osm_kind`);
6. enrichment scalars copied onto anchor nodes stay inside the namespaced
   `osm_*`/`addr_*` key set (`ENRICH_KEYS`) — authoritative values are never
   overwritten (the Provenance & Multi-Source Design Principle, GRAPH.md).

## Method: source-derived expectation vs. independent graph state

The census re-derives the *expected* graph content directly from the source
`.osm.pbf`, using the **same code path** the ingestion used (`extract_layer`
ogr2ogr clip+reproject, `classify_kind`, `build_osm_props`, `_pos_list` are
imported from `ingest_osm`), with the city bounding box (bbox) recomputed from the graph's
`Dataset` node exactly as the ingestion did. It then verifies the live graph
against that expectation. The comparison is therefore between **independent
database state** and a **source-derived expectation** — nothing is taken from
the ingestion run's own bookkeeping or summary output.

Five census levels, all of which must be clean for exit code 0:

| # | Level | Check |
| - | ----- | ----- |
| 1 | Features | every expected id resolves to exactly one `OsmFeature`; per-kind counts and dynamic labels match; **no graph extras** for this source |
| 2 | Tags | tag dict reconstructed from the node (promoted keys + `addr_*` + `other_tags`) equals the source tag dict — canonical-JSON SHA1 per feature |
| 3 | Geometry | order-independent SHA1 over the feature's stored `pos_list` strings equals the digest of the source geometry rendered by the same `_pos_list`; bbox/center floats exact; points carry `location` |
| 4 | Placement | `matched=true` ⇔ ≥1 incoming `ENRICHED_BY`/`HAS_POI`; `matched=false` ⇒ `unmatched_reason` set and no anchor edge; every node `PART_OF -> Dataset`; every `ENRICHED_BY` edge carries overlap ratio, Jaccard, intersection area, case label, primary flag; anchors are `Building`/`CityObject` only; anchor `osm_*` keys ⊆ `ENRICH_KEYS` |
| 5 | Indexing | every node with a `bbox_wkt` has an incoming `RTREE_REFERENCE` (the `osm_features` R-tree); provenance fields non-null; `Entity` label present |

Like the CityGML census, the bar is **semantic completeness, not byte
identity**: the R-tree plugin re-encodes `bbox_wkt` and adds `gtype`/`bbox`
bookkeeping properties, ring order inside a feature is not significant (hence
the order-independent digest), and duplicate stable ids (the same feature
surfacing in two GDAL layers) are compared against any single candidate or
their union, and counted.

## Scope: what "all OSM data" means, precisely

GDAL's OSM driver exposes five canonical layers. The ingestion reads the three
geometry-bearing ones. `points` and `multipolygons` are ingested **unfiltered**:
every feature GDAL assembles inside the city bbox becomes an `OsmFeature`
node (features without a routable thematic tag land in kind `other` and are
kept as standalone nodes). `lines` is restricted to the transport/water network
(`highway`/`railway`/`waterway`). Deliberately out of scope:

- **untagged geometry** (nodes/ways that only exist as vertices of other
  features — GDAL's `points` layer only materialises tagged nodes to begin
  with, so these carry no thematic content of their own);
- **non-network lines** (barriers, power lines, …) excluded by the `lines`
  filter — for Hamburg 44,341 of 274,507 in-bbox line features (16.2 %);
- **`multilinestrings`** (route relations) and **`other_relations`**
  (boundary/site relations): these are *views over member ways and nodes* that
  are already ingested individually through the base layers; ingesting them
  would duplicate geometry without adding footprint semantics (Hamburg: 1,425 +
  8,613 relations);
- features whose geometry GDAL cannot assemble inside the city bbox
  (counted by the census as `geometry_skipped`; Hamburg: 0).

(Historical note, discovered by this census's scope pass: the code originally
carried a tag-filter WHERE clause for `points`/`multipolygons` too, but it
referenced fields outside GDAL's default osmconf schema, so the filter never
applied and those layers were always ingested unfiltered. The dead clause was
removed from `ingest_osm.py`; no graph content or published count changed.)

So the verified claim is: **100 % of the admitted features are in the graph,
lossless and correctly placed**, and the census *quantifies* the admission
scope per layer (unfiltered vs. admitted counts inside the same city bbox)
instead of leaving it implicit — for Hamburg, 1,188,139 of 1,242,518 features
across all five layers (95.6 %).

## Running it

```bash
# environment (Windows box): ogr2ogr from QGIS — see CLAUDE.md / TEASER_RENDERING.md
py eval/osm_ingestion_census.py --pbf input/osm/hamburg-260629.osm.pbf
# options: --dataset NAME (if >1 dataset), --buffer (must match the fusion run),
#          --skip-scope (skip the unfiltered counts), --workers N
```

Prerequisites: Neo4j running with the *already fused* graph (the census is
non-destructive, read-only apart from nothing — it writes no graph data), and
the exact `.osm.pbf` the fusion ingested. Runtime for whole Hamburg
(1.19 M OSM features): the expectation build is ogr2ogr-bound, the graph pass
runs ~1,200 batched reads over parallel sessions; low tens of minutes in total.

Outputs:

- `eval/results/osm_ingestion_census_<city>_<ts>.json` — full machine-readable
  report (per-gate counts, offending ids capped at `--max-report`);
- `output/stats/osm_ingestion_census_<city>.md` — human-readable summary table;
- `output/stats/osm_ingestion_census_<city>.tex` — booktabs coverage table
  (source-in-bbox vs ingested per layer with exact exclusions, the five
  verification gates stated as positive coverage, placement split).
  `--render <report.json>` regenerates the md + tex from a saved report without
  touching the database or the pbf.

Exit code 0 only when every gate is clean.

**Per-city workflow (standing rule):** after every city's OSM fusion, (1) run
the census, it must pass; (2) analyze the composition of the non-admitted lines
for that city (ogr2ogr CSV of the unfiltered `lines` layer, count by
`barrier`/`man_made`/building-annotation tags) so the exclusions can be stated
exactly alongside the coverage table. Hamburg's excluded-lines composition:
16,949 barrier, 13,454 building-annotation open ways, 4,151 man_made,
remainder power cables, `area:highway`, aeroways, admin-boundary segments.

## Results

### Hamburg (whole city, `hamburg-260629.osm.pbf`, run 2026-07-10)

Full table: `output/stats/osm_ingestion_census_hamburg_citygml2_lod2_2025_roofmats_gml.md`
(JSON in `eval/results/`). Headline: **PASSED**, zero findings on every gate.

- **Features**: all 1,188,139 admitted OSM features (417,309 multipolygons,
  540,664 points, 230,166 lines; 0 duplicates, 0 unparseable) are present as
  `OsmFeature` nodes; 0 missing, 0 graph extras; per-kind counts match exactly
  (building 354,162 · road 281,056 · furniture 299,406 · other 140,660 ·
  poi 81,353 · landuse 20,810 · vegetation 7,753 · water 2,939).
- **Tags + geometry**: 0 tag-reconstruction mismatches, 0 geometry digest
  mismatches, 0 bbox mismatches, 0 missing point locations — lossless.
- **Placement**: exact partition — 356,707 features `matched=true` with at
  least one `ENRICHED_BY`/`HAS_POI` anchor edge; 831,432 features
  `matched=false`, each with an `unmatched_reason` and no anchor edge; no
  feature in neither or both states; 0 anchor edges from non-CityGML nodes;
  0 `ENRICHED_BY` edges missing provenance properties; 0 anchor nodes with
  `osm_*` keys outside the enrichment namespace.
- **Indexing/provenance**: all 1,188,139 nodes in the `osm_features` R-tree,
  all with `PART_OF -> Dataset` and full provenance fields.
- **Scope** (unfiltered inside the same city bbox): points 540,664/540,664 and
  multipolygons 417,309/417,309 admitted (100 %); lines 230,166/274,507
  (83.8 %, transport/water network only); multilinestrings 1,425 and
  other_relations 8,613 skipped by design (views over already-ingested
  members). Overall 95.6 % of the five-layer total.

### Helsinki (whole city, `finland-260704.osm.pbf`, run 2026-07-12)

Full table: `output/stats/osm_ingestion_census_helsinki_citygml2_lod2_2019_gml.md`
(LaTeX `.tex` beside it; JSON in `eval/results/`). Run against the graph built
with the tiered identity resolution (all 2,980 building features as distinct
nodes; the 2026-07-10 run against the pre-fix graph is superseded, its
bbox-fallback footprints had inflated the match counts). Headline: **PASSED**,
zero findings on every gate. All 55,930 admitted OSM features (8,344
multipolygons, 32,406 points, 15,180 lines; 0 duplicates, 0 unparseable) are
present, lossless, and placed: 4,281 attached to CityGML anchors (2,476
buildings enriched, 83.1 % + 1,797 POIs), 51,649 standalone with a reason. All
five verification gates 100 %; all nodes R-tree-registered with provenance.
Scope: points 32,406/32,406 and multipolygons 8,344/8,344 admitted (100 %);
lines 15,180/20,961 (72.4 %, network only; the 5,781 excluded are 2,999
barriers, 1,353 man-made, ~555 building-annotation ways, rest
natural/administrative); multilinestrings 235 + other_relations 381 skipped by
design. Overall 89.7 % of the 62,327-feature five-layer window. This is the
first city confirming the per-city variability: no ML roof-material layer
exists for Helsinki, so the cross-source roof-material overlap is zero while
height/storey redundancy is present.

### Zurich (swissBUILDINGS3D 3.0, `switzerland-260704.osm.pbf`, run 2026-07-11)

> **Pre-fix graph.** This run predates the 2026-07-12 identity resolution (45
> buildings were collapsed on duplicate/placeholder ids); it is superseded once
> Zurich is re-ingested and re-censused.

Full table: `output/stats/osm_ingestion_census_zurich.md` (LaTeX `.tex` beside it;
JSON in `eval/results/`). Headline: **PASSED**, zero findings on every gate. All
1,860,401 admitted OSM features (591,777 multipolygons, 813,108 points, 455,516
lines; 0 duplicates, 0 unparseable) are present, lossless, and placed: 102,789
attached to CityGML anchors (93,920 buildings enriched + 13,583 POIs + others),
1,757,612 standalone with a reason. All five verification gates 100 %; all nodes
R-tree-registered with provenance. Scope: points 813,108/813,108 and
multipolygons 591,777/591,777 admitted (100 %); lines 455,516/524,034 (86.9 %,
network only; the 68,518 excluded are 39,430 barriers, ~4,994 building-annotation
ways, 3,870 man-made, rest power/cable/golf/natural/administrative);
multilinestrings 5,196 + other_relations 6,911 skipped by design. Overall 95.8 %
of the 1,941,026-feature five-layer window. Second no-ML-layer city: Zurich's
source also has no roof material and, unlike Helsinki, no storey count either, so
its only cross-source attribute overlap is height (looser than the others,
median |Δh| 2.3 m; see the KDD paper `tab:dual_attrs`). This is the heaviest
instance per building (LoD2.3 geometry): 41.5 M nodes, 27.84 GiB CityGML store,
5.72 GiB dump.

Per-city runs for the remaining Tier-1 cities (Tokyo, New York) follow the same
command after their CityGML ingest + OSM fusion (KDD notes, step D1).
