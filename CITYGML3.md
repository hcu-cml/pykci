# CityGML 3.0 in pykci: codec, model, mapping, tests

pykci reads and writes **CityGML 2.0 and 3.0** around one version-neutral graph model.
CityGML 3.0 data (for example transportation spaces of intersections, or street-space
tiles) goes into the same graph as a 2.0 city model. It comes back out as valid 3.0, or as
valid 2.0 with a loss report.

| File | Role |
|---|---|
| `citygml_profiles.py` | Version profiles: namespaces (CityGML 2.0/3.0, GML 3.1.1/3.2, xAL 2/3), structural names (LoD properties in `core:` vs the module, `core:boundary` vs `bldg:boundedBy`, `gml:Shell` vs `gml:CompositeSurface`, `relativeGeometry` vs `relativeGMLGeometry`, member wrappers, schemaLocations), `detect_citygml_version()` |
| `ingest_citygml.py` | `ingest()` picks the codec from the root namespace. The 2.0 path is unchanged (byte-identical graph, see tests); the 3.0 codec is `ingest_citygml3()` and its collectors |
| `citygml_writer.py` | Strict writer: graph loader (with clipping), 3.0 serializer (`Writer3`), 2.0 serializer (`Writer2`), loss report, streaming document driver |
| `export_citygml.py` | CLI (`--version 2/3`, `--dataset`, `--bbox`, `--writer`, `--v2-subfeatures`) and the legacy 2.0 writer, with three export defects fixed (§5) |
| `eval/roundtrip_census.py` | Element / attribute / text / coordinate census, now with `--include-appearance`, an object-count table and `--json` |
| `tools/validate_citygml.py` | Offline XSD validation of CityGML 2.0 and 3.0 (`--setup` downloads the OGC schema bundle once into `~/.cache/pykci/citygml-schemas`) |
| `eval/citygml3_tests.py` | End-to-end test runner (T1–T11, below) against a throwaway Neo4j |
| `eval/fixtures/citygml3/` | XSD-valid synthetic fixtures: `kitchen_sink.gml` (every element type), `tile_a.gml` and `tile_b.gml` (street-space tile pair), `README.txt` with expected counts |

## 1. Codec architecture

**Ingest.** `detect_citygml_version()` reads only the root start tag. CityGML 3.0 goes to
`ingest_citygml3()`, everything else to the untouched 2.0 code.

The 3.0 codec streams `core:cityObjectMember` and `core:appearanceMember`. It does not
stream feature tags, so nested features and unknown top-level types are handled
explicitly and reported. Each member is dispatched by its qualified tag (profile names,
no namespace wildcards) to one of these collectors:

- `_collect_building3`: Building and BuildingPart.
- `_collect_city_object3`: the transportation spaces; frn, veg and wtr features; OtherConstruction; LandUse; the four gen:Generic* classes; Bridge.
- Shared helpers:
  - `_collect_space_common3` handles `core:boundary` plus all core LoD geometry (lod0Point, lod0/2/3MultiSurface, lod0/2/3MultiCurve, lod1/2/3Solid, lod1–3TerrainIntersectionCurve, lod1–3ImplicitRepresentation; every LoD is kept).
  - `_collect_thematic_surface3` handles boundaries and filling surfaces.
  - `_collect_sub_feature3` handles Section, Intersection, TrafficSpace, AuxiliaryTrafficSpace, Hole, ClearanceSpace and Marking.
  - `_collect_implicit3` and `_collect_prototype3` handle implicit geometry.
  - `_collect_appearance3` and `_collect_relations3` handle appearances and relations.

Rows go into the same batches as in 2.0 and are written by the same writer threads with
idempotent `MERGE`. The new batch keys are written only when non-empty, so a 2.0 batch
flushes exactly as before.

xlinks whose target lies inside the current feature (for example a building's
`gml:Shell` members, which point at its own boundary polygons) become normal batch
edges. All other references are **deferred**: relatedTo, predecessor/successor,
appearance targets, xlinked geometry properties, and template references
(`relativeGeometry xlink:href`). They are resolved after parsing by the fragment id.
Unresolved ones are **parked** on the source node (`pending_refs_json` + plumbing label
`PendingRefs`) and retried after every 3.0 ingest, at the end of every CLI run, and by
`--resolve-pending`. A reference into a tile that is ingested later therefore becomes an
edge as soon as that tile arrives.

**Export.** The 2.0-sourced → 2.0 path uses the legacy writer. Its output is identical
to before except for the fixes listed in §5. Every other path uses the strict writer:

- `GraphTree` loads each selected top-level feature's owned subtree by following
  containment edges, and records reference edges and xlinked members without expanding
  them. Each expansion step names its relationship types (no untyped `(n)-[r]->(c)`
  scan over the dense reference edges), and the clip test runs in Cypher, so a clipped
  child returns one marker row instead of its properties.
- `Writer3` and `Writer2` write every element block by block in the xs:sequence order of
  the target content model: gml head → boundedBy → lifespan → CityObject block → Space or
  ThematicSurface block → PhysicalSpace → OccupiedSpace → construction block → module
  properties. The order was derived from the XSDs and is checked by
  `tools/validate_citygml.py`.
- gml:ids are written only once (a second occurrence becomes an xlink). Ids that the
  ingest synthesised are not written.
