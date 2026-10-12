# Roof-material ingestion (`ingest_roof_materials.py`)

Attaches an ML classifier's **per-building roof-material predictions** to the
authoritative CityGML graph as the *ML-predicted* layer of the multi-source model
(authoritative CityGML + crowd-sourced OSM + **ML-predicted roof materials** +
reconstructed LoD3). It is additive, provenance-aware, and **matched by building
`gml:id` only** — the GeoJSON footprint geometry is ignored, so there is no spatial
matching to tune.

## Input

Two files, both under `input/geojson/`:

1. **The predictions GeoJSON**, one `Feature` per building:

   ```json
   { "properties": {
       "gml_id": "DEHHALKAP00013PP",
       "predicted_roof_materials": ["4", "1"],   // material CLASS CODES
       "material_cov":            [0.4323, 0.5677] // pixel coverage, ALIGNED to the codes
     }, "geometry": { "type": "Polygon", ... } }   // ignored
   ```

   `predicted_roof_materials[i]` is classified with pixel coverage `material_cov[i]`
   (the order is consistent, so code `4` above has coverage `0.4323`). A building may
   carry one or several materials (in the Hamburg 2025 input: 194,799 buildings, up to
   3 materials each, almost all single-material).

2. **The class dictionary** `roof_materials.csv` (`;`-separated `code;name`):

   ```text
   0;concrete
   1;metal
   2;glass
   3;roof_tiles
   4;tar_paper
   ```

## What it writes (schema)

For every building whose `gml:id` is found as a `:Building` node, ranked by coverage
**descending** (so index `0` is the dominant class):

| Property | Type | Meaning |
| --- | --- | --- |
| `predicted_roof_material_<i>` | string | material **name**, `i = 0 .. count-1`, ranked by coverage desc |
| `predicted_roof_material_<i>_coverage` | float | fraction of the building's roof pixels the classifier assigned to that material (`0..1`) |
| `predicted_roof_material_count` | int | number of predicted materials |
| `predicted_roof_material_source` | string | provenance tag of this ML layer, e.g. `ml:roof_material:hamburg_roofmats_with_citygml_footprints_2025` |
| `predictedroofmaterial` | string | the **primary** (highest-coverage) material name; identical to `predicted_roof_material_0` |

Example (a two-material building):

```text
predictedroofmaterial              = tar_paper
predicted_roof_material_0          = tar_paper
predicted_roof_material_0_coverage = 0.784557
predicted_roof_material_1          = concrete
predicted_roof_material_1_coverage = 0.215443
predicted_roof_material_count      = 2
predicted_roof_material_source     = ml:roof_material:hamburg_roofmats_with_citygml_footprints_2025
```

### Design decisions (answering the open questions)

- **Indexed scalar keys (`_0`, `_1`, …) rather than array properties.** This mirrors
  the `osm_*` scalar style and, as the paper argues for the vocabulary hubs, indexed
  keys are visible in the *graph schema* a text-to-query model is prompted with and
  give one-hop per-rank access (`predicted_roof_material_0`, its `_coverage`), which a
  packed list property does not. The count is small and bounded (one index per
  material class), so the schema stays compact.
- **Roof material stays a namespaced scalar layer, not a hub node.** Promoting roof
  material to a `RoofMaterial` hub with per-edge confidence and model provenance is the
  documented v2 extension (it changes graph content); this ingestion implements the
  current scalar design. Every written key is in the derived `predicted_roof_material*`
  namespace, so it can never overwrite an authoritative CityGML/ALKIS attribute.
- **`predictedroofmaterial` is kept for backward compatibility.** `build_3dtiles.py
  --color-by roofmat` and `export_roofmat_footprints.py` read that exact (one-word)
  key; it is populated with the primary material so those tools keep working. It always
  equals `predicted_roof_material_0`.
- **Coverage vs. confidence.** The value stored is the **pixel-coverage fraction**: the
  share of the building's roof-pixel area the classifier assigned to that material. It
  *is* the model's per-material confidence signal — `_0` is the highest-confidence
  (dominant) class — which is why it drives the ranking and satisfies the "carry
  confidence on predictions" provenance rule. It is named `coverage` rather than
  `confidence` deliberately, because it is an **areal fraction**, not a calibrated
  posterior probability; calling it coverage keeps that semantics honest while still
  serving as the confidence measure. Per-building coverages usually sum to ≈ 1.

## Provenance & coverage accounting

Follows the project's binding provenance principle:

- **`source` on every enriched building** (`predicted_roof_material_source`) traces the
  layer to its origin; nothing authoritative is overwritten (derived namespace only).
- **Confidence is carried** (the per-material coverage).
- **Partial coverage is explicit**: the script reports `graph_buildings_with_prediction`
  vs `graph_buildings_without_prediction`, so *absence of a prediction is not a negative
  prediction* — it is the uncovered subset (bounded by orthophoto availability). This
  supports the coverage-aware benchmark question category.

## Idempotency

Before writing, the script clears this layer's keys from any building that already
carries the same `predicted_roof_material_source` (a bounded, label-anchored
`Building` scan in `CALL … IN TRANSACTIONS` batches), so a re-run reflects the current
input with no stale higher-index keys. It is a no-op on a graph that has never had this
layer. Matching by `gml:id` (the `Entity(id)` index) means the run naturally touches
only the buildings present in the input.

## Where it fits in the pipeline

The roof-material layer is added **after** the CityGML ingest and OSM fusion and
**before** LoD3 enrichment:

```text
ingest_citygml.py  →  ingest_osm.py  →  ingest_roof_materials.py  →  enrich_lod3_facades.py
```

It is independent of OSM (it neither reads nor writes OSM state) and of the spatial
R-tree (it only sets scalar properties), so its position relative to OSM is a
convention, not a dependency; placing it after fusion keeps the authoritative +
crowd-sourced base complete before the derived opinion layer rides on top.

## Usage

```bash
# whole Hamburg (default dictionary = roof_materials.csv beside the GeoJSON)
py ingest_roof_materials.py input/geojson/hamburg_roofmats_with_citygml_footprints_2025.geojson

# options
py ingest_roof_materials.py <file.geojson> \
   [--dict roof_materials.csv] [--dataset NAME] [--source-tag STR] [--batch 5000]
```

`--dataset` scopes only the idempotency reset (matching is by `gml:id` regardless).
Writes a JSON summary to `output/<geojson-stem>_roofmat_summary.md`
(features in / buildings enriched / input ids not found / coverage % / primary-material
distribution).

## Test (HafenCity)

Ingest the plain HafenCity tile (`hamburg_citygml2_lod2_2025_hafencity.gml`, 81
buildings, no baked-in roofmats) and run the ingestion against the whole-Hamburg
GeoJSON: 70 of the 81 buildings match by `gml:id` (the other 194,729 input ids belong
to the rest of Hamburg and are correctly not matched), 70/81 = 86.4 % coverage, primary
materials `{tar_paper 31, concrete 22, metal 6, roof_tiles 6, glass 5}`. Re-running is
idempotent (same 70, no stale keys).
