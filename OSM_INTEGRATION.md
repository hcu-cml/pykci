# OSM → pykci Integration — Concept & Implementation

> Companion to [GRAPH.md](GRAPH.md). This document specifies how OpenStreetMap (OSM)
> data is integrated into the pykci knowledge graph (built from CityGML 2.0 LoD2 /
> ALKIS), how OSM and pykci features are matched across the 1:1 / 1:n / n:1 / n:m
> footprint cases, and how the graph stays **compact** and **provenance-aware** while
> doing so. Implemented by [`ingest_osm.py`](ingest_osm.py).
>
> This integration is the **reference implementation** of the *Provenance & Multi-Source
> Design Principle* in [GRAPH.md](GRAPH.md) — the patterns below (`source` on every node,
> non-destructive `osm_*` namespacing, an `OsmFeature` provenance node, confidence-weighted
> `ENRICHED_BY` edges, a separate R-tree layer, compact `other_tags`) are the templates any
> future source (ML predictions, LoD3, sensor data) should follow.

---

## 1. Goal

pykci already encodes a city as a compact, spatially-indexed labelled property graph
from **authoritative** CityGML/ALKIS data (geometry + cadastral semantics). OSM adds a
**second, crowd-sourced, semantically rich** view of the same city: building use,
names, addresses, POIs, heights, roof shapes, the street network, land use, water and
vegetation. The objective is to **enrich** pykci nodes with everything OSM knows about
the same real-world objects, **without overwriting** authoritative values, **without
inflating** the graph, and while keeping a clear **hint that the data came from OSM**.

The integration is therefore *additive enrichment*, not a merge. pykci nodes remain the
anchors; OSM contributes attributes and new nodes for things pykci does not model.

---

## 2. What OSM stores (data model)

OSM has **no schema**. Everything is built from three primitive element types plus
free-form tags:

| Primitive | What it is | Carries |
| --------- | ---------- | ------- |
| **node** | a single lon/lat point | `id`, tags, version, timestamp, changeset, uid |
| **way** | an ordered list of node refs | open way = polyline; **closed way = area** (e.g. a building) |
| **relation** | an ordered list of member elements, each with a **role** | e.g. `type=multipolygon` (outer/inner rings → polygons with holes), `type=route`, `type=boundary` |

**Tags** are arbitrary `key=value` strings; semantics come by convention from the OSM
*Map Features* wiki, not from a schema. Each element also carries **metadata**:
`version`, `timestamp`, `changeset`, `uid`/`user` — useful provenance.

**Implicit topology.** OSM topology is encoded by *sharing*: two ways that share a node
are connected; a multipolygon relation binds outer and inner rings; a route relation
orders its member ways. There are no explicit geometry objects — geometry is assembled
from node coordinates at read time.

### How GDAL exposes the `.osm.pbf`

We read the PBF through GDAL's OSM driver (`ogr2ogr`/OGR Python), which flattens OSM
into **five layers** (confirmed on `input/osm/hamburg-260629.osm.pbf`):

| OGR layer | Geometry | Holds |
| --------- | -------- | ----- |
| `points` | Point | tagged nodes — trees, POIs, street furniture, entrances |
| `lines` | LineString | open ways — roads, rails, paths, barriers, waterways |
| `multilinestrings` | MultiLineString | line relations — routes |
| `multipolygons` | (Multi)Polygon | **closed-way areas + multipolygon relations** — buildings, landuse, water, forests |
| `other_relations` | GeometryCollection | relations not reducible to the above |

Each layer promotes a fixed set of common keys to columns (`building`, `amenity`,
`landuse`, `natural`, `leisure`, `man_made`, `highway`, `name`, …) and dumps **all
remaining tags** into a single HSTORE-style `other_tags` string
(`"key"=>"value","key2"=>"value2"`). The driver reports **CRS = EPSG:4326 (WGS84
lon/lat)**; the file's extent is the **entire Hamburg state**
(7.97–10.33 °E, 53.39–54.06 °N) — far larger than any single pykci CityGML tile, which
makes spatial clipping (§9) mandatory.

---

## 3. Design principles

1. **Additive, non-destructive.** OSM never overwrites a pykci property. OSM-derived
   scalars live under an `osm_` prefix (`osm_height`, `osm_building`, `osm_name`, …) or
   on a dedicated provenance node — never on the authoritative key (`measured_height`,
   `function_code`, …).
   - **Critical invariant — `name` → `osm_name`.** In pykci a `Building`'s `name` property
     *is its gml:id* (`name | gml:id`). The OSM feature's `name` tag must therefore be
     copied onto the building as **`osm_name`**, never `name`. `build_enrich_writes()`
     remaps `name → osm_name` during the scalar copy; `reset_osm()` restores `b.name =
     b.id` defensively. (This was a real bug: a verbatim copy was clobbering the gml:id.)
   - **Geometry-comparability over fidelity for matching.** Footprint matching uses 2-D
     X/Y only; OSM z is dropped (OSM is largely LoD1, no useful heights in geometry).
2. **Provenance is structural.** Every OSM object becomes one `(:OsmFeature)` node
   (label = the hint that data came from OSM) carrying `source='osm:<file>'`, its
   `osm_id`, `osm_type`, version/timestamp, geometry and *all* tags. Enrichment links
   are explicit, weighted edges. You can always answer *"which part of this building is
   OSM-sourced, and how confident is the match?"*
3. **Compact.** One node per OSM object — **not** one node per tag. Tags become
   properties (promoted keys) plus an `other_tags` JSON for the long tail, exactly
   mirroring how pykci stores CityGML `gen:*Attribute` values. Geometry is stored once,
   reusing the existing `GeometryPolygon`/`GeometryRing` chain.
4. **Geometry-comparable.** OSM coordinates are reprojected WGS84 → dataset CRS
   (EPSG:25832 for Hamburg) at ingest, so OSM and pykci geometries share one coordinate
   system and one R-tree.
5. **Lossless tags.** No OSM tag is dropped; unmodelled keys survive verbatim in
   `other_tags`, the same proven pattern as CityGML's `ade_xml`.

---

## 4. Semantic mapping — OSM ↔ pykci / CityGML modules

pykci's schema already covers all CityGML 2.0 thematic modules. OSM features are routed
to the **matching pykci feature type**; their tags enrich the matched pykci node, and
OSM objects with no pykci counterpart become standalone (flagged) `OsmFeature` nodes.