- An implicit template is written inline once, at its first use in the document, and
  referenced afterwards.
- Appearance targets whose geometry is not in the document are removed (reported as a
  loss, or as an export statistic in an area-limited export).
- **Dataset-level appearances** (`dataset_appearances()`) are driven by the written
  geometry ids: the TARGETS edges are looked up from the geometry side in batches of
  5,000 ids, only the materials hit are fetched, and each material is written once
  inside its host Appearance (the one with the smallest id that holds it, in the
  original surfaceData order). A single-file Dataset keeps its appearanceMembers
  unchanged; a tiled Dataset collapses to the few host Appearances. A whole-Dataset export also keeps materials without written targets
  and counts unwritten targets as a loss from the relationship degree, without loading
  them.
- Everything the target version cannot express is counted in `<out>.loss.json`.
- **Streaming:** top-level features are loaded, written (`lxml.etree.xmlfile`) and released
  in batches of 200, so memory is bounded by the batch, not by the document. A *plan*
  (an id-only, level-wise BFS from the selected features over the feature edges,
  applying the same clip test) lists the feature-level ids the document will contain. It
  decides whether a relatedTo / predecessor / successor reference or an xlinked
  MultiSurface can stay local. An xlinked MultiSurface whose owner is not exported is
  written inline instead.
- **`--bbox` + `--clip`** (clip is on by default with `--bbox`): see §4a.

## 2. Version-neutral graph model (what 3.0 adds)

The 3.0 codec reuses the canonical pykci pattern:

- Feature node → `HAS_BOUNDARY` → `BoundarySurface` → `HAS_POLYGON` → `GeometryPolygon` → `GeometryRing{pos_list verbatim}`.
- `HAS_LOD_SOLID`, `HAS_LOD_SURFACE`, `HAS_LOD_MULTICURVE`, `HAS_TERRAIN_INTERSECTION`.
- The `Entity` plumbing label and `source` on every node.

3.0 buildings produce **the same graph as 2.0 buildings**, so `build_3dtiles.py`,
`export_citygml.py` and existing queries work unchanged. What both versions have uses the
2.0 nodes and edges (BuildingRoom → `Room` via `HAS_INTERIOR_ROOM`, BuildingFurniture and
BuildingInstallation with the 2.0 keys `class_code` / `function_code` / `usage_code`,
Window / Door → `Opening`); what only 3.0 has gets its own node or edge (Storey /
BuildingUnit, BuildingConstructiveElement as the `NestedFeature` already used for bridge
elements, con:Window / con:Door fillings, PointCloud). The module both versions exclude
(appearance aside) is the same: nothing of the FZK-Haus models is dropped in either.

**Version marker.** Every feature-level node (features, sub-features, thematic surfaces,
openings, rooms, installations, furniture, subdivisions, point clouds, appearances and
surface data) records `citygml_version` ('2.0' / '3.0') of the file it came from; geometry
nodes are version-neutral. `export_citygml.py --writer auto` uses the legacy 2.0 writer only
when the Dataset and all its top-level features are 2.0-sourced, and the strict writer's
loss report lists the exported feature-level nodes per source version (`source_versions`).

Additions (all documented in GRAPH.md):

```text
(Road|Railway|Track|Square|Waterway :CityObject)
   -[:HAS_SECTION {order}]-> (Section:TransportationSpace)
   -[:HAS_INTERSECTION {order}]-> (Intersection:TransportationSpace)
(Road|…|Section|Intersection) -[:HAS_TRAFFIC_SPACE {order, auxiliary}]-> (TrafficSpace|AuxiliaryTrafficSpace)
(…) -[:HAS_MARKING {order}]-> (Marking)   (…) -[:HAS_HOLE {order}]-> (Hole)-[:HAS_BOUNDARY]->(BoundarySurface:HoleSurface)
(TrafficSpace) -[:HAS_CLEARANCE_SPACE {order}]-> (ClearanceSpace)
(TrafficSpace) -[:PREDECESSOR|SUCCESSOR {order, href?}]-> (TrafficSpace)
(space) -[:HAS_BOUNDARY {order, prop:'boundary'}]-> (BoundarySurface:TrafficArea|AuxiliaryTrafficArea)
(Road)  -[:HAS_BOUNDARY {order, prop:'trafficArea'|'auxiliaryTrafficArea'}]-> (…)        # 2.0 areas
(space|Marking|surface) -[:HAS_LOD_MULTICURVE {lod:'0', order, multi_curve_id}]-> (GeometryLineString)
(Entity) -[:RELATED_TO {order, relation_type, relation_type_codespace, relation_id?, gen_attrs_json?}]-> (Entity)
(feature) -[:HAS_PROTOTYPE {lod, transformation_matrix, reference_point, reference_point_id, via_xlink}]-> (GeometryPrototype)
(Dataset|Entity) -[:HAS_APPEARANCE]-> (Appearance)-[:HAS_SURFACE_DATA]->(X3DMaterial|ParameterizedTexture|GeoreferencedTexture)
                 -[:TARGETS {order, texture_coordinates_json?}]-> (GeometryPolygon|…)
(Bridge) -[:HAS_BRIDGE_PART]-> (BridgePart)   (Bridge|BridgePart) -[:HAS_NESTED {prop}]-> (NestedFeature)
(Building|BuildingPart) -[:HAS_SUBDIVISION {order}]-> (BuildingSubdivision:Storey|BuildingUnit)
(Building|BuildingPart|BuildingSubdivision) -[:HAS_NESTED {prop:'buildingConstructiveElement'}]-> (NestedFeature)
          -[:HAS_OPENING {order, prop:'filling'}]-> (Opening {filling_type:'Window'|'Door'})
(Building|BuildingPart|BuildingSubdivision) -[:HAS_INTERIOR_ROOM|HAS_OUTER_INSTALLATION|HAS_INTERIOR_INSTALLATION|HAS_INTERIOR_FURNITURE {order}]-> (…)
(Room) -[:HAS_INTERIOR_FURNITURE|HAS_ROOM_INSTALLATION {order}]-> (BuildingFurniture|BuildingInstallation)
(space|BoundarySurface|Opening) -[:HAS_POINT_CLOUD]-> (PointCloud {mime_type, point_file, point_file_srs_name,
          points_pos_list, points_srs_dimension, points_count, points_member_style, multipoint_id?})
(Entity) -[:HAS_DYNAMIZER {order}]-> (Dynamizer {attribute_ref, attribute_name, start_time, end_time,
          connection_type, observation_property, uom, sensor_id, sensor_name, observation_id, datastream_id,
          base_url, auth_type, mqtt_server, mqtt_topic, link_to_observation, link_to_sensor_description,
          sensor_location, dynamic_data_type, dynamic_data_xml, first/last_timestamp, value_type, n_values,
          live_* (written by a live job, never exported)})
```

