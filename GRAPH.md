# LLM Knowledge Graph System — Instructions

## General Rules

- The LLM may read, write, edit, or delete files if necessary, but **only** within the current working directory (where this `GRAPH.md` is located) and its subfolders. Never access or modify files above this directory.

---

## LLM Query Workflow

When the user asks a question about the data (e.g. "how many buildings are over 20m?", "what is the nearest building to X?", "list all buildings with roof type Y?"):

1. **Read this file** (`GRAPH.md`) to recall the graph schema, node/relationship types, and available properties.
2. **Write a Cypher query** that answers the question, following the Query / Traversal Rules below (spatial index first for location queries, scalar `WHERE` for attribute filters).
3. **Connect to Neo4j** using the connection details above and execute the query via the `py` launcher:

   ```python
   py -c "
   import time
   from neo4j import GraphDatabase
   driver = GraphDatabase.driver('bolt://localhost:7687', auth=('neo4j', 'neo4jneo4j'))
   with driver.session() as session:
       t0 = time.perf_counter()
       result = session.run('...')
       rows = [dict(r) for r in result]
       elapsed = time.perf_counter() - t0
   driver.close()
   for r in rows:
       print(r)
   print(f'Query time: {elapsed:.3f}s')
   "
   ```

4. **Display the Cypher query** in a fenced `cypher` code block.
   - All literal values (IDs, coordinates, thresholds) must be **inlined directly** into the query — no `$param` placeholders.
   - The displayed query must be **copy-pasteable** into the Neo4j Browser and run as-is without modification.
5. **Display the results** clearly — as a table, list, or prose depending on what fits best.
6. **Display the query execution time** in seconds (e.g. `Query time: 0.042 s`) on its own line after the results.

Do this automatically whenever a query question is asked — do not wait to be told to connect.

---

## File Ingestion Rules

- Files are **read-only** — never modify source files in `input/`.
- Supported formats: PDF, TXT, Markdown, CityGML, and similar text-bearing or geospatial documents.
- Each file may be chunked or parsed into entities and relationships before graph insertion.

---

## Graph Model

### Nodes

- Each node represents a **distinct entity or concept** extracted from the source material.
- Every node has:
  - A **label** — a semantic category (e.g., `Person`, `Place`, `Event`, `Organization`, `Concept`, `Document`)
  - A **`name`** property — the primary identifier string
  - An **`id`** property — a stable unique identifier (use a slug or UUID)
  - Optional additional properties capturing attributes (e.g., `founded`, `population`, `description`)

### Edges (Relationships)

- Each edge is **directed** from a source node to a target node.
- Every edge has:
  - A **type/label** — a semantically meaningful verb or relation (e.g., `LOCATED_IN`, `PART_OF`, `FOUNDED_BY`, `BORDERS`, `LEADS_TO`, `MENTIONS`, `INSTANCE_OF`)
  - Optional **properties** on the relationship (e.g., `since`, `confidence`, `source_file`)
- Edge direction encodes the logical direction of the relationship (e.g., `Berlin -[LOCATED_IN]-> Germany`, not the reverse).

---

## Ingestion Process

1. Parse the source file and extract candidate entities and relationships using an LLM.
2. Deduplicate nodes by normalized `name` and `label` — merge into existing nodes if a match exists.
3. Create directed relationships between nodes with meaningful labels.
4. Tag each node and edge with a `source` property indicating the originating file.
5. Write a summary of what was ingested to `output/<filename>_summary.md`.

---

## CityGML Ingestion Rules

CityGML files require special handling to preserve all geometric and spatial information in the graph.

### No-Hardcoding Rule

**No coordinate value, code value, CRS string, LOD level, or spatial extent may ever be written as a literal in ingestion code or Cypher.** Every value must be read from the file at runtime:

- `bbox_*` coordinates — read from `gml:boundedBy` envelopes at runtime.
- CRS / `srsName` — read from `gml:Envelope/@srsName`.
- LOD level — detected by scanning which `bldg:lod*` elements are present; check both `*Solid` and `*MultiSurface` variants per LOD.
- Building function / roof type codes — resolved at runtime using the ALKIS reference tables below.
- `gen:stringAttribute` values — iterate and store **all** of them; never select by name.
- District / city — derived from `Gemeindeschluessel` gen:stringAttribute (prefix-matched against known keys) or from the filename stem as fallback.
- `format` version — read from the `xmlns` namespace URI of the root `CityModel` element.

### CityGML 2.0 modules

CityGML 2.0 defines 14 modules. 10 are ingested as graph nodes; the remaining 4 are not stored directly.

| Module | Neo4j label(s) | Geometry style | Supported |
| ------ | -------------- | -------------- | --------- |
| Building | `Building` | Solid + BoundarySurfaces | ✓ |
| Transportation | `Road` `Railway` `Track` `Square` `TransportationComplex` | lod*MultiSurface | ✓ |
| Vegetation | `SolitaryVegetationObject` `PlantCover` | lod*Geometry / ImplicitGeometry | ✓ |
| WaterBody | `WaterBody` | Solid or lod*MultiSurface + BoundarySurfaces | ✓ |
| LandUse | `LandUse` | lod*MultiSurface | ✓ |
| CityFurniture | `CityFurniture` | lod*Geometry / ImplicitGeometry | ✓ |
| Relief | `ReliefFeature` | TIN / MassPoint / Breakline | ✓ |
| Bridge | `Bridge` | Solid or lod*MultiSurface + BoundarySurfaces | ✓ |
| Tunnel | `Tunnel` | Solid or lod*MultiSurface + BoundarySurfaces | ✓ |
| CityObjectGroup | `CityObjectGroup` | lod*Geometry | ✓ |
| Appearance | — | — | — |
| Generics | — | gen:*Attribute values stored on feature nodes | — |
| TexturedSurface | — | deprecated in CityGML 2.0 | — |
| Core | — | abstract base types, not stored directly | — |

All concrete feature types (`Building` + all `CityObject` subtypes) that carry a `gml:boundedBy` envelope are registered in the **Neo4j Spatial R-tree** (layer `features`) after ingestion, enabling `spatial.bbox`, `spatial.intersects`, and `spatial.withinDistance` queries across the full dataset regardless of feature type.

### Graph schema (complete)