| OSM selector | OGR layer | pykci / CityGML target | Match mode |
| ------------ | --------- | ---------------------- | ---------- |
| `building=*`, `building:part=*` | multipolygons | **`Building`** | footprint overlap (§6) |
| `highway=*` (lines), `railway=*` | lines | `Road` / `Railway` / `Track` / `Square` (`CityObject`) | line-vs-area overlap (§8) |
| `natural=water`, `water=*`, `waterway=riverbank` | multipolygons | `WaterBody` (`CityObject`) | footprint overlap |
| `landuse=*`, `natural=scrub/heath/…` | multipolygons | `LandUse` (`CityObject`) | footprint overlap |
| `natural=tree` | points | `SolitaryVegetationObject` | nearest / point-in-bbox |
| `natural=wood`, `landuse=forest`, `leisure=park` | multipolygons | `PlantCover` (`CityObject`) | footprint overlap |
| `man_made=bridge`, `bridge=yes` | multipolygons/lines | `Bridge` (`CityObject`) | footprint overlap |
| `tunnel=yes` | lines | `Tunnel` (`CityObject`) | line overlap |
| `man_made=*`, `barrier=*`, street furniture | points/multipolygons | `CityFurniture` (`CityObject`) | nearest / overlap |
| `amenity`, `shop`, `office`, `tourism`, `leisure` (POIs) | points | **enrich the containing `Building`** | point-in-footprint (§8) |
| `addr:*` | points/multipolygons | enrich `Building` (address) | point-in-footprint / overlap |
| anything else, or **outside pykci extent** | any | standalone `OsmFeature` (`unmatched=true`) | none (§9) |

**Direct attribute correspondences (the high-value enrichment).** Several OSM tags map
straight onto pykci building attributes that are often *missing or modelled-only* in
CityGML, and are the most useful additions:

| OSM tag | pykci `Building` attribute (authoritative) | stored as |
| ------- | ------------------------------------------ | --------- |
| `height` | `measured_height` | `osm_height` (+ compared) |
| `building:levels` | `storeys_above_ground` | `osm_building_levels` |
| `building:levels:underground` | `storeys_below_ground` | `osm_building_levels_underground` |
| `roof:shape` | `roof_type_code` (ALKIS `AX_Dachform`) | `osm_roof_shape` |
| `roof:material` | `predicted_roof_material` (gen attr) | `osm_roof_material` |
| `building=*` | `function_code` (ALKIS `AX_Gebaeude`) | `osm_building` |
| `start_date` | `year_of_construction` | `osm_start_date` |
| `name`, `addr:*`, `operator`, `amenity` | — (no CityGML equivalent) | `osm_*` |