Rules that follow GRAPH.md:

- **Sub-features never get `CityObject` nor `PART_OF Dataset`.** This follows the BuildingPart precedent. Verified: 0 leaks in every test.
- **Provenance.** `source` is on every node. `--source-type / --method / --method-version` go on the Dataset and on every feature, sub-feature, surface, prototype, appearance and surface-data node (the 2.0 path now does the same for its features, sub-features, surfaces and openings). High-cardinality geometry leaves carry only `source`. `--source-type` predicted, reconstructed and derived need `--method` and `--method-version`.
- **One R-tree layer per source.** `--rtree-layer streetspace` (or any other name) registers the top-level features in their own layer and stamps `rtree_layer` on each node. `_index_features_spatial` and `ingest_osm`'s post-fusion indexer skip foreign-layer nodes.
- **Never overwrite authoritative values.** `RESERVED_NODE_KEYS` now contains every module scalar (`class`, `function`, `usage`, `height`, `granularity`, `surface_material`, `lod`, …). A generic attribute with such a name is stored as `gen_<key>`; the kitchen sink tests this.
- **Reuse the geometry chain.** No new geometry node types. Points are `GeometryLineString{geometry_type:'Point'}` (as relief mass points already are), CompositeSolid and MultiSolid are `GeometrySolid{geometry_type}` with `HAS_SOLID_MEMBER`, and templates are `GeometryPrototype` (the street-tree node) holding polygons, solids or lines.
- **No hardcoding.** CRS, LoD, codes and code spaces are all read from the file.

**Merge policy (street-space tiles).** Features MERGE on gml:id, with no id splitting (the 2.0 duplicate tiers are not used). The same Road / Section / TrafficSpace arriving in several tiles behaves like this:

- Children accumulate.
- `_gen_attrs_map` is unioned.
- The bbox is union-merged. A feature already in the R-tree whose extent grew is flagged `rtree_stale` and re-indexed.
- `source` holds the last file and `source_files` all files.

Children without a gml:id get deterministic **content-hash** ids (`synthetic_id`), so
re-ingest is idempotent and copies from different tiles never collide.

**Source scope.** Only files of one Dataset merge: `Dataset.source_files` lists them, and a
pre-flight pass refuses a file (nothing written) whose gml:ids belong to a node of
another source. `--id-prefix` gives such a source its own id space (recorded as
`Dataset.id_prefix`, stripped by the strict writer, which `export_citygml` then uses for
2.0 too). References resolve only to a node of the referenced document, also across
Datasets (GRAPH.md, *Source scope of gml:ids* and *Pending references*).

**Multi-valued codes.** 3.0 `function` and `usage` are 0..*. The first value stays the
queryable scalar (as in 2.0); with several values the full list is kept in `<key>_list`.

**Version-neutral derived scalars.** On buildings and constructions:

- `measured_height` comes from the con:Height with `status=measured`. All Heights are kept verbatim in `heights_json`.
- `year_of_construction` comes from `con:dateOfConstruction`, which is also kept verbatim.

## 3. 2.0 ↔ 3.0 mapping and loss policy