```text
── Spatial context ──────────────────────────────────────────────────────────────
(Dataset)                  -[:COVERS]->                         (District)
(District)                 -[:PART_OF]->                        (City)

── Building module ──────────────────────────────────────────────────────────────
(Building)                 -[:PART_OF]->                        (Dataset)
(Building)                 -[:LOCATED_IN]->                     (District)
(Building)                 -[:HAS_FUNCTION]->                   (BuildingFunction)
(Building)                 -[:HAS_ROOF_TYPE]->                  (RoofType)
(Building)                 -[:HAS_LOD_SOLID]->                  (GeometrySolid)
(Building)                 -[:HAS_BOUNDARY {order}]->           (BoundarySurface)
(Building)                 -[:HAS_TERRAIN_INTERSECTION]->       (TerrainIntersection)
(Building)                 -[:HAS_LOD_SURFACE {lod, role}]->    (GeometryMultiSurface)   # lod0FootPrint/lod0RoofEdge/lodN MS
(Building)                 -[:HAS_INTERIOR_ROOM {order}]->      (Room)                   # LoD4
(Building)                 -[:HAS_OUTER_INSTALLATION {order}]-> (BuildingInstallation)
(Building)                 -[:HAS_INTERIOR_INSTALLATION {order}]-> (BuildingInstallation)  # interiorBuildingInstallation
(Building)                 -[:HAS_LOD_MULTICURVE {lod, order}]-> (GeometryLineString)    # bldg:lodN MultiCurve (≠ TerrainIntersection)
(Building)                 -[:HAS_BUILDING_PART {order}]->      (BuildingPart)           # consistsOfBuildingPart
(Building)                 -[:NEARBY_BUILDING {distance_m}]->   (Building)

── BuildingPart (recursive AbstractBuilding, nested under a Building or BuildingPart) ──
(BuildingPart)             -[:HAS_BUILDING_PART {order}]->      (BuildingPart)           # nested parts
# A BuildingPart reuses every Building geometry edge above (HAS_BOUNDARY, HAS_LOD_SOLID,
# HAS_LOD_SURFACE, HAS_TERRAIN_INTERSECTION, HAS_LOD_MULTICURVE, HAS_INTERIOR_ROOM,
# HAS_OUTER_INSTALLATION, HAS_INTERIOR_INSTALLATION). It carries the BuildingPart label
# (+ Entity) only — never Building — so `MATCH (b:Building)` domain counts stay clean.

── Building interior (LoD3/4) ─────────────────────────────────────────────────────
(Room)                     -[:HAS_BOUNDARY {order}]->           (BoundarySurface)
(Room)                     -[:HAS_LOD_SOLID]->                  (GeometrySolid)
(Room)                     -[:HAS_INTERIOR_FURNITURE {order}]-> (BuildingFurniture)
(Room)                     -[:HAS_ROOM_INSTALLATION {order}]->  (BuildingInstallation)
(BuildingFurniture)        -[:HAS_LOD_GEOMETRY {lod}]->         (GeometryMultiSurface)
(BuildingInstallation)     -[:HAS_LOD_GEOMETRY {lod}]->         (GeometryMultiSurface)
(BuildingInstallation)     -[:HAS_BOUNDARY {order}]->           (BoundarySurface)
(BoundarySurface)          -[:HAS_OPENING {order}]->            (Opening)                # Door / Window
(Opening)                  -[:HAS_LOD_SURFACE {lod}]->          (GeometryMultiSurface)

── Transportation module ────────────────────────────────────────────────────────
(Road)                     -[:PART_OF]->                        (Dataset)
(Railway)                  -[:PART_OF]->                        (Dataset)
(Track)                    -[:PART_OF]->                        (Dataset)
(Square)                   -[:PART_OF]->                        (Dataset)
(TransportationComplex)    -[:PART_OF]->                        (Dataset)
(Road|Railway|Track|Square|TransportationComplex) -[:HAS_LOD_SURFACE {lod}]-> (GeometryMultiSurface)

── Vegetation module ────────────────────────────────────────────────────────────
(SolitaryVegetationObject) -[:PART_OF]->                        (Dataset)
(PlantCover)               -[:PART_OF]->                        (Dataset)
(SolitaryVegetationObject|PlantCover) -[:HAS_LOD_SURFACE {lod}]-> (GeometryMultiSurface)

── WaterBody module ─────────────────────────────────────────────────────────────
(WaterBody)                -[:PART_OF]->                        (Dataset)
(WaterBody)                -[:HAS_LOD_SOLID]->                  (GeometrySolid)
(WaterBody)                -[:HAS_BOUNDARY {order}]->           (BoundarySurface)

── LandUse module ───────────────────────────────────────────────────────────────
(LandUse)                  -[:PART_OF]->                        (Dataset)
(LandUse)                  -[:HAS_LOD_SURFACE {lod}]->          (GeometryMultiSurface)

── CityFurniture module ─────────────────────────────────────────────────────────
(CityFurniture)            -[:PART_OF]->                        (Dataset)
(CityFurniture)            -[:HAS_LOD_SURFACE {lod}]->          (GeometryMultiSurface)

── Relief module ────────────────────────────────────────────────────────────────
(ReliefFeature)            -[:PART_OF]->                        (Dataset)
(ReliefFeature)            -[:HAS_LOD_SURFACE {lod}]->          (GeometryMultiSurface)   # TIN: ms.geometry_type='TIN' + tin_relief_id/triangulated_surface_id

── Bridge / Tunnel modules ───────────────────────────────────────────────────────
(Bridge|Tunnel)            -[:PART_OF]->                        (Dataset)
(Bridge|Tunnel)            -[:HAS_LOD_SOLID]->                  (GeometrySolid)
(Bridge|Tunnel)            -[:HAS_BOUNDARY {order}]->           (BoundarySurface)
(Bridge|Tunnel)            -[:HAS_NESTED {prop, order}]->       (NestedFeature)          # outerBridgeInstallation / BridgeConstructionElement / outerTunnelInstallation …
(NestedFeature)            -[:HAS_LOD_GEOMETRY {lod}]->         (GeometryMultiSurface)
(NestedFeature)            -[:HAS_BOUNDARY {order}]->           (BoundarySurface)

── CityObjectGroup module ───────────────────────────────────────────────────────
(CityObjectGroup)          -[:PART_OF]->                        (Dataset)
(CityObjectGroup)          -[:HAS_GROUP_MEMBER {order, role, via_xlink}]-> (CityObject)  # grp:groupMember xlink

── Generics module ───────────────────────────────────────────────────────────────
(GenericCityObject)        -[:PART_OF]->                        (Dataset)
(GenericCityObject)        -[:HAS_LOD_SURFACE {lod}]->          (GeometryMultiSurface)

── Geometry (shared by all feature types) ───────────────────────────────────────
(GeometrySolid)            -[:HAS_SURFACE_MEMBER {order, via_xlink, orientation}]-> (GeometryPolygon | GeometryCompositeSurface)
(GeometryMultiSurface)     -[:HAS_SURFACE_MEMBER {order, via_xlink, orientation}]-> (GeometryPolygon)
(GeometryCompositeSurface) -[:HAS_SURFACE_MEMBER {order}]->     (GeometryPolygon)
(BoundarySurface)          -[:HAS_POLYGON {order, via_xlink, orientation}]-> (GeometryPolygon)
(GeometryPolygon)          -[:HAS_EXTERIOR_RING]->              (GeometryRing)
(GeometryPolygon)          -[:HAS_INTERIOR_RING {ring_index}]-> (GeometryRing)
(TerrainIntersection)      -[:HAS_LINE {order}]->               (GeometryLineString)
```

**XLinks are explicit, marked edges.** A CityGML `xlink:href` geometry reference
becomes a real `HAS_SURFACE_MEMBER` / `HAS_POLYGON` edge to the shared target node
(merged on `gml:id`), with **`via_xlink = true`** so export re-emits `xlink:href`
rather than inlining the geometry; inline members carry `via_xlink = false`/absent.
A `gml:OrientableSurface` wrapper records its `orientation` (`+`/`-`) on the edge.
A `gml:CompositeSurface` that is referenced by xlink (e.g. a `lodNSolid` member) is
materialized as a `GeometryCompositeSurface` node so the reference is a first-class
edge. This makes shared geometry queryable in the graph and round-trips the exact
reference list on export.

All non-Building feature nodes carry two labels: the module-specific label (e.g. `Bridge`) and the shared base label `CityObject`. Query either way:

- `MATCH (n:Bridge)` — works when APOC was available at ingest time
- `MATCH (n:CityObject {feature_type: 'Bridge'})` — always works

**Shared `Entity` label (ingestion index anchor).** Every node that can be the source or target of a *generic*, label-agnostic relationship — one whose endpoint label is not known when the edge batch is flushed (e.g. `HAS_LOD_SOLID`, `HAS_BOUNDARY`, `HAS_LOD_GEOMETRY`, `HAS_NESTED`, `HAS_SURFACE_MEMBER`, `HAS_GROUP_MEMBER`) — also carries the extra label `Entity`. This includes `Building`, `BuildingPart`, every `CityObject`/module feature, `Room`, `BuildingInstallation`, `BuildingFurniture`, `Opening`, `NestedFeature`, `BoundarySurface`, `GeometrySolid`, `GeometryMultiSurface`, `GeometryCompositeSurface`, `GeometryPolygon`, `BuildingFunction`, and `RoofType`. The `Building → BuildingPart`, `HAS_INTERIOR_INSTALLATION`, and `HAS_LOD_MULTICURVE` edges all resolve their source through the `Entity(id)` index, so the same flush works whether the parent is a `Building` or a nested `BuildingPart`. It lets those edge-creation `MATCH (a:Entity {id: …})` lookups resolve through a single `Entity(id)` index instead of an all-nodes scan (Neo4j `id` indexes are per-label, so a label-less `MATCH (a {id})` cannot use any of them — the root cause of slow large-file ingestion). `Entity` is purely a plumbing/index label, orthogonal to the semantic labels; ignore it in domain queries. The high-cardinality leaf nodes that are never a generic edge endpoint — `GeometryRing`, `GeometryLineString`, `TerrainIntersection`, and `Dataset` — deliberately do **not** carry `Entity`, keeping the index small.

Relationship properties carry document-order counters (`order`, `ring_index`) so the exact CityGML element sequence can be restored on export.

**Lossless geometry-wrapper round-trip.** The compact schema flattens geometry to `GeometryPolygon`/`GeometryRing`, but some CityGML files nest polygons in `gml:CompositeSurface` or `gml:OrientableSurface` wrappers, and solids/MultiSurfaces reference members by `xlink:href`. These are reproduced from **explicit edge properties**, not JSON blobs:

- `HAS_SURFACE_MEMBER` / `HAS_POLYGON` edges carry `via_xlink` (the member was an `xlink:href` → re-emit as href, not inline) and `orientation` (the member was wrapped in a `gml:OrientableSurface` → re-emit the wrapper with its `+`/`-`). `order` preserves member sequence.
- `gml:CompositeSurface` referenced by xlink (e.g. a `lodNSolid` member) is materialized as a `GeometryCompositeSurface` node so the reference is a first-class, labelled edge; `composite_surface_id` (on `BoundarySurface`/`GeometryMultiSurface`/`GeometrySolid`) still records the id of an inline wrapping `gml:CompositeSurface` so the wrapper element re-emits with its original gml:id.

Other lossless-only properties: `Building.address_xml` (verbatim xAL subtree), `*_codespace` (codeSpace of coded scalars), `_gen_attrs_map` values `{name,type}` (preserve `gen:*Attribute` type + uom). Validate completeness with `eval/roundtrip_census.py` (appearance module excluded by design).

See [LOSSLESS_ROUNDTRIP.md](LOSSLESS_ROUNDTRIP.md) for the full catalogue of CityGML round-trip challenges (LoD0–4) and how each was solved — read it before changing ingest/export geometry handling.

### Node types and properties

#### Dataset

| Property | Source |
| -------- | ------ |
| `id` | slug of filename stem |
| `name`, `source` | filename |
| `format` | root namespace URI → `"CityGML 2.0"` |
| `crs` | `gml:Envelope/@srsName` |
| `srs_dimension` | `gml:Envelope/@srsDimension` (default 3) |
| `lod` | highest `lod*` element found across all modules |
| `bbox_lower_corner`, `bbox_upper_corner` | raw corner strings |
| `bbox_min/max_x/y/z` | parsed floats |

#### Building