Storing OSM and authoritative values side-by-side under distinct keys makes them
**directly queryable for agreement/disagreement** (e.g. "buildings where `osm_height`
and `measured_height` differ by > 3 m") — a ready-made cross-source quality-analysis task.

---

## 5. Geometry & CRS handling

- **Reproject.** WGS84 (EPSG:4326) → dataset CRS read from `Dataset.crs` (EPSG:25832 for
  Hamburg) via `pyproj.Transformer(always_xy=True)`. All stored OSM coordinates are in
  the dataset CRS, identical to pykci, so geometries co-register and share the R-tree.
- **Footprint storage.** Each `OsmFeature` stores `bbox_min/max_x/y`, `center_x/y`,
  `bbox_wkt` (2-D polygon for the R-tree) and `location` (Neo4j `point`) — exactly the
  property set pykci uses on `Building`/`CityObject`. The full ring is stored once via the
  existing `(:OsmFeature)-[:HAS_FOOTPRINT]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->(:GeometryRing)`
  chain (interior rings → `HAS_INTERIOR_RING`, reusing the CityGML geometry model — OSM
  multipolygon holes map cleanly to CityGML interior rings).
- **R-tree.** OSM features register in a **separate** WKT layer `osm_features` (not the
  CityGML `features` layer), so existing CityGML spatial queries are unaffected and
  cross-source matching is an explicit two-layer operation. Both layers read `bbox_wkt`.
- **pykci footprint source.** For matching, the pykci building footprint is the
  `GroundSurface` exterior ring (`(:Building)-[:HAS_BOUNDARY]->(:BoundarySurface
  {surface_type:'GroundSurface'})->(:GeometryPolygon)->(:GeometryRing)`), falling back to
  `lod0FootPrint`, falling back to the bbox polygon. Rings are 3-D `posList`s; we use the
  X/Y components.

### Topology

- **Multipolygon holes** → interior rings (`HAS_INTERIOR_RING`), same as CityGML.
- **Relation membership** → `(:OsmFeature)-[:HAS_OSM_MEMBER {role, order}]->(:OsmFeature)`
  for relations we keep (multipolygons, optionally routes/boundaries).
- **Shared-node adjacency / route order** is *not* materialised by default (expensive,
  rarely queried); it can be derived on demand from geometry via `spatial.intersects`.
  This is a deliberate compactness choice.

---

## 6. The footprint-matching problem (core)

Over the same area, an OSM building polygon and a pykci/CityGML building footprint may
relate as **1:1, 1:n, n:1, or n:m** because the two sources digitise the same structures
at different granularity (the paper itself notes a surface "can be represented either as
a single polygon or as a collection of smaller adjacent polygons that together form the
same geometry", §2.2). We solve all four cases with **one** mechanism.

### 6.1 Build a weighted overlap graph

1. **Candidate generation (cheap).** For each OSM building, query the CityGML R-tree
   `CALL spatial.intersects('features', osm.bbox_wkt)` to get pykci buildings whose
   bounding boxes intersect. (Bounding boxes only — fast.)
2. **Exact overlap (precise).** With **shapely**, compute the true polygon intersection
   area between the OSM footprint and each candidate pykci footprint.
3. **Significance filter.** Keep an edge only if the overlap is meaningful — drop slivers
   from digitising offsets. We use the **max overlap ratio**

   `r = area(A ∩ B) / min(area(A), area(B))   ≥  τ  (default τ = 0.3)`

   (The 0.5 OSM-conflation convention is the precision-max setting; an empirical
   buffer/threshold sweep on HafenCity — see below — showed τ = 0.3 enriches a few more
   pykci buildings with *identical* primary-match Jaccard, so 0.3 is the tuned default.)

   and also record the **Jaccard / IoU** `J = area(A ∩ B) / area(A ∪ B)` on the edge.
   `r` (rather than IoU) is the *acceptance* test because in 1:n/n:1 cases a correct
   small-vs-large pair has low IoU but high `r` — IoU alone would wrongly reject true
   subset matches. (The 0.5 significance cut-off follows the common OSM-conflation
   convention; see References.)

The surviving edges form a **bipartite graph** `OSM ↔ pykci`, each edge weighted by
`(overlap_ratio, jaccard, intersection_area)`.

**Geometry-aware ratio (lines).** OSM roads are *centre-lines*, not areas, so `area` is 0
for them. `match_areas()` detects a line (`geom.area == 0 and geom.length > 0`) and uses a
**length** ratio instead: `r = length(line ∩ pykci_polygon) / length(line)` (and `J = r`).
Without this, every road edge would score 0 and silently never match — this was a real
bug caught in testing.

> **Implementation note — how the R-tree is actually used.** The conceptual description
> above is per-OSM-feature, but `ingest_osm.py` does it in two stages for efficiency at
> tile scale:
> 1. **Coarse scoping (Neo4j R-tree, once per target type).** `fetch_pykci_footprints()`
>    issues a *single* `CALL spatial.intersects('features', $wkt)` where `$wkt` is the
>    dataset bbox (+ buffer), pulling **all** pykci footprints in the tile in one query and
>    assembling their `GroundSurface` rings into shapely polygons.
> 2. **Exact matching (in-memory shapely).** `match_areas()` then tests each OSM feature
>    against that fetched set using a `shapely.prepared` `intersects()` pre-filter followed
>    by exact `intersection()`. This is O(|OSM| × |pykci|) over the *tile's* footprints —
>    fine at tile scale (≈145 × 81), but for a whole-city graph switch to the per-feature
>    R-tree form (`spatial.intersects('features', osm.bbox_wkt)` once per OSM feature) so
>    you never load every footprint into memory at once.
>
> **Use `spatial.intersects`, not `spatial.bbox`.** The neo4j-spatial `spatial.bbox(layer,
> min, max)` procedure throws `NullPointerException: ... "p1" is null` on WKT layers in
> this plugin build. `spatial.intersects(layer, wkt)` (passing a bbox WKT polygon) is the
> reliable equivalent and is what both `fetch_pykci_footprints()` and GRAPH.md's overlap
> example use.

### 6.2 Classify by connected component

Take the **connected components** of that bipartite graph. Within a component, count OSM
nodes (`|O|`) and pykci nodes (`|P|`):

| `|O|` : `|P|` | Case | Meaning |
| ------------- | ---- | ------- |
| 1 : 1 | **1:1** | clean correspondence |
| 1 : n | **1:n** | one OSM polygon covers several adjacent pykci buildings (OSM coarser) |
| n : 1 | **n:1** | several adjacent OSM polygons make up one pykci building (OSM finer) |
| n : m | **n:m** | general many-to-many block |

This component formulation is exactly *"apply 1:n and n:1 on top of each other"* — n:m is
not a separate algorithm, it is just a component with > 1 node on both sides, handled by
the same attribution rule below. **This is the answer to "do we just apply 1:n and n:1 on
top of each other?": yes — the connected component subsumes all four.**

### 6.3 Why we keep the graph instead of solving optimal m:n

There *is* a rigorous formulation — **"Many-to-Many Polygon Matching à la Jaccard"**
(Naumann et al., ESA 2024) — which finds the partition maximising total Jaccard. It is
**NP-complete** and overkill here. Because pykci is a *knowledge graph*, we are not forced
to commit to a single hard partition the way a one-shot conflation pipeline is: we **keep
the full weighted overlap graph as first-class `ENRICHED_BY` edges** and let queries
resolve cardinality. This is simpler, lossless (no match is discarded), and strictly more
faithful — and it sidesteps the NP-complete optimisation entirely. The optimal method is
noted as the rigorous alternative if a single canonical 1:1 assignment is ever required.

### 6.4 Attribution rules — *where does each OSM attribute go?*

The pykci building is always the anchor. We split OSM data into two classes:

- **Shared-thematic** (plausibly true of the whole structure): `building`, `name`,
  `addr:*` (street/postcode/city), `height`, `building:levels`, `roof:shape`,
  `roof:material`, `operator`, `amenity` (when building-wide). Safe to replicate.
- **Singular** (belongs to exactly one object/place): the OSM geometry, `osm_id`,
  version/timestamp, a POI that physically sits at one spot, a house-number that belongs
  to one entrance. **Must not** be blindly duplicated.

For **every** component we always create one `(:OsmFeature)` per OSM object (holding all
singular data + full tags) and link it to the pykci building(s) it overlaps via
`(:Building)-[:ENRICHED_BY {overlap_ratio, jaccard, intersection_area, match_type,
is_primary}]->(:OsmFeature)`. Singular data therefore lives **once** on the `OsmFeature`
and is reached by traversal — never duplicated. What differs per case is only how
shared-thematic scalars are *copied onto* the building node(s) for convenient querying:

| Case | Shared-thematic scalars | `is_primary` | Notes |
| ---- | ----------------------- | ------------ | ----- |
| **1:1** | copied onto the one pykci building (`osm_*`) | the single edge | trivial |
| **n:1** (many OSM → 1 pykci) | the pykci building gets the values of the **dominant** OSM contributor — the one sharing the **largest intersection area** (`osm_*`), plus `osm_match_count` | edge from the largest-shared-area OSM | all OSM parts remain as separate `OsmFeature` nodes linked to the one building, so nothing is lost; conflicting values (different names/heights) survive on their own nodes. (Dominance is by shared *area*, not overlap ratio — the latter saturates to ~1.0 for a small fully-contained fragment and would mis-rank tiny pieces.) |
| **1:n** (1 OSM → many pykci) | shared-thematic scalars **replicated onto all** covered pykci buildings (`osm_*`, with `osm_shared=true`) | edge to the **highest-Jaccard** pykci building | replication is intentional — adjacent buildings genuinely share block-level name/use; singular data (geometry, `osm_id`, POIs) stays only on the single `OsmFeature` and is **not** replicated |
| **n:m** | apply n:1 then 1:n within the component: each pykci building takes the largest-overlap OSM's shared scalars; each OSM links to all pykci it overlaps | per-pair largest overlap | same edge model, no special case |

**"To which pykci building should a 1:n OSM object's data attach?"** — the answer:
- *Shared-thematic* values → **all** covered pykci buildings (redundant on purpose, more
  exact representation, flagged `osm_shared=true` so consumers know it is block-level).
- *Singular / must-not-duplicate* values → kept **once** on the `OsmFeature`, and the
  single **highest-Jaccard** pykci building is marked `is_primary=true` as the canonical
  holder (Jaccard, not bare overlap, because among several covered buildings the one whose
  *whole* footprint best fills the OSM polygon is the truest primary). Other buildings keep
  the (non-primary) `ENRICHED_BY` edge so the relationship is never lost.

This gives the best of both options the user raised: redundancy *where it improves
representation*, single-storage *where redundancy would be wrong*, and a confidence weight
(`jaccard`, `overlap_ratio`) on every edge so any downstream query can re-decide.

---

## 7. Schema additions

```text
── OSM provenance ────────────────────────────────────────────────────────────────
(OsmFeature)        -[:PART_OF]->                  (Dataset)          # the OSM dataset node
(Building)          -[:ENRICHED_BY {overlap_ratio, jaccard,
                       intersection_area, match_type, is_primary}]-> (OsmFeature)
(CityObject)        -[:ENRICHED_BY {…}]->          (OsmFeature)       # water / landuse / vegetation / bridge …
(Building)          -[:HAS_POI {distance_m}]->     (OsmFeature)       # point POI inside the footprint
(OsmFeature)        -[:HAS_FOOTPRINT]->            (GeometryPolygon)  # reuse CityGML geometry chain
(OsmFeature)        -[:HAS_OSM_MEMBER {role, order}]-> (OsmFeature)   # relation members
(OsmFeature)        -[:LOCATED_IN]->               (District)
```

`OsmFeature` also carries the shared `Entity` label (so generic `Entity(id)` edge-lookups
work) and, when APOC is present, a dynamic semantic label for its mapped type
(`OsmBuilding`, `OsmWater`, `OsmLandUse`, `OsmTree`, `OsmPOI`, …) so `MATCH (n:OsmBuilding)`
works alongside `MATCH (n:OsmFeature {osm_kind:'building'})`.

**`osm_kind` is the canonical, always-present type key** (use it for filtering — it works
with or without APOC). The dynamic label is a *transient* `apoc.create.addLabels`
parameter (`$labels` side-map in `write_osm_nodes`), **never stored as a `label`
property** — persisting it would duplicate the real Neo4j label, and `osm_kind` already
carries the type. (Mirrors CityGML: `CityObject` nodes store `feature_type`, not `label`.)

**`gtype` and `bbox` are not ours.** Any node added to a neo4j-spatial R-tree layer gets a
`gtype` (plugin geometry-type code, e.g. `3` = polygon) and `bbox` (`[minx,miny,maxx,maxy]`
array) written *by the plugin*. They appear on every indexed node — CityGML and OSM alike —
and `ingest_osm.py` never sets them. Presence of `gtype` is a handy "is this node indexed?"
marker.

### `OsmFeature` node properties

| Property | Source |
| -------- | ------ |
| `id` | `osm:<type>/<osm_id>` (stable, e.g. `osm:way/12345`) |
| `osm_id`, `osm_type` | OSM element id and `node`/`way`/`relation` |
| `osm_kind` | mapped class: `building`/`water`/`landuse`/`vegetation`/`road`/`poi`/`furniture`/`other` |
| `osm_version`, `osm_timestamp` | element metadata (provenance) |
| `name`, `osm_building`, `osm_amenity`, `osm_landuse`, … | promoted tag columns (kept under stable keys) |
| `osm_height`, `osm_building_levels`, `osm_roof_shape`, `osm_roof_material`, `osm_start_date` | the direct-correspondence tags (§4) |
| `addr_street`, `addr_housenumber`, `addr_postcode`, `addr_city` | parsed `addr:*` |
| `other_tags` | JSON object of **all** remaining tags (lossless long tail) |
| `bbox_min/max_x/y`, `center_x/y`, `bbox_wkt`, `location`, `ground_z` | geometry in dataset CRS |
| `matched` | `true` if linked to ≥1 pykci feature, else `false` (orphan) |
| `unmatched_reason` | orphans only: `no_overlap` / `no_pykci_type` / `no_containing_building`; cleared (null) on matched features |
| `source` | `osm:<filename>` |

On the enriched pykci `Building`/`CityObject` we additionally set, under the `osm_`
namespace, the curated shared-thematic scalars plus:

| Property | Meaning |
| -------- | ------- |
| `osm_match_type` | `1:1` / `1:n` / `n:1` / `n:m` |
| `osm_match_count` | number of `ENRICHED_BY` edges |
| `osm_shared` | `true` when scalars were replicated from a 1:n OSM polygon |
| `osm_best_jaccard` | Jaccard of the primary edge (match-quality signal) |

---

## 8. Non-building OSM features

The same machinery generalises:

- **Area features** (`landuse`, `natural=water`/`wood`, `leisure=park`, large
  `man_made`): overlap-graph matched (§6) against the corresponding `CityObject`
  (`WaterBody`, `LandUse`, `PlantCover`, …). Same `ENRICHED_BY` edges and cardinality
  rules.
- **Point POIs** (`amenity`, `shop`, `office`, `tourism`, points with `addr:*`): there is
  no CityGML geometric equivalent, so they enrich the **containing building** via
  point-in-footprint (R-tree `spatial.intersects` on the point, then exact
  `polygon.contains(point)` in shapely) → `(:Building)-[:HAS_POI]->(:OsmFeature)`. A
  building can host many POIs; each stays its own node (no scalar flattening), which is
  the natural, compact representation of "this building contains a café, a shop and an
  office". POIs that fall in no footprint become orphan `OsmFeature` nodes.
- **Point trees / furniture** (`natural=tree`, `man_made=*` points): nearest-feature or
  standalone `OsmFeature` (`OsmTree`/`OsmFurniture`), spatially indexed; linked to a
  `SolitaryVegetationObject`/`CityFurniture` only on a confident proximity match,
  otherwise kept as a flagged standalone node (pykci LoD2 city tiles rarely model every
  tree, so most OSM trees will be net-new, valuable nodes).
- **Line features** (`highway`, `railway`, `waterway`): OSM stores these as **centre-lines
  (1-D)** while CityGML Transportation is **area (2-D)** — a genuine topology mismatch.
  We store OSM lines as `OsmFeature` nodes with a `GeometryLineString` geometry and link
  them to overlapping CityGML `Road`/`Railway` areas by line-in-area overlap (length of
  line inside the area / total length ≥ τ) via `ENRICHED_BY`. Where pykci has no road
  area (common — LoD2 tiles often omit transportation), the OSM street network is kept as
  standalone, queryable line nodes — a substantial net addition.

---

## 9. Spatial-extent mismatch & orphans

The PBF covers all of Hamburg state; a pykci tile is a few hundred metres. **Clip first:**
before matching, restrict OSM reading to `Dataset` bbox **+ buffer** (default 10 m, so
features straddling the edge are still considered) using GDAL's `-spat` (in WGS84). This
is both a correctness and a graph-size measure. The sweep showed the buffer never adds a
*match* (every matchable OSM building lies inside the bbox); a larger buffer only pads the
graph with margin orphans and depresses the apparent match rate — hence the small 10 m
default.

Within the clipped area there are three orphan situations, all handled uniformly:

1. **OSM feature with no pykci overlap** (`no_overlap`) — e.g. a building OSM has but the
   CityGML tile predates, or one in the buffer margin beyond pykci's coverage. Kept as a
   standalone, spatially-indexed `OsmFeature` (`matched=false`) — net-new knowledge.
2. **OSM type pykci does not model in this tile** (`no_pykci_type`) — e.g. roads,
   furniture, landuse, vegetation when the LoD2 tile carries no such `CityObject`. Kept as
   standalone `OsmFeature` with its mapped label.
3. **POI inside no building footprint** (`no_containing_building`) — a point POI that
   falls outside every building; kept as a standalone, queryable node.
4. **pykci feature with no OSM match** — simply has no `ENRICHED_BY` edge. No action
   needed; queries find it by the *absence* of the edge (`WHERE NOT (b)-[:ENRICHED_BY]->()`).

(`matched=true` features carry no `unmatched_reason`; orphans carry exactly one of the
three reasons above. On a HafenCity run (buffer 10 m, τ 0.3): 64 of 81 pykci buildings are
enriched (79 %), 112 POIs attach, and ~2.8 k net-new road/furniture/landuse/vegetation
nodes are added.)

Orphan OSM features outside the **buffered** dataset bbox are not ingested at all (they
belong to a different tile / the wider region), keeping the graph scoped to pykci's extent.

---

## 10. Compactness accounting

| Choice | Effect |
| ------ | ------ |
| One `OsmFeature` per OSM object, tags as properties + `other_tags` JSON | no node-per-tag explosion (mirrors CityGML `gen:*Attribute` handling) |
| Geometry stored once via existing `GeometryPolygon`/`GeometryRing` | no parallel geometry model; reuses pykci's |
| Singular data lives only on `OsmFeature`, reached by traversal | zero duplication of geometry / `osm_id` / POIs |
| Shared-thematic scalars replicated **only** in the 1:n case, flagged `osm_shared` | bounded, intentional redundancy for representation accuracy |
| Topology adjacency not materialised (derived on demand) | avoids O(edges²) `TOUCHES` blow-up |
| Clip to dataset bbox + buffer | graph stays scoped to the pykci tile |

---

## 11. Implementation — `ingest_osm.py`

Mirrors `ingest_citygml.py` conventions: same Neo4j driver/auth, `UNWIND $rows … MERGE
(n {id}) SET n += row, n:Entity` batched writes (`BATCH_SIZE = 1000`), `IF NOT EXISTS`
indexes, APOC dynamic labels with a no-APOC fallback, separate WKT R-tree layer. Fully
**idempotent**: each run first calls `reset_osm()` to drop the `osm_features` layer, strip
prior `osm_*` enrichment props (restoring `Building.name = gml:id`), and delete previous
`OsmFeature` nodes + their OSM-only geometry — so a re-run with changed
`--buffer`/`--overlap-threshold` cleanly reflects the new parameters with no stale margin
nodes. CityGML nodes/geometry are never touched.

```bash
python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf            # auto-detect single Dataset + its bbox/CRS
python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf --dataset hamburg_hafencity
python3 ingest_osm.py input/osm/hamburg-260629.osm.pbf --buffer 10 --overlap-threshold 0.3
```

**Pipeline phases:**

1. **Resolve target.** Read the `Dataset` node (CRS + bbox) from Neo4j to learn the clip
   window and projection target. Build `pyproj` WGS84 → dataset-CRS transformer.
2. **Read & clip OSM.** Open the PBF with OGR; for each layer apply the bbox+buffer
   spatial filter; reproject every geometry to the dataset CRS; classify each feature to
   an `osm_kind` (§4); parse promoted columns + `other_tags`.
3. **Write `OsmFeature` nodes** (batched) with geometry chain, register them in the
   `osm_features` R-tree layer.
4. **Match & attribute.** For each OSM area/line/point, R-tree candidate lookup on the
   CityGML `features` layer → exact shapely overlap → significance filter → accumulate the
   overlap graph; compute connected components; classify cardinality; write `ENRICHED_BY`
   / `HAS_POI` edges and the curated `osm_*` scalars per the §6.4 rules.
5. **Flag orphans** (`matched=false`, `unmatched_reason`).
6. **Summary** → `output/<osm-file>_summary.md` (counts per kind, per cardinality case,
   match rate, orphan counts).

**Dependencies:** `pyproj`, `shapely`, `neo4j` (already in `requirements.txt`); GDAL/OGR
(the `osgeo.ogr` Python binding, or `ogr2ogr` to a temporary GeoPKG/GeoJSON if the binding
is absent). All present on this machine (GDAL 3.11, shapely 2.1, pyproj 3.7).

---

## 12. Tag handling — promoted properties vs `other_tags`

OSM's keyspace is **unbounded and free-form** (any mapper invents any `key=value`; tens of
thousands of distinct keys are in active use). Exploding every key into a node property
would give an unbounded, sparse, undocumentable schema — the opposite of pykci's compact,
context-sized design. So tags are split:

- **Promoted** (`PROMOTED_TAGS` list) → first-class properties under stable keys
  (`name`, `osm_building`, `osm_height`, `osm_roof_shape`, …; `addr:*` → `addr_*`). These
  are the documented, queryable, schema-surface keys.
- **Everything else** → one `other_tags` **JSON object** (the lossless long tail), exactly
  mirroring CityGML `gen:*Attribute` handling. Nothing is dropped.

The tags *are* parsed: GDAL packs non-promoted tags into an HSTORE string
(`"k"=>"v","k2"=>"v2"`); `_parse_other_tags()` regex-parses it to a dict, `_all_tags()`
merges the promoted columns + parsed HSTORE, and the leftover becomes the `other_tags`
JSON. So grouping them is a **design choice for compactness/losslessness, not an
inability**. Query the tail with APOC:

```cypher
MATCH (o:OsmFeature {osm_kind:'building'})
WHERE apoc.convert.fromJsonMap(o.other_tags).`building:material` = 'porcelain'
RETURN o.osm_id, apoc.convert.fromJsonMap(o.other_tags).website AS site
```

To make a tail key first-class, add it to `PROMOTED_TAGS` (it lands as `osm_<slugged_key>`,
e.g. `building:material` → `osm_building_material`) and re-run. Good candidates seen in
Hamburg: `building:material`, `building:colour`, `official_name`, `website`. Long-tail like
`image`/`loc_name` is better left in `other_tags`.

---

## 13. Implementation reference (code map)

`ingest_osm.py` pipeline (`main()`), phase → function:

1. **Resolve target** — `get_dataset(sess, --dataset)` reads the `Dataset` node's `crs`
   (parsed to EPSG via `_epsg_from_srs`) and bbox corners; one dataset required (or name it).
2. **City bbox** — inverse `pyproj` transform of `bbox ± buffer` → WGS84 `-spat` filter.
3. **Idempotent clean** — `ensure_indexes()` then `reset_osm()` (drop `osm_features` layer,
   strip `osm_*` props restoring `Building.name = id`, delete prior `OsmFeature`s + OSM-only
   geometry via `id STARTS WITH 'osm:'`). CityGML data untouched.
4. **Read & classify** — `extract_layer()` shells out to **`ogr2ogr`** (no GDAL Python
   binding needed) per layer (`multipolygons`/`points`/`lines`), clipping (`-spat`) and
   reprojecting (`-t_srs`) into GeoJSON; `points`/`multipolygons` are read **unfiltered**
   (every in-bbox feature is ingested; untagged-kind ones land in kind `other`), `lines`
   is restricted to the transport/water network (`LAYER_WHERE`); parsed with
   `json` + `shapely.shape`. Each feature: `classify_kind()` → `build_osm_props()` →
   `geom_bbox_fields()`; geometry via `ring_rows()` / `line_rows()`; areas/lines bucketed by
   `(target_label, target_ft)` for matching; points of kind `poi` collected for containment.
5. **Write nodes** — `write_osm_nodes()`: batched `UNWIND…MERGE…SET n += row, n:Entity`,
   APOC dynamic label from the `$labels` side-map (no-APOC fallback), geometry chains
   (`HAS_FOOTPRINT`/`HAS_EXTERIOR_RING`/`HAS_INTERIOR_RING`/`HAS_LOD_SURFACE`), `PART_OF`
   dataset, `LOCATED_IN` district.
6. **Spatial index** — `ensure_spatial_layer('osm_features')` + `index_osm_spatial()`
   (`spatial.addNodes`). Separate layer from CityGML `features` (see §5).
7. **Match** (per target type) — `fetch_pykci_footprints()` (R-tree bulk fetch, §6.1 note)
   → `match_areas()` (weighted overlap graph + connected components) →
   `build_enrich_writes()` (§6.4 attribution: dominant = largest `inter_area`; 1:n primary =
   highest Jaccard; `name`→`osm_name`) → `write_enrichment()` (`ENRICHED_BY` + `osm_*`
   scalars).
8. **POIs** — `fetch_pykci_footprints(Building)` → shapely point-in-polygon → `write_pois()`
   (`HAS_POI {distance_m}`).
9. **Orphans & summary** — flag `matched=false` + `unmatched_reason` per `orphan_reason()`;
   write `output/<file>_osm_summary.md`.

**Key constants:** `KIND_LABEL` (kind → dynamic-label, pykci-target-label, target
feature_type), `PROMOTED_TAGS`, `DIRECT_CORRESPONDENCE` (OSM tag → authoritative pykci attr,
reference only), `SHARED_THEMATIC` (keys replicable in 1:n), `ENRICH_KEYS` (osm_* keys
`reset_osm` strips), `BATCH_SIZE=1000`, `NEO4J_URI`/`NEO4J_AUTH` (`bolt://localhost:7687`,
`neo4j/neo4jneo4j`).

**Stable id scheme** (relied on by `reset_osm`'s `STARTS WITH 'osm:'`):
- feature node: `osm:<type>/<osm_id>` (e.g. `osm:way/12345`)
- polygon: `<feature_id>_poly{i}`; exterior ring `…_ext`; interior ring `…_int{i}`
- line: `<feature_id>_line{i}`

**Dependencies:** `ogr2ogr` (GDAL CLI ≥ 3.x on PATH), `shapely`, `pyproj`, `neo4j`. The OGR
**Python binding is *not* required** — extraction goes through the CLI.

---

## 14. Empirical tuning record (buffer / threshold sweep)

### Pilot-tile result (HafenCity tile, 81 buildings)

*This is the original tuning record on one pilot tile. Its τ grid step was not recorded and
its sweep script was not kept; the city-scale sweep below repeats it on all of Hamburg with a
documented grid and a kept script.*

A standalone read-only sweep (reusing `match_areas`/`build_enrich_writes`; OSM extracted once
at max buffer, pykci footprints fetched once, combos evaluated in memory) characterised the
trade-off. With no ground truth, **mean primary-edge Jaccard** and the **J≥0.5 / J<0.2 edge
fractions** are the precision proxies. Findings (HafenCity, 220 OSM buildings, 81 pykci
footprints):

- **Buffer never adds a match.** `matchB` is constant (76) across buffers 0→50 m; only the
  in-bbox count grows, diluting the rate (0.60→0.47). Every matchable OSM building is
  already inside the bbox; buffer only pads the graph with margin orphans. → **default 10 m**.
- **Threshold trades case-purity, not primary quality.** Mean primary Jaccard is flat at
  **0.775** across *all* settings. Lowering τ 0.5→0.1 enriches 62→66 buildings but adds
  low-Jaccard *secondary* edges (J<0.2 share 33%→37%) and reclassifies `1:1`→`n:m` as slivers
  bridge components (`n:m` 1→3); τ=0.7 loses 2 matches. **τ = 0.3** enriches 64 vs 62 with
  identical primary quality and no sliver inflation → **default 0.3**.
- **Structural ceiling, not a threshold artdefact.** 76 of 126 in-bbox OSM buildings match
  even at τ=0.1 — the other 50 have *no* overlapping pykci footprint (OSM maps more/different
  buildings than the CityGML tile). Better framing: **enriched pykci buildings / total =
  64/81 (79%)** at the tuned defaults.
- **Low-Jaccard edges are not errors.** The ~33% J<0.2 edges at τ=0.5 are the legitimate
  small-fragment edges intrinsic to n:1/1:n (a small OSM piece fully inside a big building →
  high overlap-ratio, low Jaccard) — which is exactly why *primary/dominant* selection uses
  **shared intersection area**, not Jaccard or overlap-ratio (overlap-ratio saturates to ~1
  for a contained fragment).

**Tuned live run** (`--buffer 10 --overlap-threshold 0.3`): 145 OSM buildings; match cases
`1:1=28, n:1=9, 1:n=8, n:m=3`; **64/81 pykci buildings enriched**; 112 POIs attached; ~2.8 k
net-new nodes; orphans by reason `no_pykci_type 2833 / no_overlap 69 /
no_containing_building 511`.

### City-scale sweep (whole Hamburg), 2026-10-05

**Script:** `eval/osm_tau_sweep.py` (read-only; imports `ingest_osm` the way
`eval/osm_ingestion_census.py` does). **Inputs:** the released Hamburg graph
(`backups/neo4j_hamburg_lod2_roofmats_lod3_osm_20260717.dump`: 388,267 buildings,
EPSG:25832), `input/osm/hamburg-260629.osm.pbf`, buffer 10 m, GDAL 3.13.1 (QGIS 3.44.12),
GEOS 3.13.1. **Method:** OSM side re-derived with the ingest's own code path (373,251 OSM
features under the `("Building", None)` key, i.e. building polygons plus POI polygons);
`match_areas(τ=0)` once (503,298 positive-overlap pairs); per τ the edges with
`overlap_ratio >= τ` are kept, components recomputed and `build_enrich_writes` called.
**Grid:** τ = 0.01, then 0.05 to 0.95 in steps of 0.05 (20 values). Full results:
`output/stats/osm_tau_sweep_hamburg.{md,csv,json}`.

**Validation gate (passed).** At τ = 0.30 the sweep reproduces the graph exactly: 373,211
`ENRICHED_BY` edges, 331,318 enriched buildings, 334,488 matched OSM features, 302,209
primary edges, match types 1:1 256,219 / 1:n 48,276 / n:1 48,489 / n:m 20,227. Edge by edge,
all 373,211 pairs coincide with bit-identical `overlap_ratio`, `jaccard` and
`intersection_area`. The only differences are a few dozen `is_primary` flags (62 to 82 per
run), all inside 1:n components where two or more buildings tie exactly on Jaccard;
`build_enrich_writes` breaks that tie with `max()` over a Python set, whose order depends on
the per-process hash seed. Primary-edge count and primary-Jaccard values are unaffected.
Graph node and relationship totals were identical before and after the run.

| τ | enriched buildings | % of 388,267 | mean primary J | median | p10 | J<0.2 edges | n:m comps |
| --: | --: | --: | --: | --: | --: | --: | --: |
| 0.10 | 334,429 | 86.13 | 0.792 | 0.885 | 0.470 | 14.0% | 6,380 |
| 0.20 | 332,407 | 85.61 | 0.794 | 0.885 | 0.475 | 12.4% | 3,254 |
| **0.30** | **331,318** | **85.33** | **0.795** | **0.886** | **0.478** | **11.8%** | **2,363** |
| 0.40 | 330,411 | 85.10 | 0.796 | 0.886 | 0.480 | 11.4% | 1,904 |
| 0.50 | 329,332 | 84.82 | 0.797 | 0.886 | 0.483 | 11.2% | 1,497 |
| 0.60 | 327,444 | 84.33 | 0.799 | 0.887 | 0.488 | 11.1% | 1,329 |
| 0.70 | 324,003 | 83.45 | 0.802 | 0.889 | 0.495 | 11.0% | 1,207 |

Findings:

- **Precision proxy is nearly flat, not constant.** Mean primary Jaccard rises slowly from
  0.792 (τ = 0.10) to 0.802 (τ = 0.70), a range of 0.011; median 0.885 to 0.889. The
  city-scale level is about 0.795, not the pilot tile's 0.775.
- **Recall proxy falls slowly.** Enrichment drops from 334,429 (τ = 0.10) to 324,003
  (τ = 0.70) buildings; τ = 0.30 keeps 99.07% of the τ = 0.10 enrichment.
- **Attribute sources do not move.** Among buildings enriched at both τ = 0.10 and 0.50,
  99.98% (329,266 of 329,332) copy their `osm_*` values from the same OSM feature (the one
  sharing the largest intersection area). τ mostly prunes secondary sliver edges; the
  dominant edge survives.
- **τ shapes topology at the low end.** n:m components fall from 32,550 (τ = 0.01) and 6,380
  (0.10) to 2,363 (0.30) and 1,497 (0.50) as sliver edges stop bridging neighbouring
  footprints; the J<0.2 edge share falls from 25.3% (0.01) to 11.8% (0.30).
- **0.30 sits at the low end of a flat optimum.** Per 0.05 step, the enrichment loss is
  smallest between 0.25 and 0.45 (444 to 489 buildings per step), while below 0.25 the n:m
  count grows quickly (575 to 2,139 components per step) and above 0.50 the enrichment loss
  accelerates (883 to 2,067 buildings per step). Any τ in [0.25, 0.45] gives near-identical
  results; the default 0.3 is kept.
- **Structural ceiling.** 46,459 buildings (11.97%) overlap no OSM feature under the
  Building key at all, so no τ can enrich them; τ = 0.30 enriches 96.93% of the 341,808 that
  do.
- **The largest n:m component is not a sliver effect.** At every τ from 0.10 to 0.70 it has
  about 287 buildings and 239 OSM features, held together by one POI polygon
  (`osm:relation/10460644`, "Universität Hamburg", `amenity=university`, spanning several
  campus sites): the overlap coefficient's min-area denominator gives every contained
  building a ratio near 1.

### Cross-city check (all five cities), 2026-10-05

The same read-only sweep on each released city graph (each restored from its latest dump
into its own Docker volume; one Neo4j at a time). The validation gate passed in every city
(edges, enriched buildings, matched OSM, primary edges and match types reproduced exactly;
graph totals unchanged). The script now also writes threshold diagnostics: footprint source,
the overlap-ratio distribution of all candidate pairs, and, per 0.05 step, whether the
buildings that lose their match were matched to their own OSM outline or to a neighbour's
(the OSM feature overlaps another building more). `eval/osm_tau_sweep.py --summarize` builds
`output/stats/osm_tau_sweep_cities.md`.

| city | buildings | GroundSurface footprints | enriched % at 0.1 / 0.3 / 0.5 | mean primary J at 0.1 / 0.3 / 0.7 | pairs with ratio 0.25 to 0.45 | extra at 0.1 (from a neighbour) | own-majority from τ |
| --- | --: | --: | --: | --: | --: | --: | --: |
| Tokyo | 2,005,762 | 8.3% | 60.37 / 58.88 / 57.93 | 0.564 / 0.576 / 0.584 | 5.02% | 29,861 (97.1%) | 0.65 |
| New York | 1,083,437 | 100.0% | 98.81 / 98.78 / 98.72 | 0.980 / 0.981 / 0.982 | 0.08% | 385 (55.8%) | 0.30 |
| Hamburg | 388,267 | 100.0% | 86.13 / 85.33 / 84.82 | 0.792 / 0.795 / 0.802 | 0.71% | 3,111 (92.1%) | 0.50 |
| Zurich | 102,668 | 98.4% | 91.92 / 91.52 / 91.15 | 0.757 / 0.760 / 0.766 | 0.83% | 410 (84.4%) | 0.45 |
| Helsinki | 2,980 | 98.0% | 83.86 / 83.09 / 82.15 | 0.709 / 0.716 / 0.727 | 1.58% | 23 (60.9%) | (too few) |

Findings:

- **The precision proxy is nearly flat in every city** (range at most 0.020 over τ 0.1 to
  0.7). Its level differs by city (0.98 New York, where OSM imported the city's own
  footprints; 0.58 Tokyo) and reflects how well the two sources' outlines agree, not τ.
- **τ = 0.3 keeps at least 97.5% of the τ = 0.1 enrichment everywhere** (at least 99% outside
  Tokyo), and at least 99.8% of buildings keep their attribute source between 0.1 and 0.5.
- **Why not 0.1:** in the larger cities most of what 0.1 adds is a neighbour's outline (97%
  Tokyo, 92% Hamburg, 84% Zurich), and it pulls clean 1:1 matches into larger groups (249,219
  Tokyo, 11,128 Hamburg, 4,400 Zurich, 4,121 New York).