| Graph (version-neutral) | CityGML 3.0 | CityGML 2.0 | 3.0 → 2.0 | 2.0 → 3.0 |
|---|---|---|---|---|
| `Building`/`BuildingPart` + `HAS_BOUNDARY` → `BoundarySurface` | `core:boundary/con:WallSurface…`, `core:ClosureSurface` | `bldg:boundedBy/bldg:WallSurface…` | lossless | lossless |
| `GeometrySolid` (+`composite_surface_id`) | `core:lodNSolid/gml:Solid/gml:exterior/gml:Shell` | `bldg:lodNSolid/…/gml:CompositeSurface` | lossless | lossless (LoD4 → LoD3 if free) |
| `measured_height`, `heights_json`, `height_*` | `con:height/con:Height{high/lowReference, status, value}` | `bldg:measuredHeight` | first measured height; references, status and extra Heights reported | `highestRoofEdge` / `lowestGroundPoint` / `measured` assumed (reported) |
| `year_of_construction`, `date_of_construction` | `con:dateOfConstruction` (date) | `bldg:yearOfConstruction` | year kept; full date, conditionOfConstruction and demolition date reported | `YYYY-01-01` (reported) |
| `address_xml` (verbatim) | xAL 3 `core:Address` | xAL 2 `AddressDetails` | converted (country, locality, street, number, postcode) and reported | converted and reported |
| `Opening` | `con:fillingSurface/con:WindowSurface/DoorSurface` | `bldg:opening/bldg:Window/Door` | LoD3/4 kept; LoD0–2 reported | lossless |
| `BuildingSubdivision:Storey\|BuildingUnit` (`HAS_SUBDIVISION`) | `bldg:buildingSubdivision/bldg:Storey\|BuildingUnit` (class, function, usage, elevation, sortKey, members) | - | flattened: their boundary surfaces, installations and rooms are written on the building; the subdivision, its attributes and own geometry reported | - |
| `NestedFeature{BuildingConstructiveElement}` + `Opening{filling_type Window\|Door}` | `bldg:buildingConstructiveElement` + `con:filling/con:Window\|Door` | - | reported | - |
| `Room` + furniture + room installations | `bldg:buildingRoom/BuildingRoom` (any LoD), `buildingFurniture`, `buildingInstallation`, `roomHeight` | `bldg:interiorRoom/Room` (LoD4), `interiorFurniture`, `roomInstallation/IntBuildingInstallation` | written as LoD4 (policy: the highest LoD of each geometry kind, reported); roomHeight and furniture outside rooms reported | LoD4 → LoD3 |
| `HAS_INTERIOR_INSTALLATION` | `bldg:buildingInstallation` with `con:relationToConstruction inside` | `bldg:interiorBuildingInstallation/IntBuildingInstallation` | written as LoD4 (policy) | `relationToConstruction inside` |
| `PointCloud` (`HAS_POINT_CLOUD`) | `core:pointCloud/pcl:PointCloud` | - | reported | - |
| Building `HAS_LOD_SURFACE{role}` | `core:lod0/2/3MultiSurface` | `lod0FootPrint` / `lod0RoofEdge` / `lodNMultiSurface` | lod0 → lod0FootPrint (policy) | footprint → lod0MultiSurface; roofedge and lod1MultiSurface reported |
| Road / Railway / Track / Square | `tran:*` spaces | `tran:*` complexes | kept | kept |
| `Waterway` | `tran:Waterway` | - | `tran:TransportationComplex` (reported) | - |
| `Section`, `Intersection`, `TrafficSpace`, `AuxiliaryTrafficSpace`, `Hole`, `ClearanceSpace`, `Marking` | spaces / thematic surface | - | flattened: every (Auxiliary)TrafficArea below the road becomes `tran:trafficArea` / `auxiliaryTrafficArea`; the spaces themselves are reported, or kept as `citygml3_*` gen:stringAttributes with `--v2-subfeatures genattrs` | - |
| `TrafficArea` on the Road (2.0) | - | `tran:trafficArea` | - | one synthesised `TrafficSpace` / `AuxiliaryTrafficSpace` `<ta_id>__space`, `granularity` `way` (reported) |
| `PREDECESSOR` / `SUCCESSOR`, `granularity`, `trafficDirection`, `occupancy` | `tran:*` | - | reported (or gen attrs) | - |
| lod0 curves (`HAS_LOD_MULTICURVE`) | `core:lod0MultiCurve` on spaces / areas / markings | `tran:lod0Network` | all lod0 curves of a road → one `lod0Network/gml:CompositeCurve`; area-level curves reported | `lod0Network` → `core:lod0MultiCurve` |
| `RELATED_TO` | `core:relatedTo/CityObjectRelation` | - | reported (or gen attr `citygml3_relatedTo_i`) | - |
| `HAS_PROTOTYPE` → `GeometryPrototype` | `core:lodNImplicitRepresentation` (template inline once, then `relativeGeometry xlink:href`) | `<module>:lodNImplicitRepresentation/core:ImplicitGeometry` (`relativeGMLGeometry`) | kept | kept (also the street-tree variant) |
| CityFurniture explicit geometry | `core:lod2MultiSurface` / `lod1Solid` / `lod2MultiCurve` / `lod0Point` | `frn:lodNGeometry` (one per LoD) | one geometry per LoD (others and lod0 reported) | kept |
| PlantCover | `core:lod0/2/3MultiSurface`, `lodNSolid`, `averageHeight`/`minHeight`/`maxHeight` | `veg:lod1–4MultiSurface`, `lodNMultiSolid`, `averageHeight` | lod0MultiSurface, minHeight and maxHeight reported | kept |
| SolitaryVegetationObject | `core:*`, `rootBallDiameter`, `maxRootBallDepth` | `veg:lodNGeometry`, implicit | the two 3.0-only measures reported | kept |
| Bridge / BridgePart / `NestedFeature` | `brid:Bridge`, `bridgePart`, `bridgeConstructiveElement`, `bridgeInstallation` | `brid:Bridge`, `consistsOfBridgePart`, `outerBridgeConstruction`, `outerBridgeInstallation` | kept; con:height reported | kept |
| OtherConstruction, Generic*Space, GenericThematicSurface | `con:OtherConstruction`, `gen:Generic*` | - | `gen:GenericCityObject` (reported) | `GenericCityObject` → `GenericOccupiedSpace` |
| WaterBody | `wtr:WaterBody` + `core:boundary` WaterSurface / WaterGroundSurface / `core:ClosureSurface` | `wtr:WaterBody` + `wtr:boundedBy` (… `WaterClosureSurface`, `lodNSurface`) | lod2/3 MultiSurface on the body reported | kept |
| LandUse | `luse:LandUse` (thematic surface) | `luse:LandUse` | kept | kept |
| Appearance | `core:appearanceMember` / `core:appearance`, `app:surfaceData`, TextureAssociation + TexCoordList | `app:appearanceMember` / `app:appearance`, `surfaceDataMember`, `target uri` + `TexCoordList` | kept (targets of dropped geometry removed and reported) | (2.0 appearance is not ingested) |
| Generic attributes (type + set + uom + codeSpace) | `core:genericAttribute/gen:<Type>Attribute`, `gen:GenericAttributeSet` | `gen:<type>Attribute name=…`, `genericAttributeSet` | `CodeAttribute` → `stringAttribute` (codeSpace reported) | kept |
| `external_reference_xml` | `core:ExternalReference{targetResource, informationSystem, relationType}` | `core:externalReference{informationSystem, externalObject/name\|uri}` | converted; relationType reported | converted |
| `valid_from`/`valid_to`, `space_type`, `volume_xml`/`area_xml`, `gml_identifier` | 3.0 / GML 3.2 | - | reported | - |
| `HAS_DYNAMIZER` → `Dynamizer` | `core:dynamizer/dyn:Dynamizer`: attributeRef, start/endTime, `dyn:sensorConnection/SensorConnection` (fields as scalars) and/or `dyn:dynamicData` (timeseries kept verbatim) | - | reported (`dyn:Dynamizer (no 2.0 equivalent)`) | - |
| Tunnel, ReliefFeature, CityObjectGroup | - (not in the 3.0 codec) | legacy writer | - | reported (not written) |