| Property | Source |
| -------- | ------ |
| `id`, `name` | `gml:id` |
| `creation_date` | `cg:creationDate` |
| `function_code` | `bldg:function` text |
| `roof_type_code` | `bldg:roofType` text |
| `measured_height` | `bldg:measuredHeight` (float, m) |
| `measured_height_uom` | `bldg:measuredHeight/@uom` |
| `storeys_above_ground` | `bldg:storeysAboveGround` (int) |
| `storeys_below_ground` | `bldg:storeysBelowGround` (int) |
| `storey_heights_above_ground` (+`_uom`) | `bldg:storeyHeightsAboveGround` (MeasureOrNullList, verbatim) |
| `storey_heights_below_ground` (+`_uom`) | `bldg:storeyHeightsBelowGround` (MeasureOrNullList, verbatim) |
| `class_code`, `usage_code` (+`_codespace`) | `bldg:class`, `bldg:usage` |
| `year_of_construction`, `year_of_demolition` | `bldg:yearOfConstruction/Demolition` |
| `relative_to_terrain`, `relative_to_water` | `cg:relativeToTerrain/Water` |
| `termination_date` | `cg:terminationDate` |
| `address_xml` | `bldg:address` xAL subtree (verbatim) |
| `external_information_system` | `cg:externalReference/cg:informationSystem` (query convenience) |
| `external_object_name` | `cg:externalReference/cg:externalObject/cg:name` (query convenience) |
| `external_reference_xml` | JSON list of full `cg:externalReference` subtrees (verbatim, lossless, multiple) |
| `metadata_xml` | JSON list of `gml:metaDataProperty` subtrees (verbatim) |
| `location_xml` | `gml:location` subtree (verbatim) |
| `generalizes_to_xml` | JSON list of `cg:generalizesTo` subtrees (verbatim) |
| `ade_xml` | JSON list of ADE / `gml:genericADEElement` subtrees — any direct child in a non-CityGML namespace (verbatim) |
| `bbox_lower_corner`, `bbox_upper_corner` | raw corner strings |
| `bbox_min/max_x/y/z` | parsed floats |
| `center_x`, `center_y` | centroid of bbox |
| `ground_z` | = `bbox_min_z` |
| `bbox_wkt` | 2D WKT POLYGON for spatial R-tree |
| `location` | Neo4j `point({x, y})` for distance calc |
| `_gen_attrs_map` | JSON `{lowercase_slug: OriginalName}` — preserves CamelCase names for export |
| *(all gen:*Attribute values)* | stored as `lowercase_slug` properties |
| `description` | generated text for semantic search |
| `embedding` | 1536-dim vector (optional, GenAI plugin) |
| `community_id` | Louvain id (optional, GDS plugin) |
| `source` | filename |

All `gen:stringAttribute`, `gen:intAttribute`, `gen:doubleAttribute`, `gen:dateAttribute`, `gen:uriAttribute`, and `gen:measureAttribute` elements are stored as properties using a lowercase slug of the `name` attribute as the key.  The `_gen_attrs_map` JSON records the original CamelCase names in insertion order for faithful round-trip export.

**Shared "common extras" on every feature.** `termination_date` plus the verbatim subtree properties `metadata_xml`, `location_xml`, `generalizes_to_xml`, `external_reference_xml`, and `ade_xml` are captured by `_capture_common_extras` for **every** CityGML feature node — `Building`, `BuildingPart`, all `CityObject` modules, and `BoundarySurface` — because these come from the shared `_CityObject`/`_Feature`/`_GML` base types. `ade_xml` is detected structurally: any direct-child element in a namespace the ingester does not model natively (i.e. not in `NS`) is preserved verbatim, the same proven, fully lossless pattern as `Building.address_xml`. Element order within a feature is not significant for the lossless gate (`roundtrip_census.py` compares element/attribute/text/coordinate multisets, not sequence).

#### BuildingPart

`bldg:BuildingPart` is itself an `AbstractBuilding`, so it carries the **same property set as `Building`** (all scalars + verbatim extras above) and reuses the entire Building geometry pipeline (boundaries, solid, terrain, `lodN` MultiCurve, multisurfaces, interior rooms/installations). It is linked from its parent via `HAS_BUILDING_PART {order}` and may nest further parts recursively. A `BuildingPart` node carries the `BuildingPart` label (plus the shared `Entity` label) but **never** the `Building` label, so it stays out of `MATCH (b:Building)` domain queries, the R-tree, and `PART_OF Dataset` — it is reached only by traversing `HAS_BUILDING_PART` from a Building. Its `gml:boundedBy` envelope (`bbox_lower_corner`/`bbox_upper_corner` + `has_bbox_envelope`) is preserved when present but parts are not registered spatially.

#### CityObject

All non-building top-level CityGML features (Transportation, Vegetation, WaterBody, LandUse, CityFurniture, Relief, Bridge, Tunnel, CityObjectGroup). Each node has label `CityObject` plus a module-specific label when APOC is available.

| Property | Source |
| -------- | ------ |
| `id`, `name` | `gml:id` |
| `feature_type` | element local name (e.g. `"Road"`, `"WaterBody"`, `"Bridge"`) |
| `creation_date` | `cg:creationDate` (optional) |
| `class` | `{module}:class` (optional) |
| `function` | `{module}:function` (optional) |
| `usage` | `{module}:usage` (optional) |
| `bbox_lower_corner`, `bbox_upper_corner` | raw corner strings (when present) |
| `bbox_min/max_x/y/z`, `center_x/y` | parsed floats (when bbox present) |
| `bbox_wkt` | 2D WKT POLYGON for spatial R-tree (when bbox valid, i.e. min < max) |
| `location` | Neo4j `point({x, y})` for distance calc (when bbox valid) |
| `_gen_attrs_map` | JSON for gen:*Attribute original names |
| *(all gen:*Attribute values)* | lowercase slug properties |
| `source` | filename |

Module-specific additional properties:

| Module (`feature_type`) | Extra properties |
| ---------------------- | --------------- |
| Transportation (Road, Railway, Track, Square, TransportationComplex) | `surface_material` |
| Vegetation (SolitaryVegetationObject, PlantCover) | `species`, `height`, `trunk_diameter`, `crown_diameter`, `average_height` |
| WaterBody | `water_level` |
| Bridge | `year_of_construction`, `year_of_demolition`, `is_movable` |
| Tunnel | `year_of_construction`, `year_of_demolition` |
| ReliefFeature | `lod` (XML attribute) |

#### BoundarySurface

Maps to building surfaces (`bldg:*Surface`) and module boundary surfaces:
`wtr:WaterSurface/WaterGroundSurface/WaterClosureSurface`,
`brid:*/tun:*` surface types (GroundSurface, WallSurface, RoofSurface, etc.).
Used by both `Building` and `CityObject` nodes via `HAS_BOUNDARY`.

Maps to `bldg:GroundSurface`, `bldg:WallSurface`, `bldg:RoofSurface`, `bldg:ClosureSurface`, `bldg:FloorSurface`, `bldg:InteriorWallSurface`, `bldg:CeilingSurface`, `bldg:OuterFloorSurface`, `bldg:OuterCeilingSurface`.

| Property | Source |
| -------- | ------ |
| `id` | `gml:id` of the surface element |
| `surface_type` | element local name (e.g. `"WallSurface"`) |
| `lod` | detected from child `bldg:lod{N}MultiSurface` |
| `multi_surface_id` | `gml:MultiSurface/@gml:id` (for export) |
| `creation_date` | `cg:creationDate` (optional) |
| `_gen_attrs_map` | JSON for surface's gen:*Attribute names |
| *(all surface gen:*Attribute values)* | lowercase slug properties |
| `source` | filename |

#### GeometrySolid

| Property | Source |
| -------- | ------ |
| `id` | `gml:Solid/@gml:id` |
| `lod` | `"1"`, `"2"`, `"3"`, or `"4"` |
| `composite_surface_id` | `gml:CompositeSurface/@gml:id` (for export) |
| `source` | filename |

#### GeometryMultiSurface

Used for direct `lod*MultiSurface` geometry on non-building city objects (Transportation, LandUse, WaterBody direct surface, etc.) and for implicit geometry prototypes (Vegetation, CityFurniture). Linked from `CityObject` via `HAS_LOD_SURFACE {lod}`.

| Property | Source |
| -------- | ------ |
| `id` | `gml:MultiSurface/@gml:id` (auto-generated if absent) |
| `lod` | detected from parent `lod*MultiSurface` or `lod*ImplicitRepresentation` wrapper |
| `geometry_type` | `"TIN"` for ReliefFeature TIN components (optional) |
| `is_implicit` | `true` when geometry comes from `core:ImplicitGeometry` |
| `transformation_matrix` | 16 row-major floats from `core:transformationMatrix` (implicit only) |
| `reference_point` | 3 floats from `core:referencePoint/gml:Point/gml:pos` (implicit only) |
| `prototype_href` | GML id of shared prototype `MultiSurface` when `relativeGMLGeometry` uses `xlink:href` instead of inline geometry (transient — removed after prototype resolution) |
| `source` | filename |

**Implicit geometry note:** `GeometryRing.pos_list` stores **local prototype coordinates** (unit-scale, typically sub-metre range). To get world coordinates in the dataset CRS, apply the transform: `P_world = M * [P_local; 1] + P_reference`, where `M` is the 4×4 row-major `transformation_matrix` and `P_reference` is the `reference_point` vector.

**xlink:href prototype resolution:** When multiple features reference the same implicit geometry prototype via `<relativeGMLGeometry xlink:href="#id"/>`, each referencing feature gets its own `GeometryMultiSurface` node (with `prototype_href` set to the prototype GML id). After all features are flushed, a post-pass Cypher query copies `HAS_SURFACE_MEMBER` relationships from the prototype `GeometryMultiSurface` to each referencing one, so every feature ultimately has the same polygon members. The `prototype_href` property remains on the node after resolution as a provenance marker.

#### GeometryPolygon

| Property | Source |
| -------- | ------ |
| `id` | `gml:Polygon/@gml:id` (auto-generated if absent) |
| `source` | filename |

#### GeometryRing

| Property | Source |
| -------- | ------ |
| `id` | `gml:LinearRing/@gml:id` (auto-generated if absent) |
| `pos_list` | `gml:posList` text — **verbatim, never reformatted** |
| `srs_dimension` | `gml:posList/@srsDimension` (default 3) |
| `ring_type` | `"exterior"` or `"interior"` |
| `source` | filename |

#### TerrainIntersection

| Property | Source |
| -------- | ------ |
| `id` | `{building_id}_terrain_lod{N}` |
| `lod` | detected from `bldg:lod{N}TerrainIntersection` |
| `source` | filename |

#### GeometryLineString

| Property | Source |
| -------- | ------ |
| `id` | `gml:LineString/@gml:id` (auto-generated if absent) |
| `pos_list` | `gml:posList` text — **verbatim** |
| `srs_dimension` | `gml:posList/@srsDimension` (default 3) |
| `source` | filename |

#### Lookup nodes

Shared nodes that group buildings by semantic category (only created for codes actually present in the file):

