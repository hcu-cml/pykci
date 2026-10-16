# Street trees (`enrich_street_trees.py`, `build_tree_tiles.py`)

Adds a city's street-tree cadastre to the graph as authoritative `SolitaryVegetationObject`
features, and builds an instanced, textured 3D Tiles layer for them from the graph alone.
Written for Hamburg's *Straßenbaumkataster* (summer trees, 2021-09-01, © LGV Hamburg,
dl-de/by-2-0); it works for any CityGML 2.0 file of `veg:SolitaryVegetationObject` with
`lod{N}ImplicitRepresentation`.

## Input

One CityGML 2.0 file plus its texture folder, e.g.

```text
Strassenbaumkataster_20210901_Sommerbaeume.gml          486 MB, 224,185 trees
Strassenbaumkataster_20210901_Sommerbaeume_appearance/  115 PNG textures (2048², RGBA)
```

- Every tree is an `ImplicitGeometry`: a 4×4 `transformationMatrix` (rotation about the
  vertical, horizontal and vertical scale), a `referencePoint` (the trunk base) and a
  `relativeGMLGeometry`. The 115 prototype shapes (`gml:TriangulatedSurface`, 80–792
  triangles, median 608) are written inline at their first use and referenced by
  `xlink:href` everywhere else.
- One `app:ParameterizedTexture` plus one `app:X3DMaterial` per prototype; texture
  coordinates per triangle.
- Generic attributes (strings): `Baumart`, `Gattung`, `Baumnummer`, `Straße`,
  `Hausnummer`, `Bezirk`, `Hoehe_aus_ALS`, `Kronendurchmesser`, `Stammumfang`,
  `Pflanzjahr`, each also as `*_Text`.
- No `gml:boundedBy` per tree, and no municipality key.

## Run

```bash
# after ingest_citygml.py has loaded the city model
python enrich_street_trees.py input/citygml/Strassenbaumkataster_20210901_Sommerbaeume.gml
python enrich_street_trees.py trees.gml --skip-ingest   # re-run steps 2–5 only
python enrich_street_trees.py trees.gml --relocate      # recompute every position

# 3D Tiles for the trees, next to the city tileset of build_3dtiles.py --tiled
python build_tree_tiles.py --out ../pykci-client/data/tiles/hamburg/trees \
    --texture-root input/citygml \
    --align ../pykci-client/data/tiles/hamburg/tileset.json
```

`--texture-root` is the folder the `imageURI`s are relative to (the one holding the
`_appearance` folder). `--align` reads `geoid_offset_m` from a city tileset built with
`build_3dtiles.py --tiled` (as used by pykci-client), so trees and buildings share one
vertical convention; without it, or for a tileset that does not record it, the offset is 0. Connection: `NEO4J_URI` (or `NEO4J_URL`),
`NEO4J_USER`, `NEO4J_PASSWORD`. `build_tree_tiles.py` needs Pillow for the textures.

Hamburg, full file: ingest 101 s, prototypes 8 s, R-tree 19 s; tiles 77 s.

## What `enrich_street_trees.py` does

Every step is idempotent (MERGE / SET); a second run changes no labels, relationships or
R-tree entries.

1. **Ingest** through `ingest_citygml.ingest()` with `nearby_edges=False` and
   `skip_spatial_index=True` (both are passes over every Building in the graph). Gives the
   tree nodes with their generic attributes, one implicit `GeometryMultiSurface` per tree,
   the `Dataset` and `PART_OF`.
2. **Prototypes.** `ingest_citygml` resolves an `xlink:href` by copying the prototype's
   polygons onto each instance: 224k trees × ~615 triangles ≈ 138 M relationships. Instead,
   each prototype is stored once as a `GeometryPrototype` (triangles like a TIN relief) and
   every implicit geometry gets one `HAS_PROTOTYPE` edge. The appearance stays with the
   prototype (image, mime type, theme, material) and its rings (texture coordinates), so the
   textured trees can be rebuilt from the graph alone. `GeometryPrototype` is deliberately
   not a `GeometryMultiSurface`: a later `ingest_citygml` run would otherwise copy the
   triangles onto every tree after all.
3. **Locate.** From the reference point and the transformed prototype: `center_x/center_y`
   (trunk), `ground_z`, `location`, `bbox_*` (crown extent and tree height) and `bbox_wkt`
   (crown footprint, at least `--footprint` m).
4. **Index** the located trees in the `features` R-tree (remove then add per batch, so
   re-runs do not duplicate entries).
5. **Tidy.** The District and City the ingest named after the file are removed; the tree
   `Dataset` `COVERS` the city model's district(s) and records the file's licence and
   attribution comments.