- **Why not higher:** the step from which raising τ mostly removes *own-outline* (valid)
  matches is 0.30 in New York, 0.45 in Zurich and 0.50 in Hamburg (Helsinki has at most 12
  buildings per step, too few to tell). Between 0.3 and 0.5 enrichment changes by at most
  0.6% of enriched buildings in these cities. The defensible range is therefore 0.3 to 0.5;
  0.3 is its inclusive end and is kept as the single default.
- **Tokyo is the exception, and the cause is footprints, not τ.** 91.7% of Tokyo buildings are
  LoD1 solids without a GroundSurface, so `fetch_pykci_footprints` matches their axis-aligned
  bbox rectangle. Rectangles of rotated buildings reach into neighbours, which lowers Jaccard,
  fattens the middle of the ratio distribution (5% of pairs between 0.25 and 0.45) and keeps
  neighbour matches dominant up to τ = 0.65. Matching LoD1 solids on their base outline would
  address this; that is a pipeline change, not made here.

---

## 15. Example queries enabled

```cypher
-- Buildings where OSM and authoritative height disagree by > 3 m (cross-source QA)
MATCH (b:Building)-[:ENRICHED_BY {is_primary:true}]->(o:OsmFeature)
WHERE o.osm_height IS NOT NULL AND b.measured_height IS NOT NULL
  AND abs(toFloat(o.osm_height) - b.measured_height) > 3.0
RETURN b.id, b.measured_height, o.osm_height, o.name
ORDER BY abs(toFloat(o.osm_height) - b.measured_height) DESC

-- All POIs (shops, cafés, offices) inside a given building
MATCH (b:Building {id:'DEHHALKA4IB000Cv'})-[:HAS_POI]->(p:OsmFeature)
RETURN p.osm_amenity, p.osm_shop, p.name, p.addr_housenumber

-- pykci buildings still missing any OSM correspondence (data-gap analysis)
MATCH (b:Building) WHERE NOT (b)-[:ENRICHED_BY]->(:OsmFeature)
RETURN count(b) AS unmatched_buildings

-- Inspect a non-trivial match (n:1 or n:m) with confidence weights
MATCH (b:Building)-[r:ENRICHED_BY]->(o:OsmFeature {osm_kind:'building'})
WHERE b.osm_match_type IN ['n:1','n:m']
RETURN b.id, collect({osm:o.osm_id, jaccard:r.jaccard, overlap:r.overlap_ratio, primary:r.is_primary})
```