- `BuildingFunction {id, name, code}` — one per unique `bldg:function` value; `name` from ALKIS table
- `RoofType {id, name, code}` — one per unique `bldg:roofType` value
- `City {id, name}` / `District {id, name}` — one each, derived from `Gemeindeschluessel`

### Ingestion rules for geometry

1. **Process BoundarySurfaces before the Solid** so polygon nodes exist before xlink:href resolution.
2. For each `bldg:boundedBy` element, find the concrete surface child (`bldg:*Surface`), read its `gen:*Attribute` elements, detect the LOD from the child `bldg:lod{N}MultiSurface`, then create `BoundarySurface → GeometryPolygon → GeometryRing` chains.
3. For `bldg:lod{N}Solid`, parse the `gml:CompositeSurface/gml:surfaceMember` list.  If a member has `xlink:href="#id"`, link the Solid to the existing `GeometryPolygon {id}`.  If a member contains an inline `gml:Polygon`, create a new polygon node and link it.
4. `gml:posList` text is stored verbatim on `GeometryRing`/`GeometryLineString` — never round, truncate, or reformat coordinates.
5. `order` and `ring_index` relationship properties must be set so the export can reconstruct the original element sequence exactly.

### What must never be dropped

- `gml:boundedBy` envelope of `CityModel` → stored on `Dataset`.
- `gml:boundedBy` envelope of each `bldg:Building` → stored as `bbox_*` on `Building`.
- The `crs` (`srsName`) → stored on `Dataset`; used by every spatial query and export.
- `bbox_min_z` (ground elevation) and `bbox_max_z` (roof peak) → stored separately.
- All `gml:id` values on geometric elements — stable identifiers referenced by `xlink:href`.
- All `gml:posList` strings — stored verbatim.
- All `bldg:boundedBy` surfaces → each becomes a `BoundarySurface` node with its own gen:*Attribute properties.
- The `composite_surface_id` on `GeometrySolid` and `multi_surface_id` on `BoundarySurface` — needed to restore the original inner-wrapper gml:id values on export.
- The `HAS_BOUNDARY.order` and `HAS_POLYGON.order` and `HAS_SURFACE_MEMBER.order` relationship properties — needed to restore CityGML element sequence.

### Neo4j B-tree Indexes

`ingest_citygml.py` creates a B-tree index on the `id` property for every node label, plus one on the shared `Entity(id)` (see the *Shared `Entity` label* note above) that backs the generic, label-agnostic edge-creation lookups:

| Index name | Label | Property |
| ------------ | ------- | ---------- |
| `entity_id_idx` | `Entity` | `id` |
| `building_id_idx` | `Building` | `id` |
| `buildingpart_id_idx` | `BuildingPart` | `id` |
| `boundarysurface_id_idx` | `BoundarySurface` | `id` |
| `geometrypolygon_id_idx` | `GeometryPolygon` | `id` |
| `geometryring_id_idx` | `GeometryRing` | `id` |
| `geometrysolid_id_idx` | `GeometrySolid` | `id` |
| `geometrylinestring_id_idx` | `GeometryLineString` | `id` |
| `terrainintersection_id_idx` | `TerrainIntersection` | `id` |
| `buildingfunction_id_idx` | `BuildingFunction` | `id` |
| `rooftype_id_idx` | `RoofType` | `id` |
| `dataset_id_idx` | `Dataset` | `id` |
| `cityobject_id_idx` | `CityObject` | `id` |
| `cityobject_feature_type_idx` | `CityObject` | `feature_type` |
| `geometrymultisurface_id_idx` | `GeometryMultiSurface` | `id` |

All `MATCH (n:Label {id: $id})` lookups use these indexes automatically. Always filter by `id` (not `name`) when accessing a specific node by its GML identifier.

---

### Neo4j Spatial Plugin — R-tree Index