## 4. CLI

```bash
# CityGML 3.0 file into a given Dataset and R-tree layer (provenance optional)
python ingest_citygml.py intersections.gml \
    --dataset-name intersections --rtree-layer citygml3 --source-type authoritative

# street-space tiles (a directory or several files) into ONE Dataset
python ingest_citygml.py tiles/ --dataset-name city_streetspace --rtree-layer streetspace \
    --source-type reconstructed --method streetspace-recon --method-version 1.0
python ingest_citygml.py --resolve-pending        # after loading more tiles out of order

# a second model of buildings that are already in the graph, with the same ids
python ingest_citygml.py other_model.gml --dataset-name other_model --rtree-layer other_model \
    --id-prefix other_ --source-type authoritative --district HafenCity

# export
python export_citygml.py --dataset city_streetspace --version 3 --out streetspace_v3.gml
python export_citygml.py --dataset city_streetspace --version 2 --out streetspace_v2.gml   # + .loss.json
python export_citygml.py --dataset city_streetspace --version 2 --v2-subfeatures genattrs
python export_citygml.py --dataset intersections --version 3 \
    --bbox "POLYGON((565090 5933680, 565220 5933680, 565220 5933780, 565090 5933780, 565090 5933680))"
python export_citygml.py --dataset <2.0 dataset> --version 2 --writer strict   # XSD-ordered 2.0

# validate
python tools/validate_citygml.py --setup                          # once: OGC schema bundle
python tools/validate_citygml.py streetspace_v3.gml --version 3
```

Connection for both scripts: `--uri/--user/--password`, defaulting to `$NEO4J_URI`,
`$NEO4J_USER`, `$NEO4J_PASSWORD`, or else the previous hardcoded values.
`--dataset` takes a Dataset id, name or source. It is required when the graph holds
several Datasets; before, an arbitrary one was exported.

## 4a. Sub-feature bboxes, `--backfill-bbox`, clipped bbox export

**Why.** A Road spans its whole street, sometimes kilometres. When the R-tree selects
such a Road for a 400 m box, the old writer pulled the Road's complete subtree into one
in-memory document (on a city-wide street space: many GB of memory).

**Ingest.** The 3.0 codec makes one post-order pass per top-level feature and stores
`bbox_min/max_x/y/z` on every sub-feature and every BoundarySurface/Opening:

- a surface: its polygons and curves;
- a space: the union of its boundaries and lod0 curves;
- a Section/Intersection: the union of its spaces.

When the same id arrives from several tiles the bbox is union-merged. Sub-features get
no `center`/`bbox_wkt` and are not R-tree indexed.

**Backfill for older graphs.** `ingest_citygml.py --backfill-bbox [--dataset ID]`
computes the same values from the graph: own polygons (exterior and interior rings),
MultiSurfaces, solids, curves and points, unioned with the contained sub-features.