---

## 16. Completeness gate — `eval/osm_ingestion_census.py`

Fusion statistics (match rates, orphan counts) describe *what was matched*, not whether
anything was silently dropped or corrupted on the way into the graph. The **OSM ingestion
census** closes that gap: it re-derives the expected graph content from the source
`.osm.pbf` using the *same code path* the ingestion used (`extract_layer`,
`classify_kind`, `build_osm_props`, `_pos_list` imported from `ingest_osm`), then verifies
the live graph at five levels — (1) every admitted feature exists exactly once under its
stable id, no extras; (2) the full tag dict is reconstructable from the node
(canonical-JSON SHA1); (3) geometry `pos_list` strings match the source verbatim
(order-independent digest) and bboxes match exactly; (4) the attach-or-orphan partition is
exact and exhaustive (`matched=true` ⇔ ≥1 incoming `ENRICHED_BY`/`HAS_POI`;
`matched=false` ⇒ `unmatched_reason`, no anchor edge; anchor `osm_*` keys stay inside
`ENRICH_KEYS`); (5) every node is R-tree-registered with full provenance + `PART_OF`.
A scope pass reports unfiltered vs. admitted counts per GDAL layer, so the admission
boundary is quantified rather than implicit. Exit 0 only when every gate is clean.

Run it after **every** fusion (per city): see [eval/OSM_INGESTION_CENSUS.md](eval/OSM_INGESTION_CENSUS.md)
for the full method + the Hamburg result (2026-07-10: **PASSED** — 1,188,139/1,188,139
features lossless and correctly placed, 0 violations on all gates).

---

## 17. References

- A. Naumann, J. Spoerhase, S. Wolff. *Many-To-Many Polygon Matching à la Jaccard.* ESA
  2024 — formal optimal m:n footprint matching (NP-complete); our rigorous-alternative
  baseline. https://drops.dagstuhl.de/storage/00lipics/lipics-vol308-esa2024/LIPIcs.ESA.2024.90/LIPIcs.ESA.2024.90.pdf
- M. Brovelli et al. *A New Method for the Assessment of Spatial Accuracy and Completeness
  of OpenStreetMap Building Footprints.* ISPRS IJGI 7(8):289, 2018 — IoU/overlap
  significance filtering and 1:1 vs 1:n footprint correspondence.
  https://www.mdpi.com/2220-9964/7/8/289
- *DBSM R2023 — pan-European building footprints* (JRC) — hierarchical conflation of OSM +
  ML footprints + settlement layers against cadastral data. https://data.jrc.ec.europa.eu/dataset/60c6b14d-3dda-4034-b461-390dc8ed8665
- OSM *Map Features* and the GDAL OSM driver `osmconf.ini` — tag→column promotion model.
```