The Neo4j Spatial plugin (https://github.com/neo4j-contrib/spatial) is installed in the database. `ingest_citygml.py` automatically creates and populates an R-tree spatial index for **all top-level features** (both `Building` and `CityObject` nodes) after ingestion.

#### How it works

- Every `Building` and `CityObject` node that has a valid `gml:boundedBy/gml:Envelope` carries a `bbox_wkt` property: a 2D WKT `POLYGON` string derived from `bbox_min_x/y` and `bbox_max_x/y`.
- A WKT spatial layer named **`features`** is created once via `CALL spatial.addWKTLayer('features', 'bbox_wkt')`.
- All `Building` and `CityObject` nodes with a valid `bbox_wkt` are registered in the layer via `CALL spatial.addNodes('features', nodes)`, inserting their bounding-box polygons into the R-tree.
- Re-running the ingest script is safe: the layer is only created if absent, and `spatial.addNodes` re-indexes nodes in place.

#### Spatial layer procedures (reference)

| Procedure | Purpose |
|-----------|---------|
| `CALL spatial.layers() YIELD name` | List all spatial layers |
| `CALL spatial.addWKTLayer(name, wktProp)` | Create a WKT layer reading geometry from node property |
| `CALL spatial.addNode(layer, node) YIELD node` | Add / re-index a node in the layer |
| `CALL spatial.intersects(layer, wkt) YIELD node` | Return nodes whose geometry intersects a WKT shape |
| `CALL spatial.bbox(layer, lowerLeft, upperRight) YIELD node` | Return nodes within a bounding rectangle |
| `CALL spatial.withinDistance(layer, point, distKm) YIELD node` | Return nodes within a radius |

#### Example — find features (buildings + city objects) overlapping ≥ 90 % with a reference building

The query uses the R-tree to cheaply narrow candidates, then computes exact bbox-overlap percentage in Cypher:

```cypher
MATCH (ref:Building {id: $ref_id})
WITH ref,
     ref.bbox_min_x AS rx1, ref.bbox_min_y AS ry1,
     ref.bbox_max_x AS rx2, ref.bbox_max_y AS ry2,
     (ref.bbox_max_x - ref.bbox_min_x) * (ref.bbox_max_y - ref.bbox_min_y) AS ref_area
CALL spatial.intersects('features', ref.bbox_wkt) YIELD node AS cand
WHERE cand.id <> ref.id
WITH ref, cand, rx1, ry1, rx2, ry2, ref_area,
     cand.bbox_min_x AS cx1, cand.bbox_min_y AS cy1,
     cand.bbox_max_x AS cx2, cand.bbox_max_y AS cy2
WITH ref, cand, ref_area,
     (apoc.coll.max([rx1, cx1]) ) AS ix1,
     (apoc.coll.max([ry1, cy1]) ) AS iy1,
     (apoc.coll.min([rx2, cx2]) ) AS ix2,
     (apoc.coll.min([ry2, cy2]) ) AS iy2
WHERE ix2 > ix1 AND iy2 > iy1
WITH cand, ref_area, (ix2 - ix1) * (iy2 - iy1) AS inter_area
WHERE inter_area / ref_area >= 0.9
RETURN cand.id AS feature_id, labels(cand) AS types,
       round(inter_area / ref_area * 100) AS overlap_pct
ORDER BY overlap_pct DESC
```

Without APOC, use `CASE`-based max/min instead:

```cypher
MATCH (ref:Building {id: $ref_id})
WITH ref,
     ref.bbox_min_x AS rx1, ref.bbox_min_y AS ry1,
     ref.bbox_max_x AS rx2, ref.bbox_max_y AS ry2,
     (ref.bbox_max_x - ref.bbox_min_x) * (ref.bbox_max_y - ref.bbox_min_y) AS ref_area
CALL spatial.intersects('features', ref.bbox_wkt) YIELD node AS cand
WHERE cand.id <> ref.id
WITH ref, cand, rx1, ry1, rx2, ry2, ref_area,
     cand.bbox_min_x AS cx1, cand.bbox_min_y AS cy1,
     cand.bbox_max_x AS cx2, cand.bbox_max_y AS cy2
WITH ref, cand, ref_area,
     CASE WHEN rx1 > cx1 THEN rx1 ELSE cx1 END AS ix1,
     CASE WHEN ry1 > cy1 THEN ry1 ELSE cy1 END AS iy1,
     CASE WHEN rx2 < cx2 THEN rx2 ELSE cx2 END AS ix2,
     CASE WHEN ry2 < cy2 THEN ry2 ELSE cy2 END AS iy2
WHERE ix2 > ix1 AND iy2 > iy1
WITH cand, ref_area, (ix2 - ix1) * (iy2 - iy1) AS inter_area
WHERE inter_area / ref_area >= 0.9
RETURN cand.id AS feature_id, labels(cand) AS types,
       round(inter_area / ref_area * 100) AS overlap_pct
ORDER BY overlap_pct DESC
```

#### Example — all features within a bounding rectangle

```cypher
CALL spatial.bbox('features',
     {x: 566500.0, y: 5932800.0},
     {x: 567000.0, y: 5933200.0}) YIELD node
RETURN node.id, labels(node) AS types,
       coalesce(node.feature_type, 'Building') AS feature_type,
       node.measured_height AS height
ORDER BY height DESC
```

---

### APOC, GenAI, and GDS Plugin Integration

Four plugins are installed and should be used whenever applicable:

| Plugin | Purpose in this project |
| ------ | ----------------------- |
| **APOC** | Batch operations (`apoc.periodic.iterate`), collection utilities (`apoc.coll.max/min`) |
| **Neo4j Spatial** | R-tree WKT index for all top-level features — Building + CityObject (`spatial.bbox`, `spatial.intersects`) |
| **Neo4j GenAI** | Vector embeddings for semantic similarity search on `Building.description` |
| **Neo4j GDS** | Louvain community detection on the `NEARBY_BUILDING` proximity graph |

#### APOC — batch indexing example

`ingest_citygml.py` uses `spatial.addNodes` to batch-insert all features with `bbox_wkt` into the R-tree in one transaction, avoiding per-node overhead on large datasets:

```cypher
MATCH (n) WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NOT NULL
WITH collect(n) AS nodes
CALL spatial.addNodes('features', nodes) YIELD count
RETURN count AS indexed
```

#### GenAI — semantic similarity search

Each Building node carries a `description` property (natural-language text) and, when `NEO4J_GENAI_KEY` is set, an `embedding` vector (1 536-dim, cosine).

A vector index named `building_embeddings` is created automatically:

```cypher
CREATE VECTOR INDEX building_embeddings IF NOT EXISTS
FOR (b:Building) ON (b.embedding)
OPTIONS {indexConfig: {`vector.dimensions`: 1536, `vector.similarity_function`: 'cosine'}}
```

**Semantic search — find buildings most similar to a text query:**

```cypher
// Encode the query text on the fly and find the 10 most similar buildings
CALL genai.vector.encode('<query text>', 'OpenAI', {token: $api_key}) YIELD vector AS qv
CALL db.index.vector.queryNodes('building_embeddings', 10, qv) YIELD node AS b, score
RETURN b.id, b.description, score
ORDER BY score DESC
```

Set `NEO4J_GENAI_KEY` to your OpenAI API key before running `ingest_citygml.py` to populate embeddings:

```bash
export NEO4J_GENAI_KEY=sk-...
python3 ingest_citygml.py
```

#### GDS — community detection and proximity graph

This enrichment is **opt-in** — pass `--nearby-edges` to `ingest_citygml.py` (off by default, since it is expensive on large datasets). When enabled, it creates `NEARBY_BUILDING` directed edges between buildings whose centroids are within 100 m, then runs GDS Louvain to assign each building a `community_id`.

**Proximity graph schema:**

```text
(Building)-[:NEARBY_BUILDING {distance_m: float}]->(Building)
```

**Query buildings by community:**

```cypher
MATCH (b:Building)
RETURN b.community_id AS community, count(b) AS members,
       avg(b.measured_height) AS avg_height_m
ORDER BY members DESC
```

**GDS community detection workflow (re-run manually if needed):**

```cypher
// Drop stale projection (non-fatal if absent)
CALL gds.graph.drop('building_proximity', false)

// Project NEARBY_BUILDING as undirected
CALL gds.graph.project(
  'building_proximity',
  'Building',
  {NEARBY_BUILDING: {orientation: 'UNDIRECTED'}}
)

// Run Louvain and write community_id back to Building nodes
CALL gds.louvain.write('building_proximity', {writeProperty: 'community_id'})
YIELD communityCount, modularity
RETURN communityCount, modularity

// Drop the in-memory projection
CALL gds.graph.drop('building_proximity')
```

**APOC max/min in overlap queries:**

When computing bbox intersections, use `apoc.coll.max` / `apoc.coll.min` for clarity:

```cypher
WITH apoc.coll.max([rx1, cx1]) AS ix1,
     apoc.coll.max([ry1, cy1]) AS iy1,
     apoc.coll.min([rx2, cx2]) AS ix2,
     apoc.coll.min([ry2, cy2]) AS iy2
```

Without APOC, use `CASE WHEN rx1 > cx1 THEN rx1 ELSE cx1 END` equivalents.

---

### Example Cypher — spatial and geometry queries

Replace `<dataset-name>`, `<building-gml-id>`, and the bbox coordinates with values from your actual
dataset. Bbox coordinates must be in the dataset CRS (see `ds.crs`).

```cypher
-- Dataset-level bounding box
MATCH (ds:Dataset {name: "<dataset-name>.gml"})
RETURN ds.bbox_lower_corner, ds.bbox_upper_corner, ds.crs

-- Building-level extent
MATCH (b:Building {id: "<building-gml-id>"})
RETURN b.bbox_min_x, b.bbox_min_y, b.bbox_min_z,
       b.bbox_max_x, b.bbox_max_y, b.bbox_max_z

-- All features within a geographic area — spatial.bbox uses the R-tree index
-- Never use WHERE b.center_y >= ... for range scans; that forces a full node scan
CALL spatial.bbox('features',
     {x: <min_x>, y: <min_y>},
     {x: <max_x>, y: <max_y>}) YIELD node
RETURN node.id, labels(node) AS types,
       coalesce(node.feature_type, 'Building') AS feature_type,
       node.center_x, node.center_y
ORDER BY node.center_x

-- Buildings only — filter the R-tree result by label
CALL spatial.bbox('features',
     {x: <min_x>, y: <min_y>},
     {x: <max_x>, y: <max_y>}) YIELD node AS b
WHERE b:Building
RETURN b.id, b.center_x, b.center_y, b.measured_height
ORDER BY b.measured_height DESC

-- All wall surfaces of a building with their coordinate rings
MATCH (b:Building {id: "<building-gml-id>"})
      -[:HAS_BOUNDARY]->(s:BoundarySurface {surface_type: "WallSurface"})
      -[:HAS_POLYGON]->(p:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
RETURN s.id AS surface, p.id AS polygon, r.pos_list AS coordinates

-- All surface types on a building with polygon counts
MATCH (b:Building {id: "<building-gml-id>"})
      -[:HAS_BOUNDARY]->(s:BoundarySurface)
      -[:HAS_POLYGON]->(p:GeometryPolygon)
RETURN s.surface_type AS type, count(p) AS polygons

-- Retrieve the full LOD solid: solid node + all referenced polygon rings
-- Relationship is always HAS_LOD_SOLID regardless of the LOD level (2, 3, or 4)
MATCH (b:Building {id: "<building-gml-id>"})
      -[:HAS_LOD_SOLID]->(solid:GeometrySolid)
      -[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
RETURN solid.id AS solid, p.id AS polygon, r.pos_list AS coordinates

-- Terrain intersection curve of a building
MATCH (b:Building {id: "<building-gml-id>"})
      -[:HAS_TERRAIN_INTERSECTION]->(ti:TerrainIntersection)
      -[:HAS_LINE]->(ls:GeometryLineString)
RETURN ls.pos_list AS terrain_curve, ls.srs_dimension AS dim

-- All city objects by module in a dataset
MATCH (n:CityObject)-[:PART_OF]->(d:Dataset {name: "<dataset-name>.gml"})
RETURN n.feature_type AS type, count(n) AS count
ORDER BY count DESC

-- All roads with their surface geometry polygons
MATCH (n:CityObject {feature_type: 'Road'})
      -[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
      -[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
RETURN n.id AS road, ms.lod AS lod, r.pos_list AS coordinates

-- WaterBody boundary surfaces
MATCH (w:CityObject {feature_type: 'WaterBody'})
      -[:HAS_BOUNDARY]->(s:BoundarySurface)
      -[:HAS_POLYGON]->(p:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
RETURN w.id AS waterbody, s.surface_type AS surface, r.pos_list AS coordinates

-- Bridge solid geometry
MATCH (b:CityObject {feature_type: 'Bridge'})
      -[:HAS_LOD_SOLID]->(solid:GeometrySolid)
      -[:HAS_SURFACE_MEMBER]->(p:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(r:GeometryRing)
RETURN b.id AS bridge, r.pos_list AS coordinates

-- Vegetation objects with height
MATCH (v:CityObject)
WHERE v.feature_type IN ['SolitaryVegetationObject', 'PlantCover']
  AND v.height IS NOT NULL
RETURN v.id, v.feature_type, v.height, v.species
ORDER BY toFloat(v.height) DESC

-- Implicit geometry: vegetation trees with world-space reference point and transform
MATCH (v:CityObject {feature_type: 'SolitaryVegetationObject'})
      -[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface {is_implicit: true})
RETURN v.id AS tree, ms.reference_point AS world_anchor,
       ms.transformation_matrix AS transform, ms.lod AS lod
```

**Hamburg HafenCity example values:**
`<dataset-name>` = `hamburg_hafencity` · `<building-gml-id>` = `DEHHALKA4IB000Cv` ·
bbox min = `{x: 566500.0, y: 5932800.0}` · bbox max = `{x: 567000.0, y: 5933200.0}` (ETRS89 UTM32)

---

## Query / Traversal Rules

### Spatial Index Rule

**Always use the R-tree spatial index as the entry point for any query that filters `Building` or `CityObject` nodes by geographic location.**

- For a bounding-box area query use `CALL spatial.bbox('features', lowerLeft, upperRight) YIELD node`.
- For an intersection / overlap query use `CALL spatial.intersects('features', wkt) YIELD node`.
- For a radius query use `CALL spatial.withinDistance('features', point, distKm) YIELD node`.

**Never substitute a full `MATCH (b:Building) WHERE b.center_y >= … AND b.center_y <= …`** — that forces a complete label scan. Always prefer the spatial procedures above and filter further in `WHERE` clauses on the yielded nodes (e.g. `WHERE node:Building` or `WHERE node.feature_type = 'Road'`).

This rule applies to all Cypher written in this project: ad-hoc queries in Neo4j Browser, queries embedded in Python scripts, and Cypher in documentation examples.

---

### Starting a Query

1. Identify the **best start node** based on the query intent — choose the node whose `name` and `label` most closely match the topic being asked about.
2. Use a Cypher `MATCH` to retrieve that node by label and name (or fuzzy match via `toLower`).
3. If the query involves finding buildings by location, **start with a spatial procedure** (see Spatial Index Rule above) rather than `MATCH (b:Building)`.

### Traversal

- Always follow **outgoing edges** only (direction: `(a)-[r]->(b)`).
- At each node, inspect the **labels of outgoing edges**. Decide which edge types are semantically relevant to the query goal.
- Follow only those edges that are likely to lead toward the answer — prune irrelevant branches.
- Collect node and edge properties at each step to build the answer.

### Cycle Prevention

- Maintain a **visited set** of node `id` values for each query session.
- Before visiting a node, check if its `id` is already in the visited set.
- If it is, **skip** that node and do not traverse its edges again.
- Add the node's `id` to the visited set immediately upon first visit.

### Depth Limit

- Apply a maximum traversal depth (default: **5 hops**) to prevent runaway queries.
- This can be adjusted per query if broader context is needed.

---

## Output naming conventions

- `output/<source_file>_summary.md` — ingestion summaries
- `output/<query_slug>_result.md` — query results
- `output/html/<name>.html` — standalone HTML exports (maps, viewers, reports)

---

## Visualization

### When to use 2D vs 3D

| Signal in user request | Response |
| --- | --- |
| "footprint", "polygon", "plan view", "map" | 2D canvas viewer |
| "3D", "model", "building volume", "height", "facade" | 3D Three.js viewer |
| Neither explicit → default | 3D (richer) |

### Cypher patterns for geometry

#### Feature centre + metadata (any feature type)

```cypher
MATCH (f)
WHERE (f:Building OR f:CityObject) AND f.id = '<id>'
RETURN f.center_x AS cx, f.center_y AS cy,
       f.ground_z  AS gz,
       coalesce(f.measured_height, 0.0) AS h,
       coalesce(f.description, f.id)    AS desc
LIMIT 1
```

#### 2D footprint ring (Building GroundSurface)

```cypher
MATCH (b:Building {id: '<id>'})
      -[:HAS_BOUNDARY]->(bs:BoundarySurface {surface_type: 'GroundSurface'})
      -[:HAS_POLYGON]->(gp:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
RETURN gr.pos_list AS pos_list
LIMIT 1
```

#### All 3D surface polygons (Building — walls + roof + ground)

```cypher
MATCH (b:Building {id: '<id>'})
      -[:HAS_BOUNDARY]->(bs:BoundarySurface)
      -[:HAS_POLYGON]->(gp:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
RETURN bs.surface_type AS stype, gr.pos_list AS pos_list
ORDER BY bs.surface_type
```

#### Fallback for non-Building features (Road, Bridge, WaterBody, …)

```cypher
MATCH (f {id: '<id>'})
      -[:HAS_LOD_SURFACE]->(ms:GeometryMultiSurface)
      -[:HAS_SURFACE_MEMBER]->(gp:GeometryPolygon)
      -[:HAS_EXTERIOR_RING]->(gr:GeometryRing)
RETURN 'Surface' AS stype, gr.pos_list AS pos_list
```

**Parsing `pos_list`** — space-separated `x y z` triplets:

```python
vals = list(map(float, pos_list.split()))
ring = [[vals[i], vals[i+1], vals[i+2]] for i in range(0, len(vals) - 2, 3)]
# ring[0] == ring[-1]  (closing vertex — keep it; JS triangulator drops it)
```

### Quick generation via `visualize_buildings_3d.py`

```bash
# Minimal CSV — one column named gmlid / id / building_id / fid:
#   gmlid
#   DEHHALKA4IB000Cv

# With optional role and label columns:
#   gmlid,role,label
#   DEHHALKA4IB000Cv,reference,Tower A
#   DEHHALKA4IB000Ce,neighbour,Block B

py visualize_buildings_3d.py ids.csv
py visualize_buildings_3d.py ids.csv --radius 100
py visualize_buildings_3d.py ids.csv --output output/html/my_view.html --title "HafenCity selection"
py visualize_buildings_3d.py ids.csv --ref-id DEHHALKA4IB000Cv --radius 100
```

Output: self-contained HTML saved to `output/html/buildings_3d.html` (default) or `--output` path.

### HTML / Three.js technical rules

| Requirement | Reason |
| --- | --- |
| Use Three.js r128 **UMD** (`three.min.js`) + `examples/js/controls/OrbitControls.js` | ES modules fail over `file://` (CORS). r128 is the last version with non-module OrbitControls. |
| Use earcut `dist/earcut.dev.js` (UMD) | Same reason — sets `window.earcut` global. |
| Never use `type="module"` on `<script>` | Blocks loading from `file://`. |
| Subtract a coordinate origin before passing to WebGL | UTM coordinates (~566000, ~5932000) exceed float32 precision — centre on a scene origin. |
| CityGML → Three.js axis mapping | `Three.x = UTM_X − OX` · `Three.y = UTM_Z − OZ` · `Three.z = −(UTM_Y − OY)` |
| Triangulate with Newell-normal projection + earcut | Fan-from-vertex-0 breaks non-convex polygons; Newell determines the dominant plane, earcut triangulates the 2D projection. |

### Color conventions

| Role | WallSurface | RoofSurface | GroundSurface | Generic Surface |
| --- | --- | --- | --- | --- |
| `reference` | `#558b2f` | `#8bc34a` | `#33691e` | `#66bb6a` |
| `neighbour` | `#c62828` | `#ef9a9a` | `#7f0000` | `#ef5350` |
| `highlight` | `#e65100` | `#ffcc80` | `#bf360c` | `#ff9800` |
| default-0 | `#1565c0` | `#90caf9` | `#0d47a1` | `#42a5f5` |
| default-1 | `#6a1b9a` | `#ce93d8` | `#4a148c` | `#ab47bc` |
| default-2 | `#004d40` | `#80cbc4` | `#00251a` | `#26a69a` |

Additional unlabelled features cycle through the default palette.

### 2D footprint pattern (canvas, no CDN)

1. Fetch `center_x`, `center_y`, and `gr.pos_list` from the GroundSurface ring.
2. Compute a 2D bounding box that includes the optional radius circle.
3. Render with HTML5 Canvas (no external dependencies — works offline).
4. Save to `output/html/` or `web/`.

Key canvas Y-flip (UTM Y grows upward; Canvas Y grows downward):

```javascript
function toScreen(wx, wy) {
  return [
    offX + (wx - wxMin) * scale,
    H - offY - (wy - wyMin) * scale
  ];
}
```

---

## Scripts

The following Python scripts implement the full pipeline. Always use these scripts — do **not** write ad-hoc ingestion or export code.

### Dependencies

```bash
pip3 install lxml neo4j pyproj mapbox-earcut numpy --break-system-packages
pip3 install shapely --break-system-packages   # optional: clean window/door holes in 3D Tiles
```

---

### `ingest_citygml.py` — CityGML → Neo4j

Parses a CityGML 2.0 file and writes all supported modules into Neo4j.

```bash
python3 ingest_citygml.py <path-to-file.gml>
```

- Input : path to a `.gml` file passed as argument
- Output: nodes and relationships in Neo4j (see Graph Model above)

The script is **idempotent** — re-running it merges into existing nodes via `MERGE … SET`, so it can be used to refresh data after fixes without duplicating anything.

**Supported CityGML 2.0 modules:** Building, Transportation (Road, Railway, Track, Square, TransportationComplex), Vegetation (SolitaryVegetationObject, PlantCover), WaterBody, LandUse, CityFurniture, Relief (ReliefFeature with TIN/MassPoint/Breakline components), Bridge, Tunnel, CityObjectGroup.

**Works for any CityGML dataset** — no region-specific values are hardcoded. CRS, LOD, codes, municipality keys, gen:stringAttribute names, and externalReference URLs are all read from the file at runtime.

**What it ingests:**
- `Dataset` node with CRS, LOD, bounding box (from `CityModel/gml:boundedBy`)
- `Building` nodes with all scalar attributes, bounding box, and every `gen:stringAttribute`
- `CityObject` nodes for all other top-level feature types, each also carrying a module-specific label (e.g. `Road`, `WaterBody`) when APOC is available
- `_gen_attrs_map` — JSON map of `{lowercase_key: OriginalCamelCaseName}` stored on each feature for faithful round-trip export
- `external_information_system` and `external_object_name` — read from `cg:externalReference` in the file (absent if the file has none)
- `creationDate` on buildings, city objects, and boundary surfaces
- `BuildingFunction`, `RoofType` lookup nodes (ALKIS codes → human-readable names; unrecognised codes stored as-is)
- Predicted roof material is **not** a lookup node — stored as a `gen:stringAttribute` named `PredictedRoofMaterial` on the Building node (property key: `predicted_roof_material`)
- `District` and `City` nodes derived from `Gemeindeschluessel` when present, or from the filename otherwise
- Full LOD geometry: `GeometrySolid` (lod*Solid), `GeometryMultiSurface` (lod*MultiSurface on city objects), `BoundarySurface` (module boundary surfaces including wtr/brid/tun), `GeometryPolygon`, `GeometryRing` (verbatim posList), `TerrainIntersection`, `GeometryLineString`
- Implicit geometry: `lod{N}ImplicitRepresentation` (Vegetation, CityFurniture) stored as `GeometryMultiSurface` with `is_implicit=true`, `transformation_matrix` (4×4 row-major), and `reference_point` (world anchor in dataset CRS); polygon rings contain local prototype coordinates. Features using `relativeGMLGeometry xlink:href` share the same prototype polygons via a post-ingest Cypher resolution pass.
- Relief geometry: TIN triangles stored as polygon chains; MassPoint/Breakline stored as GeometryLineString nodes

**Known namespace requirement:**  
`creationDate` is in the CityGML core namespace (`http://www.opengis.net/citygml/2.0`), registered in the script's `NS` dict as `"cg"`. Use `cg:creationDate` in any xpath lookup — plain `"creationDate"` without a prefix will silently fail.

---

### `export_citygml.py` — Neo4j → CityGML

Reconstructs a CityGML 2.0 file from the graph database, covering all modules supported by the ingestion pipeline.

```bash
python3 export_citygml.py
```

- Input : Neo4j (queries `Dataset`, `Building`, `CityObject`, geometry, and lookup nodes)
- Output: `output/citygml/<stem>_export.gml` (stem derived from `Dataset.source`)

The `export(out_path, bbox_wkt=None)` function accepts an optional `bbox_wkt` argument (WKT POLYGON in dataset CRS). When supplied, only buildings whose bounding box intersects the polygon are exported (using `spatial.intersects` on the R-tree layer); all `CityObject` nodes are always exported regardless of bbox.

**Works for any CityGML dataset** — gen:stringAttribute names and externalReference URLs are read from the graph (stored during ingest), not reconstructed from hardcoded patterns. The output filename is derived from the `Dataset.source` property.

**Modules exported** (driven by `CO_MODULE_MAP`):

| `feature_type` | CityGML namespace | Geometry style |
| -------------- | ----------------- | -------------- |
| `Road`, `Railway`, `Track`, `Square`, `TransportationComplex` | `citygml/transportation/2.0` | `lod{N}MultiSurface` |
| `SolitaryVegetationObject`, `PlantCover` | `citygml/vegetation/2.0` | `lod{N}Geometry` or `lod{N}ImplicitRepresentation` |
| `WaterBody` | `citygml/waterbody/2.0` | Solid + boundary surfaces or `lod{N}MultiSurface` |
| `LandUse` | `citygml/landuse/2.0` | `lod{N}MultiSurface` |
| `CityFurniture` | `citygml/cityfurniture/2.0` | `lod{N}Geometry` |
| `ReliefFeature` | `citygml/relief/2.0` | TIN → `lod{N}MultiSurface` (MassPoint/Breakline not exported) |
| `Bridge` | `citygml/bridge/2.0` | Solid + boundary surfaces or `lod{N}MultiSurface` |
| `Tunnel` | `citygml/tunnel/2.0` | Solid + boundary surfaces or `lod{N}MultiSurface` |
| `CityObjectGroup` | `citygml/cityobjectgroup/2.0` | `lod{N}Geometry` |

**What is reconstructed for Buildings:**
- `CityModel` root with full namespace declarations and `xsi:schemaLocation`
- Dataset-level `gml:boundedBy` envelope
- All `cityObjectMember/bldg:Building` elements with:
  - `gml:boundedBy` envelope
  - `creationDate` (when stored)
  - `externalReference` — only emitted when `external_information_system` is present on the Building node (stored verbatim from the original file; omitted if the source file had none)
  - All `gen:stringAttribute` values with original CamelCase names restored via `_gen_attrs_map`; Hamburg ADV attrs emitted first in canonical order, followed by any dataset-specific attrs
  - `bldg:function`, `bldg:roofType`, `bldg:measuredHeight` (with `uom`), `bldg:storeysAboveGround`
  - `bldg:lod{N}Solid` → `gml:Solid` (original `gml:id`) + `gml:CompositeSurface` (new UUID) + `xlink:href` surface members; LOD N read from `GeometrySolid.lod` (1–4)
  - `bldg:lod{N}TerrainIntersection` → `gml:MultiCurve` → `gml:LineString` with verbatim `posList`; LOD N read from `TerrainIntersection.lod` (1–4)
  - `bldg:boundedBy` surfaces (`WallSurface`, `RoofSurface`, `GroundSurface`) with `creationDate`, `gml:Polygon`, `gml:LinearRing`, verbatim `posList`, and interior rings; `bldg:lod{N}MultiSurface` is omitted for surfaces that have no stored polygons (schema allows `minOccurs="0"`)

**Not reconstructed (not stored in graph):**
- `gml:CompositeSurface gml:id` and `gml:MultiSurface gml:id` — replaced with fresh UUIDs (not referenced by any `xlink:href`)
- MassPoint and Breakline relief geometry — stored as orphaned `GeometryLineString` nodes during ingest with no link back to the `ReliefFeature`; omitted from export

---

### `build_3dtiles.py` — CityGML → 3D Tiles

Converts all CityGML 2.0 geometry to a 3D Tiles 1.1 dataset for browser visualisation in CesiumJS. Supports all modules ingested by `ingest_citygml.py`. **Geometry is read from the Neo4j graph, not from the GML file** (run `ingest_citygml.py` first).

```bash
python3 build_3dtiles.py                       # the single dataset in the graph
python3 build_3dtiles.py --dataset <file.gml>  # specific Dataset.source (alias: --source)
python3 build_3dtiles.py --out <dir>           # custom output dir (default output/3dtiles)
```

- Input : a `Dataset` node in Neo4j, selected by `--dataset`/`--source` (the `Dataset.source` value); required only when more than one dataset is present.
- Output: `<out>/tileset.json` + `<out>/tiles/city.glb` (default `out` = `output/3dtiles`)

**To view in browser:** point a CesiumJS viewer (or `visualize_graph_3d.py`'s combined viewer) at the generated `tileset.json`, served over HTTP — 3D Tiles will not load from `file://`.

**Works for any CityGML dataset** — the input CRS and geoid undulation are derived automatically from the file's `gml:Envelope/@srsName` at runtime; no hardcoded EPSG codes or region-specific offsets.

**Supported modules and material colours** (driven by `FEATURE_MATERIAL` + `MATERIALS_GLTF`):

| Module | Feature types | Colour |
| ------ | ------------- | ------ |
| Building | WallSurface, ClosureSurface / RoofSurface / GroundSurface | grey / terracotta / dark grey |
| Transportation | Road, Railway, Track, Square, TransportationComplex | asphalt |
| Vegetation | SolitaryVegetationObject, PlantCover | green |
| WaterBody | WaterBody | blue |
| LandUse | LandUse | yellow-green |
| CityFurniture / CityObjectGroup | CityFurniture, CityObjectGroup | orange |
| Relief | ReliefFeature (TIN triangles) | tan |
| Bridge | Bridge | light brown |
| Tunnel | Tunnel | mid grey |

Geometry is fetched from the graph by `fetch_surfaces()` via Cypher — building/CityObject boundary surfaces, `lodN` solids, and MultiSurfaces (`HAS_BOUNDARY`/`HAS_LOD_SOLID`/`HAS_LOD_SURFACE` → `GeometryPolygon` → `HAS_EXTERIOR_RING`), so all geometry styles (MultiSurface, Solid, lod{N}Geometry, TIN) are captured automatically.

**Window/door holes:**  
Each polygon's `gml:interior` rings (`(:GeometryPolygon)-[:HAS_INTERIOR_RING {ring_index}]->(:GeometryRing)`) are fetched alongside the exterior and cut as holes in the triangulated wall — so LoD3 openings are no longer obscured by solid wall geometry. `triangulate_polygon_enu(verts, holes)` projects every ring onto an isometric in-plane 2-D basis (`_plane_basis`), then, when **shapely** is available, computes `exterior − union(holes)` (`_triangulate_with_holes_shapely`) before earcut. This boolean difference is what makes overlapping, mutually-touching, and edge-touching openings render correctly — `mapbox_earcut` alone silently drops holes in those cases. Without shapely it falls back to feeding the raw rings to earcut (holes still cut, minus the robust overlap handling). To populate the interior rings, the source CityGML must carry openings as `gml:interior` rings on the wall polygon; `eval/add_opening_holes.py` rewrites a LoD3 file so every `bldg:opening` becomes such a hole.

**Coordinate pipeline:**  
Dataset CRS (read from `srsName`) → horizontal EPSG via `_horiz_epsg_from_srs()` → WGS84 lon/lat (pyproj) + geoid undulation derived by compound-CRS → EPSG:4979 transform at the dataset centroid → ECEF → local ENU → packed as glTF Y-up geometry with a Z-up ENU tile transform.

**CRS parsing (`_horiz_epsg_from_srs`):**  
Handles `EPSG:NNNNN`, `urn:ogc:def:crs:EPSG::NNNNN`, ADV UTM (`ETRS89_UTM32/33`), and ADV Gauss-Krüger (`GK2`–`GK5`). Falls back to the raw `srsName` string for pyproj to resolve.

**Geoid undulation (`_geoid_m`):**  
Uses a compound-CRS → EPSG:4979 pyproj transformation at the dataset bbox centroid. Known mappings: ETRS89-UTM32/33 and DE_DHDN GK2–GK5 (DHHN92 ≈ DHHN2016). Returns 0.0 m with a printed note for unrecognised CRS or when PROJ datum-grid files are absent.

**Tile transform convention:**  
The 3D Tiles spec remaps glTF axes before applying the tile transform (glTF +Y → local +Z/up; glTF −Z → local +Y/north). The tile transform therefore uses columns `[East_ECEF | North_ECEF | Up_ECEF | Center_ECEF]`, **not** `[East | Up | −North]`. Using the wrong column order rotates all geometry 90°.

**GLB content:**  
Up to 11 mesh primitives (one per material slot; empty slots are omitted). Polygon faces triangulated with `mapbox_earcut` (with `gml:interior` rings cut as holes — see **Window/door holes** above). All modules share a single `city.glb` file.

---

## CityGML Code Reference Tables

Use these tables to resolve numeric or compound codes to human-readable names when creating lookup nodes. Only create lookup nodes for codes **actually present in the file** — do not insert the full table.

---

### ALKIS Building Function Codes (`bldg:function`)

Format: `<FeatureTypeCode>_<SubCode>` — e.g. `31001_2050`.

#### Feature type 31001 — AX_Gebaeude (Building)

Source: ALKIS-Objektartenkatalog 6.0, Stand 11.04.2008, pp. 190–198.

| Code | Name (EN) | Name (DE) |
|------|-----------|-----------|
| 31001_1000 | Residential Building | Wohngebäude |
| 31001_1010 | Detached House | Wohnhaus |
| 31001_1020 | Residential Home / Dormitory | Wohnheim |
| 31001_1100 | Mixed-Use Building with Residential | Gemischt genutztes Gebäude mit Wohnen |
| 31001_1110 | Residential Building with Community Facilities | Wohngebäude mit Gemeinbedarf |
| 31001_1120 | Residential Building with Trade and Services | Wohngebäude mit Handel und Dienstleistungen |
| 31001_2000 | Commercial / Industrial Building | Gebäude für Wirtschaft oder Gewerbe |
| 31001_2010 | Trade and Services Building | Gebäude für Handel und Dienstleistungen |
| 31001_2020 | Office Building | Bürogebäude |
| 31001_2030 | Credit Institution | Kreditinstitut |
| 31001_2040 | Insurance Building | Versicherung |
| 31001_2050 | Commercial Building | Geschäftsgebäude |
| 31001_2051 | Department Store | Kaufhaus |
| 31001_2052 | Shopping Centre | Einkaufszentrum |
| 31001_2060 | Trade Fair Hall | Messehalle |
| 31001_2070 | Accommodation Building | Gebäude für Beherbergung |
| 31001_2071 | Hotel / Motel / Pension | Hotel, Motel, Pension |
| 31001_2081 | Restaurant / Gasthaus | Gaststätte, Restaurant |
| 31001_2100 | Industrial and Commercial Building | Gebäude für Gewerbe und Industrie |
| 31001_2110 | Production Building | Produktionsgebäude |
| 31001_2111 | Factory | Fabrik |
| 31001_2120 | Workshop | Werkstatt |
| 31001_2130 | Petrol Station | Tankstelle |
| 31001_2140 | Storage Building | Gebäude für Vorratshaltung |
| 31001_2143 | Warehouse / Storage Hall | Lagerhalle, Lagerschuppen, Lagerhaus |
| 31001_2150 | Freight / Forwarding Building | Speditionsgebäude |
| 31001_2160 | Research Building | Gebäude für Forschungszwecke |
| 31001_2180 | Industrial Social Facilities Building | Gebäude für betriebliche Sozialeinrichtung |
| 31001_2210 | Mill | Mühle |
| 31001_2220 | Weather Station | Wetterstation |
| 31001_2400 | Transport Operations Building (general) | Betriebsgebäude zu Verkehrsanlagen |
| 31001_2410 | Road Transport Operations Building | Betriebsgebäude für Straßenverkehr |
| 31001_2420 | Railway Operations Building | Betriebsgebäude für Schienenverkehr |
| 31001_2430 | Air Transport Operations Building | Betriebsgebäude für Flugverkehr |
| 31001_2440 | Shipping Operations Building | Betriebsgebäude für Schiffsverkehr |
| 31001_2441 | Shipyard Hall | Werft (Halle) |
| 31001_2444 | Boathouse | Bootshaus |
| 31001_2460 | Parking Building | Gebäude zum Parken |
| 31001_2461 | Multi-Storey Car Park | Parkhaus |
| 31001_2463 | Garage | Garage |
| 31001_2465 | Underground Car Park | Tiefgarage |
| 31001_2500 | Utilities / Supply Building | Gebäude zur Versorgung |
| 31001_2510 | Water Supply Building | Gebäude zur Wasserversorgung |
| 31001_2511 | Waterworks | Wasserwerk |
| 31001_2520 | Electricity Supply Building | Gebäude zur Elektrizitätsversorgung |
| 31001_2522 | Electrical Substation | Umspannwerk |
| 31001_2540 | Telecommunications Building | Gebäude für Fernmeldewesen |
| 31001_2570 | Gas Supply Building | Gebäude zur Gasversorgung |
| 31001_2580 | Heating Plant | Heizwerk |
| 31001_3000 | Public Purposes Building | Gebäude für öffentliche Zwecke |
| 31001_3010 | Administrative Building | Verwaltungsgebäude |
| 31001_3011 | Parliament | Parlament |
| 31001_3012 | Town Hall | Rathaus |
| 31001_3016 | Embassy / Consulate | Botschaft, Konsulat |
| 31001_3020 | Building for Education and Research | Gebäude für Bildung und Forschung |
| 31001_3021 | School | Allgemein bildende Schule |
| 31001_3023 | University Building | Hochschulgebäude |
| 31001_3024 | Research Institute | Forschungsinstitut |
| 31001_3030 | Building for Cultural Purposes | Gebäude für kulturelle Zwecke |
| 31001_3046 | Mosque | Moschee |
| 31001_3050 | Healthcare Building | Gebäude für Gesundheitswesen |
| 31001_3060 | Building for Social Purposes | Gebäude für soziale Zwecke |
| 31001_3065 | Nursery / Kindergarten | Kinderkrippe, Kindergarten |
| 31001_3070 | Security and Order Building | Gebäude für Sicherheit und Ordnung |
| 31001_3071 | Police Station | Polizei |
| 31001_3072 | Fire Station | Feuerwehr |
| 31001_3073 | Barracks | Kaserne |
| 31001_3074 | Air Raid Shelter / Bunker | Schutzbunker |
| 31001_3210 | Sports Building | Gebäude für Sportzwecke |
| 31001_3211 | Sports Hall / Gymnasium | Sport-/Turnhalle |
| 31001_3220 | Bath / Swimming Building | Badegebäude |
| 31001_3230 | Stadium Building | Gebäude im Stadion |
| 31001_9998 | Not Specifiable from Source | Nach Quellenlage nicht zu spezifizieren |

#### Feature type 51002 — AX_BauwerkOderAnlageFuerIndustrieUndGewerbe (Industrial/Commercial Structure)

Source: ALKIS-Objektartenkatalog 6.0, Stand 11.04.2008, pp. 293–295.

| Code | Name (EN) | Name (DE) |
|------|-----------|-----------|
| 51002_1210 | Clarification Basin | Klärbecken |
| 51002_1220 | Wind Turbine | Windrad |
| 51002_1230 | Solar Panels | Solarzellen |
| 51002_1250 | Mast | Mast |
| 51002_1260 | Radio / Telecommunications Mast | Funkmast |
| 51002_1290 | Chimney / Flue | Schornstein, Schlot, Esse |
| 51002_1330 | Crane | Kran |
| 51002_1331 | Rotating Crane | Drehkran |
| 51002_1332 | Portal Crane | Portalkran |
| 51002_1333 | Overhead Travelling Crane | Laufkran, Brückenlaufkran |
| 51002_1340 | Dry Dock | Trockendock |
| 51002_1350 | Blast Furnace | Hochofen |
| 51002_1370 | Hydrant | Hydrant |
| 51002_9999 | Other Industrial/Commercial Structure | Sonstiges |

#### Feature type 51009 — AX_SonstigesBauwerkOderSonstigeEinrichtung (Other Structure or Installation)

Source: ALKIS-Objektartenkatalog 6.0, Stand 11.04.2008, pp. 309–312.

| Code | Name (EN) | Name (DE) |
|------|-----------|-----------|
| 51009_1610 | Canopy / Shelter | Überdachung |
| 51009_1611 | Carport | Carport |
| 51009_1620 | Staircase | Treppe |
| 51009_1650 | Ramp | Rampe |
| 51009_1700 | Wall | Mauer |
| 51009_1720 | Retaining Wall | Stützmauer |
| 51009_1740 | Fence | Zaun |
| 51009_1750 | Monument / Memorial / Statue | Denkmal, Denkstein, Standbild |
| 51009_1780 | Well | Brunnen |
| 51009_1790 | Sheet Piling | Spundwand |
| 51009_9999 | Other Structure or Installation | Sonstiges |

---

### ALKIS Roof Type Codes (`bldg:roofType`)

Source: ALKIS-Objektartenkatalog 6.0, Stand 11.04.2008, p. 202 — AX_Dachform.

| Code | Name (EN) | Name (DE) |
|------|-----------|-----------|
| 1000 | Flat Roof | Flachdach |
| 2100 | Mono-Pitch Roof | Pultdach |
| 2200 | Offset Mono-Pitch Roof | Versetztes Pultdach |
| 3100 | Gable Roof | Satteldach |
| 3200 | Hip Roof | Walmdach |
| 3300 | Half-Hip Roof | Krüppelwalmdach |
| 3400 | Mansard Roof | Mansardendach |
| 3500 | Tent / Pyramid Roof | Zeltdach |
| 3600 | Conical Roof | Kegeldach |
| 3700 | Dome Roof | Kuppeldach |
| 3800 | Sawtooth Roof | Sheddach |
| 3900 | Barrel / Arch Roof | Bogendach |
| 4000 | Tower Roof / Spire | Turmdach |
| 5000 | Mixed Form | Mischform |
| 9999 | Other | Sonstiges |

---

### Predicted Roof Material Values (`gen:stringAttribute[@name="PredictedRoofMaterial"]`)

These are free-text values predicted by classification models — not an official ALKIS enumeration.

| Value | Name (EN) |
|-------|-----------|
| `concrete` | Concrete |
| `glass` | Glass |
| `metal` | Metal |
| `roof_tiles` | Roof Tiles |
| `tar_paper` | Tar Paper / Bitumen |
| `vegetation` | Green Roof / Vegetation |
| `solar_panels` | Solar Panels |
| `gravel` | Gravel |

---

### Data Source Codes (`DatenquelleLage`, `DatenquelleBodenhoehe`, `DatenquelleDachhoehe`, `DatenquelleGeschossanzahl`)

| Code | Meaning (EN) | Meaning (DE) |
|------|--------------|--------------|
| 1000 | Aerial Image / Orthophoto | Luftbild, Orthofoto |
| 1100 | Digital Terrain Model (DTM/DGM) | Digitales Geländemodell |
| 1200 | Digital Surface Model (DSM/DOM) | Digitales Oberflächenmodell |
| 2000 | Terrestrial Survey | Terrestrische Aufnahme |
| 3000 | Cadastre / Official Register | Kataster, amtliches Register |
| 4000 | Building Permit / Planning Documents | Baugenehmigung, Planungsunterlagen |
| 5000 | Other / Unknown | Sonstige, unbekannte Quelle |

---

### Geometry Type Reference Code (`Geometrietyp2DReferenz`)

| Code | Meaning |
|------|---------|
| 1000 | Point geometry |
| 2000 | Line geometry |
| 3000 | Area / Polygon footprint |
| 4000 | 3D solid geometry |

---

### German Municipality Key (`Gemeindeschluessel`) — Structure

Format: `LLRRRKGG` (8 digits)

| Digits | Meaning |
|--------|---------|
| LL | Federal state (Bundesland) — e.g. `02` = Hamburg |
| RRR | District / Kreis — e.g. `100` = Hamburg (city-state has one district) |
| K | Municipality type |
| GG | Municipality sub-key |

Common Hamburg keys:

| Key | District / Stadtteil |
|-----|----------------------|
| `02100` | Hamburg (city-state) |
| `02100104` | Hamburg-Mitte / HafenCity |
| `02100401` | Hamburg-Nord |
| `02100601` | Altona |
| `02100701` | Eimsbüttel |
| `02100901` | Wandsbek |
| `02101001` | Bergedorf |
| `02101101` | Harburg |
