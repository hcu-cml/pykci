# LoD3 facade integration — concept & implementation

Companion document for [enrich_lod3_facades.py](enrich_lod3_facades.py). It explains **why the
script is built the way it is**, the **method** by which a CityGML 2.0 LoD3 facade dataset is
folded into an existing pykci graph, and the **scientific problems** the design solves. Read this
alongside the *Provenance & Multi-Source Design Principle* in [GRAPH.md](GRAPH.md) and the
reference implementation it mirrors, [OSM_INTEGRATION.md](OSM_INTEGRATION.md).

The worked example throughout is
[input/citygml/hamburg/hamburg_citygml2_lod3_facades_holes_export.gml](input/citygml/hamburg/hamburg_citygml2_lod3_facades_holes_export.gml)
— 17 HafenCity buildings with reconstructed LoD3 facades: 226 wall / 60 roof / 17 ground surfaces,
416 windows, 83 doors, and 499 window/door **holes** cut into the walls.

---

## 1. The problem in two halves

A LoD3 facade dataset is a **second, lower-trust observation** of buildings that pykci already holds
authoritatively at LoD2 from ALKIS/CityGML. Two independent difficulties stand between the file and a
usable graph:

1. **The file is not schema-clean.** Real reconstruction/export pipelines emit CityGML that a strict
   validator rejects and that downstream tools (the pykci ingester, the 3D-Tiles builder, the
   viewers) mis-handle. We must repair it *without destroying the original evidence*.
2. **The data must not corrupt the authoritative graph.** LoD3 facades are **reconstructed**, not
   measured. They may only be added in a way that keeps their origin and reliability explicit, never
   overwriting an authoritative LoD2 value, and never silently blending trust levels.

`enrich_lod3_facades.py` addresses these as two ordered stages — **fix**, then **enrich** — so that
only schema-conformant, provenance-tagged data ever reaches Neo4j.

---

## 2. Diagnosis of the input defects

Validating the file against the local CityGML 2.0 schemas in
[citygml-model/xsd_citygml2/](citygml-model/xsd_citygml2/) (profile `profiles/base/2.0/CityGML.xsd`,
with GML 3.1.1 resolved from the OGC server and the OASIS `xAL` import redirected to the bundled
[xAL.xsd](citygml-model/xsd_citygml2/xAL/xAL.xsd)) exposes three defect classes:

| # | Class | Evidence | Severity |
|---|-------|----------|----------|
| a | **Element-order violation** | Every building emits `bldg:lodNSolid` **after** `bldg:boundedBy`. The `AbstractBuildingType` content model is an ordered `xs:sequence` ([building.xsd:60–68](citygml-model/xsd_citygml2/building/2.0/building.xsd#L60-L68)) where `lod2Solid` precedes `boundedBy` and `lod3Solid` follows it. → **17 hard XSD errors.** | Invalid |
| b | **LoD-tag inconsistency** | Surfaces carry LoD3-only features — `bldg:opening` windows/doors and `gml:interior` wall holes — but the geometry is tagged `lod2Solid` / `lod2MultiSurface`. Openings and holes are, by convention, LoD3/4 constructs. | Schema-valid, semantically wrong |
| c | **Envelope inconsistency** | The `CityModel/gml:boundedBy` envelope is a ~3 m × 14 m box ~100 km from the data; it neither encloses nor matches the geometry. | Schema-valid, breaks spatial indexing |

Defect **(b)** and **(a)** are entangled: because the surfaces are truly LoD3, the correct repair for
the mis-tagged shell (`lod2Solid → lod3Solid`) *also* moves it to a legal position relative to
`boundedBy` — one fix resolves both.

---

## 3. Stage 1 — the fixer (`diagnose_and_fix`, `validate_xsd`)

### Why a separate, non-destructive fixed file
The original file is the **primary evidence** of the reconstruction and must remain byte-for-byte
intact (audit trail, re-processing, provenance). The repaired copy is written next to it as
`<name>_fixed.gml` and is the only version that feeds ingestion. The example produces
[hamburg_citygml2_lod3_facades_holes_export_fixed.gml](input/citygml/hamburg/hamburg_citygml2_lod3_facades_holes_export_fixed.gml).

### What it does, and why each choice
- **(b) Retag to the true LoD.** A building is classified LoD3 iff its subtree contains a
  `bldg:opening` or a `gml:interior` ring — i.e. it *carries* LoD3 detail. For such buildings the
  shell `lod2Solid`→`lod3Solid` and every boundary `lod2MultiSurface`→`lod3MultiSurface`. This makes
  the *tag* agree with the *content*, which is what makes downstream LoD-aware queries honest.
- **(a) Canonicalise the geometry block.** Rather than special-casing the solid, the fixer re-sorts
  each building's **bldg-namespace** geometry children into the exact `AbstractBuildingType` order.
  The canonical order is **read from `building.xsd` at runtime** (`_derive_geom_order`), not
  hard-coded — the schema is the single source of truth. Non-geometry children (`creationDate`,
  `gen:*`, `bldg:function`, …) and the inherited `gml:boundedBy` envelope are left untouched; only
  the geometry elements move, and only into the slots they already occupied. This is robust to any
  ordering defect, not just the one observed.
- **(c) Recompute the envelope from the data.** The `CityModel` envelope is rebuilt as the true
  min/max over every `gml:posList` coordinate (honouring `srsName`/`srsDimension`), and only rewritten
  if the stated envelope fails to bracket the geometry. Per the project's **No-Hardcoding Rule**
  ([GRAPH.md](GRAPH.md)), the extent is derived, never literal.

### Proof, not promise
After writing the fixed file the script **re-validates it against the real XSDs** and records the
verdict in the summary (`xsd_validation_after_fix`). On the example: 35 issues found → all fixed →
**`valid: true, errors: 0`**, with coordinate/opening/hole counts identical to the source (no data
lost). Validation is best-effort: if the schema dir or network is unavailable it is reported as
skipped, never fatal.

---

## 4. Stage 2 — facade-focused enrichment (`parse_facades`, `write_facades`)

### Why *only* facade elements
The LoD2 `Building` already owns the authoritative thematic attributes — `function`, `roofType`,
`measuredHeight`, `storeysAboveGround`, the `gen:*Attribute` set, `externalReference`, `address`.
Re-ingesting those from a *reconstructed* source would either duplicate them or risk shadowing an
authoritative value with a lower-trust one. The script therefore parses **only the geometric facade**:

- semantic boundary surfaces (`WallSurface` / `RoofSurface` / `GroundSurface` …),
- their exterior rings and **interior rings (the window/door holes)**,
- their `bldg:opening` **Windows and Doors** with their own LoD3 geometry.

Everything thematic is deliberately ignored. The one shell element that *is* LoD3 geometry — the
`lodNSolid` — is skipped too: it is a redundant `xlink` aggregation of the same wall/roof/ground
polygons already captured as boundary surfaces, so ingesting it would add no facade knowledge.

### How the facade maps onto the graph — reuse, never reinvent
The geometry is written with the **exact node labels, relationship types, and property names that
`ingest_citygml.py` uses for native CityGML**, so LoD3 facades are first-class citizens of the same
model (queryable, exportable, renderable by the existing tools):

```text
(Building)  -[:HAS_LOD3_FACADE {order, source, source_type, method, method_version, confidence, lod}]->
(BoundarySurface {surface_type, lod:"3"})
    -[:HAS_POLYGON {order, via_xlink, orientation}]-> (GeometryPolygon)
            -[:HAS_EXTERIOR_RING]->              (GeometryRing {ring_type:"exterior", pos_list})
            -[:HAS_INTERIOR_RING {ring_index}]-> (GeometryRing {ring_type:"interior", pos_list})   # a hole
    -[:HAS_OPENING {order}]->                    (Opening {opening_type:"Window"|"Door"})
            -[:HAS_LOD_SURFACE {lod}]->          (GeometryMultiSurface)
                    -[:HAS_SURFACE_MEMBER {order}]-> (GeometryPolygon) -[:HAS_EXTERIOR_RING]-> (GeometryRing)
```

- `gml:posList` coordinates are stored **verbatim** on `GeometryRing.pos_list` (the lossless
  round-trip invariant).
- Every created node carries the shared `Entity` label (except `GeometryRing`, which is never a
  generic edge endpoint) so the index-backed `MATCH (:Entity {id})` lookups resolve — exactly as in
  the core ingester.

### Provenance model — the reason the attachment edge is *explicit*
The five mandatory provenance mechanisms from [GRAPH.md](GRAPH.md) are applied to the letter:

| Mechanism | In this script |
|-----------|----------------|
| **`source` on every node/edge** | `source = "lod3:<stem>"` on all facade nodes and the attachment edge. |
| **Namespaced, non-destructive scalars** | Anchor gets `lod3_enriched`, `lod3_surface_count`, `lod3_window_count`, `lod3_door_count`, `lod3_hole_count`, `lod3_confidence`, … under the `lod3_` prefix. Authoritative keys (`measured_height`, `function_code`, …) are **never** in the update map — verified: `measured_height` is unchanged after enrichment. |
| **A dedicated, explicit fusion edge** | `HAS_LOD3_FACADE` carries `source_type='reconstructed'`, `method`, `method_version`, `confidence`, `lod`. It is a *separate* relationship from the authoritative `HAS_BOUNDARY`, so a query can trust or discount the reconstructed geometry independently, and the authoritative LoD2 boundaries (if present) coexist untouched. This is the analogue of OSM's `ENRICHED_BY`. |
| **Carry confidence** | `confidence` (default 0.9, `--confidence`) on the edge and the anchor sentinel — a placeholder for the reconstruction method's reported score. |
| **Own spatial layer** | LoD3 surfaces register in an `lod3_features` R-tree layer, leaving the authoritative `features` layer — and every existing spatial query — untouched. |

Trust class: **`reconstructed`** — one of the five documented classes (authoritative / crowd-sourced /
predicted / **reconstructed** / sensed).

### Idempotency & surgical reset (`reset_lod3`)
All additions live under a single marker label **`Lod3Facade`** and the `lod3:` id prefix, so a re-run
strips *only* this source before rewriting:

```cypher
MATCH (b:Building) WHERE b.lod3_source = $src REMOVE b.lod3_enriched, b.lod3_surface_count, …
MATCH (n:Lod3Facade) WHERE n.source = $src DETACH DELETE n
CALL spatial.removeLayer('lod3_features')
```

The authoritative graph is never touched. Re-running the example is stable (476 facade nodes, 22
attachment edges, unchanged).

### Coverage honesty — the hardest correctness property
An LoD3 facade only *enriches* if the LoD2 building it describes is already in the graph. The script
therefore:
1. reads the candidate building ids from the (fixed) file,
2. asks the graph which exist (`find_present`),
3. **parses and writes facades for matched anchors only** — never fabricating a `Building`, never
   leaving dangling geometry (verified: 0 orphaned surfaces),
4. **reports the rest** as `buildings_not_found` with their ids and a `coverage_rate`.

Absence of `lod3_enriched` therefore means *"no LoD3 coverage"*, distinguishable from a negative fact —
the property that makes coverage-aware queries and honest infeasibility answers possible. On the
example, the loaded HafenCity LoD2 subset overlaps the LoD3 file on 1 of 17 buildings; the other 16
are named in the report so the operator knows exactly which LoD2 tiles to ingest before re-running.

---

## 5. Graph additions at a glance

**New relationship**

| Edge | Endpoints | Properties |
|------|-----------|------------|
| `HAS_LOD3_FACADE` | `(Building)→(BoundarySurface)` | `order`, `source`, `source_type='reconstructed'`, `method`, `method_version`, `confidence`, `lod='3'` |

**Reused node labels** (each also tagged `:Lod3Facade` for surgical reset; `:Entity` per convention):
`BoundarySurface` (`surface_type`, `lod='3'`, `source_type`), `Opening` (`opening_type`),
`GeometryMultiSurface`, `GeometryPolygon`, `GeometryRing` (`pos_list`, `ring_type`).

**Reused geometry edges:** `HAS_POLYGON`, `HAS_OPENING`, `HAS_LOD_SURFACE`, `HAS_SURFACE_MEMBER`,
`HAS_EXTERIOR_RING`, `HAS_INTERIOR_RING` — identical to native CityGML.

**Anchor sentinels (namespaced):** `lod3_enriched`, `lod3_surface_count`, `lod3_opening_count`,
`lod3_window_count`, `lod3_door_count`, `lod3_hole_count`, `lod3_source`, `lod3_confidence`,
`lod3_method`, `lod3_method_version`, `lod3_source_type`.

---

## 6. Scientific problems solved

1. **Schema-conformance repair as a reproducible, provable transform.** The defective export is
   turned into a validated CityGML 2.0 file by a transform that (i) preserves the original, (ii)
   derives the canonical element order from the schema itself rather than assuming it, (iii) recomputes
   the spatial extent from the data, and (iv) **re-validates and reports the verdict**. The fix is
   auditable, not asserted.

2. **LoD-semantics reconciliation.** It resolves the common "LoD3 content in LoD2 clothing"
   inconsistency by classifying a building's true LoD from the presence of openings/holes and
   retagging accordingly — so that the LoD level a query sees matches the geometry it gets.

3. **Multi-source, multi-trust fusion without contamination.** A *reconstructed* observation is fused
   with an *authoritative* one on the same real-world object such that the two remain independently
   addressable, comparable, and revocable. No authoritative value is overwritten; the reconstructed
   geometry is reachable only through a confidence-carrying edge; a reset removes it cleanly. This is
   the general pattern for adding any future source (predictions, sensors) to the graph.

4. **Honest coverage under partial overlap.** By enriching only anchored buildings and explicitly
   reporting the unmatched remainder, the graph never conflates *"not observed"* with *"observed
   negative"* — a prerequisite for defensible city-scale statistics over a heterogeneously-covered
   graph.

5. **Facade-level structure becomes queryable knowledge.** Windows, doors, and wall openings —
   previously locked inside XML geometry — become graph objects, enabling analyses such as
   window-to-wall ratio, opening counts per facade orientation, and cross-source geometric comparison
   between the reconstructed LoD3 shell and the authoritative LoD2 solid.

---

## 7. Example cross-source queries

```cypher
// Buildings with a reconstructed LoD3 facade, and how confident we are
MATCH (b:Building)-[e:HAS_LOD3_FACADE]->(:BoundarySurface)
RETURN b.id, b.measured_height AS authoritative_height,
       count(e) AS lod3_surfaces, max(e.confidence) AS confidence;

// Window-to-wall opening ratio per building (facade richness)
MATCH (b:Building {lod3_enriched:true})
RETURN b.id, b.lod3_window_count, b.lod3_door_count, b.lod3_hole_count,
       b.lod3_window_count * 1.0 / b.lod3_surface_count AS windows_per_surface;

// Compare authoritative height with the reconstructed facade extent
MATCH (b:Building)-[:HAS_LOD3_FACADE]->(:BoundarySurface)-[:HAS_POLYGON]->
      (:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(r:GeometryRing)
RETURN b.id, b.measured_height, count(r) AS lod3_rings;

// Coverage-aware: which loaded buildings still lack LoD3 (honest absence)
MATCH (b:Building) WHERE b.lod3_enriched IS NULL RETURN count(b) AS not_yet_reconstructed;
```

---

## 8. Usage

```bash
# Fix + enrich (Neo4j running; one Dataset present -> auto-detected)
py enrich_lod3_facades.py "input/citygml/hamburg/hamburg_citygml2_lod3_facades_holes_export.gml"

# Just produce the repaired *_fixed.gml (no database needed)
py enrich_lod3_facades.py path/to/lod3.gml --fix-only

# Named target Dataset; custom confidence; skip the XSD re-validation
py enrich_lod3_facades.py path/to/lod3.gml --dataset hamburg_citygml2_lod2_2025 \
                          --confidence 0.85 --no-validate
```

Prerequisites: `lxml`, `neo4j`; Neo4j running with the Spatial plugin (for the `lod3_features`
layer); the LoD2 buildings already ingested via [ingest_citygml.py](ingest_citygml.py). The optional
XSD re-validation needs the schemas under [citygml-model/xsd_citygml2/](citygml-model/xsd_citygml2/)
and network access for GML 3.1.1. Output report: `output/<stem>_lod3_summary.md`.