- It works per batch of 200 top-level features that have sub-features.
- Inside a batch it goes deepest node first, so every value it writes is final.
- Each query handles at most 1,000 nodes, which fits a 2 GB heap.
- It touches only nodes still missing `bbox_min_x`, so it is idempotent and resumable.

**Clipped export.**

- The R-tree selects the top-level features.
- Below a transportation feature (Road, Railway, Track, Square, Waterway), the loader skips any child (Section, Intersection, TrafficSpace, AuxiliaryTrafficSpace, Hole, ClearanceSpace, Marking, boundary area) whose bbox misses the box, before expanding it.
- Containers without kept content are pruned. A top-level transportation feature is written only when at least one sub-feature is kept.
- Buildings, furniture, bridges, water bodies and other features are written whole.
- Material targets are written only for polygons in the document.
- `--no-clip` writes every selected feature whole.

**Scaling.** Every export step is proportional to the selection, never to the Dataset:
R-tree selection → plan BFS over the selected subtree → batched feature load/write →
appearance targets looked up by written ids. `--profile` prints time and peak memory per
phase (also in `<out>.loss.json` under `profile`). A tiled street space can hold hundreds
of tile Appearances sharing a few dozen X3DMaterials through millions of TARGETS edges, so
materials are never expanded: a 400 m box of a city-wide street space exports in seconds
with about 120 MB of memory, a 2 km box in under a minute with about 420 MB.

```bash
python ingest_citygml.py --backfill-bbox --dataset city_streetspace   # once for older graphs; resumable
python export_citygml.py --dataset city_streetspace --version 3 --profile \
    --bbox "POLYGON((565000 5933500, 565400 5933500, 565400 5933900, 565000 5933900, 565000 5933500))" \
    --out streetspace_box_v3.gml
```

## 5. Fixes to existing behaviour