## Graph model

```text
(:SolitaryVegetationObject:CityObject:Entity {id: 'Veg_<n>', feature_type, source,
    baumart, gattung, baumnummer, `straße`, hausnummer, bezirk, hoehe_aus_als,
    kronendurchmesser, stammumfang, pflanzjahr, *_text, _gen_attrs_map,
    center_x, center_y, ground_z, location, bbox, bbox_min_*/bbox_max_*, bbox_wkt})
  -[:HAS_LOD_SURFACE {lod: '2'}]->
(:GeometryMultiSurface {id: 'impl_ms_<tree>_lod2', is_implicit: true,
    transformation_matrix, reference_point, prototype_href})
  -[:HAS_PROTOTYPE]->
(:GeometryPrototype {id: 'FME_GeometryIntance<n>', geometry_type: 'TriangulatedSurface',
    triangle_count, local_min_*/local_max_*, srs_name, texture_image, texture_mime_type,
    appearance_theme, x3d_shininess, x3d_specular_color, owner_feature_id})
  -[:HAS_SURFACE_MEMBER {order}]->(:GeometryPolygon)-[:HAS_EXTERIOR_RING]->
(:GeometryRing {pos_list, texture_coordinates})       -- prototype-local coordinates

(:SolitaryVegetationObject)-[:PART_OF]->(:Dataset {id: 'strassenbaumkataster_20210901_sommerbaeume',
    license, attribution, crs})-[:COVERS]->(:District)
```

Hamburg adds 593,243 nodes and 1,041,498 relationships (graph: 18,034,167 nodes,
27,340,889 relationships). Querying:

- The values are strings, kept verbatim: `toFloat(t.hoehe_aus_als) > 20`.
- `straße` contains ß: always write `` t.`straße` ``. `hausnummer = '0'` means none,
  `kronendurchmesser = '0'` means unknown.
- Trees share the `features` R-tree with the buildings: filter `node:SolitaryVegetationObject`.
- Trees have no `LOCATED_IN`; use `bezirk` (Altona, Bergedorf, Eimsbüttel, Hamburg-Mitte,
  Hamburg-Nord, Harburg, Wandsbek).

```cypher
// 10 tallest trees in Altona
MATCH (t:SolitaryVegetationObject) WHERE t.bezirk = 'Altona' AND toFloat(t.hoehe_aus_als) > 0
RETURN t.id, t.baumart, toFloat(t.hoehe_aus_als) AS height_m, t.`straße`
ORDER BY height_m DESC, t.id LIMIT 10

// trees within 100 m of the Rathaus (R-tree window, then exact distance)
CALL spatial.intersects('features',
  'POLYGON((565632 5933853, 565832 5933853, 565832 5934053, 565632 5934053, 565632 5933853))')
YIELD node WITH node AS t WHERE t:SolitaryVegetationObject
  AND point.distance(t.location, point({x: 565732, y: 5933953})) <= 100
RETURN t.gattung, count(*) ORDER BY count(*) DESC
```

## What `build_tree_tiles.py` writes

```text
trees/tileset.json        quadtree; internal tiles empty, leaves load within ~--load-distance m
trees/tiles/<q>.glb       one node per prototype with EXT_mesh_gpu_instancing (per-tree
                          TRANSLATION / ROTATION / SCALE) + EXT_instance_features +
                          EXT_structural_metadata (same `feature` class and provenance
                          extras as build_3dtiles.py --tiled)
trees/prototypes/<n>.bin  each prototype's triangles once (positions, normals, UVs)
trees/textures/<n>.png    textures downscaled to --texture-size, colour-dilated into the
                          transparent area so mipmapped leaf edges do not darken
```

Placement: reference point → dataset CRS → WGS84 → ECEF → the tileset's ENU frame; the
matrix's horizontal 2×2 is carried into ENU with the local Jacobian of that projection
(grid convergence and scale factor) and split into a rotation about the vertical plus a
horizontal scale. A tree whose matrix has a zero horizontal scale (Hamburg: 955) is drawn
with its vertical scale.

Hamburg: 212 leaf tiles (103.6 MB) + 115 prototypes (6.8 MB) + 115 textures (57.2 MB).
Checks run on that build: the Khronos glTF validator reports 0 errors and 0 warnings on all
212 tiles; every vertex of 847 sampled trees (all tiles) lies within 0.01 cm of
`M · p + referencePoint`; on a 400-tree sample the 3D crown width matches
`Kronendurchmesser` (ratio 1.00) and the 3D height matches `Hoehe_aus_ALS` (mean deviation
2.5 cm).