- **2.0 ingest is unchanged** for every file without `tran:trafficArea`, `auxiliaryTrafficArea` or `lod0Network`: identical graph digests on the regression samples (§6). Those three elements, previously dropped silently, are now ingested as `BoundarySurface:TrafficArea…` and network curves. The legacy writer re-emits them.
- **`RESERVED_NODE_KEYS`** now covers all module scalars, so a generic attribute named `height` or `function` can no longer clobber the module value. This also applies to 2.0 files: a generic attribute with such a name (`class`, `function`, `usage`, `height`, `lod`, `method`, `source_type`, …) is now stored as `gen_<key>` (the export restores its name). The 2.0 regression samples have none, so their graphs are unchanged.
- **gml:id collisions across sources** (both codecs) are refused before anything is written, see *Source scope*; `--id-prefix` resolves them.
- **District.** A 3.0 file without a Gemeindeschluessel no longer gets a District named after the file; `--district` names one (also for 2.0, where the file name stays the fallback).
- **Windows.** The export reads its peak memory without the POSIX-only `resource` module, the scripts write their reports as UTF-8, and console output no longer fails on characters the console cannot print.
- **R-tree.** `spatial.addNodes` is not idempotent (verified: a re-add creates a second `RTREE_REFERENCE` and a duplicate hit). `_index_features_spatial` now skips indexed nodes and respects `rtree_layer`; `ingest_osm.index_cityobjects_post_fusion` does the same.
- **Export defects:**
  - Features are selected by `PART_OF` the Dataset, not `source`; tiled datasets previously exported no buildings.
  - The Dataset is chosen explicitly.
  - Prototype emission: a `GeometryPrototype` (street trees) is written inline once, and referencing instances use `relativeGMLGeometry xlink:href`. The invalid `<gml:MultiSurface xlink:href>` is gone, which removes 12 of Railway's 21 legacy validation errors.
  - Legacy 2.0 writer, building element order: lodNSolid / MultiSurface / MultiCurve / TerrainIntersection are now written per LoD in the 2.0 sequence, with the installations and `boundedBy` between LoD2 and LoD3 (the solid used to follow `boundedBy`).
  - Verbatim fragments (address, metadata, external references, ADE) are appended without the source root's unused namespace declarations. An FZK-Haus address carried the file's default namespace (`profiles/base/2.0`), under which lxml wrote `core:Address` unprefixed, i.e. in the wrong namespace.
  - With both fixes all five FZK-Haus 2.0 models (LoD0-4) export through the legacy writer XSD-valid and census-lossless.
  - Strict writer: a polygon referenced by xlink (a solid's shell) whose owner is not written in the target version (an LoD2 window surface in 2.0) is written inline at the reference; members whose target is still pending (missing from the source) are written as the reference the source had.

## 6. Test results

All tests run against a throwaway Neo4j (neo4j 2025.10.1 community with APOC core and
Neo4j Spatial 2025.10.0); the runner wipes it:

```bash
python tools/validate_citygml.py --setup
python eval/citygml3_tests.py --uri bolt://localhost:7688 --password … --wipe \
    [--sample city3.gml] [--tiles tile_1.gml tile_2.gml …]
```

T5, T6 and T11 use only the fixtures in `eval/fixtures/citygml3/`. T1 to T4 and T7 run with
`--sample` (a real CityGML 3.0 file; the expected counts are read from the file), T8 to T10
with `--tiles`. T1 to T7, T11 and T12 below were run on Windows against Neo4j 2025.10.1
with a CityGML 3.0 intersection (64 buildings, 210 traffic spaces, 105 furniture objects)
as the sample; T8 to T10 are from the original run on three neighbouring street-space
tiles, which are not public:

| Test | Result |
|---|---|
| **T1** ingest of the sample (5.3 s) | All counts equal the file: 1 Road, 210 TrafficSpace, 37 AuxiliaryTrafficSpace, 210 TrafficArea, 37 AuxiliaryTrafficArea, 7 PlantCover, 8 Bridge, 105 CityFurniture, 64 Building, 1,344 Wall, 295 Roof and 64 Ground surfaces, 7 GeometryPrototype, 105 HAS_PROTOTYPE (7 inline + 98 xlink), 200 RELATED_TO, 2,284 polygons, 64 TerrainIntersection. 0 sub-feature leaks, 0 pending references |
| **T2** idempotency | Re-ingest gives an identical graph digest (16,555 nodes + edges); 185 R-tree references for 185 nodes |
| **T3** export 3.0 | XSD **VALID**, 0 unresolved references (6,740 gml:id, 2,001 xlinks, as in the source). Census incl. appearance: **lossless** (0 missing elements, attributes, texts or coordinates), loss report 0 items |
| **T4** export 2.0 | XSD **VALID**, 0 unresolved. Loss report 575 items: 247 spaces flattened, 200 relations, 64 address conversions, 64 Height references |
| **T5** kitchen sink (every element type) | 3.0 VALID and lossless incl. appearance, every object type 1:1 (61 types); 2.0 VALID |
| **T6** tile merge (tile_b, then tile_a) | One `road_shared` with `source_files` = 2; the Section has 6 spaces, the shared TrafficSpace 4 areas; 3 cross-tile refs (written before their target existed) resolved through PendingRefs; re-ingest identical; merged 3.0 and 2.0 exports VALID |
| **T7** 2.0-sourced → 3.0 | The sample's 2.0 export re-ingested (247 2.0 TrafficAreas) → 3.0 with 247 synthesised `__space` spaces, VALID |
| **T8** three real street-space tiles | 15,326 top-level features in one Dataset (ingest 66 s in total; 58 MB tile ≈ 19 s, 274 MB RSS); 3.0 and 2.0 exports VALID (≈ 29 s each) |
| **T9** clipped 400 m `--bbox` export (two of the tiles) | 3.0: VALID, 0 unresolved, 19.7 MB, 2.7 s, **119 MB peak RSS**, 1,791 areas/markings, none entirely outside the box, no empty Road. 2.0: VALID, 13.3 MB, 2.3 s, 112 MB |
| **T10** backfill | After the sub-feature bboxes are removed, `--backfill-bbox` rewrites all 12,786 with 0 value differences from the ingest values (2.9 s); a re-run writes 0 |
| **T11** dynamizer | `eval/fixtures/citygml3/dynamizer.gml` (a SensorConnection Dynamizer and a GenericTimeseries Dynamizer with 3 values): 2 `Dynamizer` sub-features (0 CityObject / PART_OF leaks), re-ingest identical, 3.0 export XSD-valid and lossless, 2.0 export valid with 2 dynamizers in the loss report |
| **T13** FZK-Haus, 26 models (KIT IAI; 21 CityGML 3.0 in four variants: with and without storeys, boundary surfaces or constructive elements, windows, interior, point clouds; 5 CityGML 2.0, LoD0-4) | Every 3.0 model: 3.0 export VALID and census lossless (appearance excluded), 2.0 export VALID; every 2.0 model: legacy 2.0 export VALID and lossless, 3.0 export VALID. Every feature-level node carries its source's `citygml_version`. The only unresolved references are the 11 the source `LoD2_Storeys_BoundarySurfaces/06-PointCloud` itself leaves dangling (shell members), re-emitted as in the source. Appearance is excluded because some 3.0 files texture ids they do not contain (an empty `app:target`, a target with a stray `"`, targets to absent ids) and 2.0 appearance is not ingested; with appearance 14 of the 21 3.0 models are lossless. The 3.0 models are not tracked (326 MB): download them from the [CityGML wiki](https://www.citygmlwiki.org/index.php?title=FZK_Haus_CityGML_30) into `input/citygml3/FZK-Haus/<variant>/<n>/`; without them T13 runs the 2.0 models only (`citygml3_models: 0`) |
| **T12** source scope | A copy of `tile_a.gml` (same ids, other file) into a second Dataset is refused with nothing written; with `--id-prefix two_` it becomes 125 nodes of its own, none shared with the first Dataset, whose nodes stay unchanged; the copy of `tile_b.gml` resolves its 3 references into `tile_a.gml` to the first Dataset's nodes; the export is VALID and has no prefixed id. The same for a 2.0 model (FZK-Haus LoD2): refused, then ingested with a prefix and exported as VALID 2.0 without it |

**Real-tile merge.** In the T8 Dataset:

- 16 Roads, 18 Sections, 41 TrafficSpaces and 42 AuxiliaryTrafficSpaces are shared by two neighbouring tiles and merged.
- No duplicate ids, no sub-feature leaks.
- All 15,326 top-level features have a bbox and exactly one R-tree reference (layer `streetspace` only). This includes PlantCovers whose only geometry is an xlinked `lod0MultiSurface`.
- 666 cross-tile PREDECESSOR/SUCCESSOR edges are resolved. 635 references (on 188 nodes) into neighbouring tiles that were not loaded stay parked and are re-emitted with their original href.

Census of two source tiles against their merged 3.0 export:

- Every element, coordinate and text is present.
- The only differences are the 333 cross-tile hrefs per direction, which became local `#id` because both tiles are in the document, the merged root envelope, and the second tile's CityModel name.
- XSD: VALID with 98,759 gml:ids, 95,339 material targets and 550 external hrefs.

**2.0 regression** (main as baseline, same DB, `nearby_edges=False`; rerun after the source-scope change: identical apart from the new `Dataset.source_files`):

- FZK-Haus LoD0–LoD4, `new_elements_test.gml`, Railway_Scene_LoD3: **identical graph digests** (every node, edge and property, random ids masked), and identical node and edge census in all sections.
- The legacy 2.0 exports are identical after masking random ids, except Railway. There, exactly the 12 implicit-geometry references changed from `<gml:MultiSurface xlink:href>` to `<relativeGMLGeometry xlink:href>`, and the XSD errors dropped from 21 to 9.
- All baseline legacy exports were XSD-invalid before this work (lod2Solid order, address namespace, installation order); since the building-module work (§5) the FZK-Haus ones are valid. `--writer strict` writes them valid too: FZK LoD0-4 and new_elements_test as 2.0 and 3.0, Railway as 3.0 (Tunnel, Relief and Group reported).

**Other checks.**

- `build_3dtiles.py --tiled` draws all 185 top-level features of the 3.0 sample (10,287 triangles; before the 3.0 support: 79, without the road and the 105 furniture objects) and 22 of the 24 kitchen-sink features (the other two have only lines or points).
- A mixed graph (FZK 2.0 in `features` + the 3.0 sample in `citygml3`) keeps both layers separate.
- A bbox export through the Dataset's own layer is valid.

## 7. Limitations

- **Not ingested in 3.0** (counted in the ingest summary, never dropped silently): tun:Tunnel, dem:ReliefFeature, grp:CityObjectGroup, brid:BridgeRoom / BridgeFurniture, con:constructionEvent, versioning, building members, subdivisions, fillings and point clouds given only by xlink, implicit representations or MultiCurves given only by xlink, `gml:surfaceMembers` arrays, gml:Surface patch types. The 3.0 writer does not write Tunnel, ReliefFeature or CityObjectGroup (reported).
- **2.0 codec gaps are left as they were**, so the 2.0 regression stays identical: 2.0 appearance, PlantCover geometry and polygon `srsName` are not ingested, and polygons without gml:id get random ids. They matter only for the 3.0 → 2.0 → graph → 3.0 chain.
- **Cross-version exports are lossy by design** (table above). Everything lost is in the loss report. A 3.0 building modelled only by constructive elements (the FZK-Haus "Construction" variants) exports to 2.0 without geometry: 2.0 has no element for them.
- **Strict 2.0 → 2.0** (only used with `--writer strict` or an `--id-prefix`) does not repeat `srsDimension` on the root envelope's corners.
- **Memory.** The strict writer streams in batches of top-level features, and every step scales with the selection (§4a: 400 m box about 120 MB, 2 km box about 420 MB). The `written`-id set, the plan and the collected appearance targets grow with the document, so a full-city export needs memory in proportion to its size and is still one huge file. The legacy 2.0 writer still builds its document in memory.
- **Tiled appearances.** A tiled Dataset's per-tile Appearances are not written one by one; their materials are written once each, inside their host Appearance (§1). The Appearance a material was read from in a given tile is therefore not preserved across tiles.
- **Clipping scope.** Only the transportation hierarchy is clipped. A Road's own lod0MultiCurve, and kept surfaces that only partly overlap the box, are written whole (no geometric cutting). Sub-features without a bbox are kept, so run `--backfill-bbox` on graphs ingested before this change. References to features outside the box are written as their original cross-tile href when one exists; otherwise they are dropped and reported (`predecessor/successor/relatedTo target not in export`).
- **Tile merge semantics.** Space-level scalars are last-writer-wins (the reconstruction keeps them identical per tile); the generic-attribute map is unioned. Child `order` values from different tiles can coincide, so the export orders by (order, id). Use `--parse-workers 1` (the default) for tiles that share ids.
- **Edge provenance.** `source` is set on the cross-feature reference edges (RELATED_TO, PREDECESSOR, SUCCESSOR), not on structural edges.
- **Templates.** A template id defined in two tiles of one Dataset is assumed to be the same template (MERGE); in two Datasets it is refused like any other shared id.
- **Source scope by file name.** Ownership is decided by the file name (`source`): two Datasets that each contain a file of the same name are not told apart. A `#id` reference resolves only to a node last written from this file.
- **Root envelopes are taken as given.** The Dataset bbox is the union of the files' root `gml:boundedBy` envelopes; a file whose envelope is larger than its features (seen in practice: z up to 505 m for features below 25 m) widens the Dataset bbox accordingly.
