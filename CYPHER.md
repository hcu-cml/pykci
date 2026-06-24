# Neo4j Cypher 25 — Reference

Source: https://neo4j.com/docs/cypher-cheat-sheet/25/all/

---

## Query Structure

### Read
```
[USE] [MATCH [WHERE]] [OPTIONAL MATCH [WHERE]]
[WITH [ORDER BY] [SKIP] [LIMIT] [WHERE]]
RETURN [ORDER BY] [SKIP] [LIMIT]
```

### Write
```
[USE] [CREATE | MERGE [ON CREATE SET] [ON MATCH SET]]
[WITH] [SET | DELETE | REMOVE | FOREACH]
[RETURN]
```

Note: Cypher keywords are case-insensitive; variable names are case-sensitive.

---

## Clauses

### MATCH
```cypher
MATCH (n) RETURN n                              -- all nodes
MATCH (n:Label) RETURN n                        -- by label
MATCH (n:Label {prop: value}) RETURN n          -- by property
MATCH (a)-[r:TYPE]->(b) RETURN a, r, b          -- relationship
MATCH (a)-[*1..3]->(b) RETURN a, b              -- variable-length (2–5 hops)
MATCH path = (a)-[*]-(b) RETURN path            -- capture path
```

### OPTIONAL MATCH
Returns `null` for missing pattern parts instead of omitting the row:
```cypher
MATCH (n:Person)
OPTIONAL MATCH (n)-[:HAS_ADDRESS]->(addr)
RETURN n.name, addr.city
```

### WHERE
```cypher
WHERE n.age > 30
WHERE n.name STARTS WITH 'A'
WHERE n.name ENDS WITH 'z'
WHERE n.name CONTAINS 'ham'
WHERE n.name =~ 'A.*'                       -- regex
WHERE n:Person OR n:Company                 -- label check
WHERE n.prop IS NOT NULL
WHERE n.prop IS NULL
WHERE n.prop IN [1, 2, 3]
WHERE exists((n)-[:KNOWS]->())              -- pattern existence
WHERE NOT (n)-[:BLOCKS]->(m)
```

### RETURN
```cypher
RETURN n                                    -- node
RETURN n.name AS name                       -- alias
RETURN DISTINCT n.city                      -- deduplicate
RETURN *                                    -- all bound variables
```

### WITH
Passes results between query parts; supports aggregation, filtering, ordering:
```cypher
MATCH (n:Building)
WITH n ORDER BY n.measured_height DESC LIMIT 10
MATCH (n)-[:HAS_ROOF_TYPE]->(rt)
RETURN n.id, rt.name
```

### ORDER BY / SKIP / LIMIT
```cypher
RETURN n ORDER BY n.name ASC, n.age DESC
RETURN n SKIP 10 LIMIT 5
```

### CREATE
```cypher
CREATE (n:Label {prop: value})
CREATE (a)-[:TYPE {prop: val}]->(b)
CREATE (a)-[:TYPE]->(b:Label {prop: value})
```

### MERGE
Matches pattern or creates it if absent:
```cypher
MERGE (n:Label {id: 'x'})
  ON CREATE SET n.created = timestamp()
  ON MATCH  SET n.updated = timestamp()
```

### SET / REMOVE
```cypher
SET n.prop = value
SET n += {prop1: v1, prop2: v2}            -- add/update, keep others
SET n = {prop1: v1}                        -- replace all properties
SET n:NewLabel
REMOVE n.prop
REMOVE n:Label
```

### DELETE
```cypher
DELETE r                                   -- remove a relationship
DELETE n                                   -- node (must have no rels)
DETACH DELETE n                            -- node + all its relationships
```

### UNWIND
Expands a list into individual rows:
```cypher
UNWIND [1, 2, 3] AS x RETURN x
UNWIND n.list_prop AS item RETURN item
```

### FOREACH
Executes update operations on each element of a list:
```cypher
FOREACH (x IN list | SET x.visited = true)
FOREACH (id IN ids | MERGE (:Label {id: id}))
```

### CALL (procedure)
```cypher
CALL db.labels() YIELD label RETURN label
CALL dbms.components() YIELD name, versions
CALL spatial.bbox('layer', {x:0,y:0}, {x:1,y:1}) YIELD node
CALL gds.louvain.write('graph', {writeProperty: 'community_id'}) YIELD communityCount
```

### CALL (subquery)
Executes once per incoming row; variables from outer scope are accessible:
```cypher
MATCH (n:Building)
CALL {
  WITH n
  MATCH (n)-[:HAS_BOUNDARY]->(s)
  RETURN count(s) AS surfaces
}
RETURN n.id, surfaces
```

### CALL IN TRANSACTIONS (batch writes)
```cypher
CALL {
  MATCH (n:Building) WHERE n.processed IS NULL RETURN n
} IN TRANSACTIONS OF 1000 ROWS
SET n.processed = true
```

### UNION

**All branches must return the exact same column names** — otherwise Neo4j raises a syntax error.

```cypher
-- Correct: same alias 'id' in every branch
MATCH (n:Building)   RETURN n.id AS id
UNION                                      -- distinct results
MATCH (n:CityObject) RETURN n.id AS id

UNION ALL                                  -- includes duplicates
MATCH (n:Dataset)    RETURN n.id AS id
```

**Never use UNION to combine different aggregations** (e.g. node count + relationship count) — the column names will differ and the query will fail.
Use WITH chaining instead:

```cypher
-- Count nodes and relationships in one query
MATCH (n) WITH count(n) AS nodes
MATCH ()-[r]->() RETURN nodes, count(r) AS relationships

-- Count by label alongside relationship count
MATCH (n) WITH labels(n)[0] AS label, count(n) AS cnt
MATCH ()-[r]->() WITH label, cnt, count(r) AS rels
RETURN label, cnt, rels ORDER BY cnt DESC
```

### LOAD CSV
```cypher
LOAD CSV WITH HEADERS FROM 'file:///data.csv' AS row
CREATE (:Label {id: row.id, name: row.name})

LOAD CSV FROM 'file:///data.csv' AS row FIELDTERMINATOR ';'
RETURN row[0], row[1]
```

---

## Patterns

### Node patterns
```cypher
(n)                          -- any node
(n:Label)                    -- with label
(n:Label {prop: value})      -- with property
(n:A|B)                      -- label OR (n must have A or B)
(n:A&B)                      -- label AND (n must have both)
```

### Relationship patterns
```cypher
(a)--(b)                     -- any direction, any type
(a)-[r]->(b)                 -- outgoing, any type
(a)<-[r]-(b)                 -- incoming
(a)-[r:TYPE]->(b)            -- specific type
(a)-[r:A|B]->(b)             -- type OR
(a)-[r:TYPE {prop: v}]->(b)  -- with property
```

### Variable-length paths
```cypher
(a)-[*]->(b)                 -- any number of hops
(a)-[*2..5]->(b)             -- 2 to 5 hops
(a)-[*..3]->(b)              -- up to 3 hops
(a)-[*3..]->(b)              -- at least 3 hops
```

### Shortest paths
```cypher
MATCH path = shortestPath((a)-[*]-(b))
RETURN path, length(path)

MATCH path = allShortestPaths((a)-[*]-(b))
RETURN path
```

---

## Predicates

### Comparison operators
```
=    <>    <    <=    >    >=    IN
IS NULL    IS NOT NULL    PROPERTY_EXISTS(n, 'prop')
```

### Boolean operators
```
AND    OR    XOR    NOT
```

### String operators
```cypher
STARTS WITH    ENDS WITH    CONTAINS    =~   (regex)
```

### List predicates
```cypher
WHERE x IN [1, 2, 3]
WHERE all(x IN list WHERE x > 0)
WHERE any(x IN list WHERE x > 0)
WHERE none(x IN list WHERE x < 0)
WHERE single(x IN list WHERE x = 1)
```

### CASE expression
```cypher
-- Simple form
CASE n.status
  WHEN 'active'   THEN 'yes'
  WHEN 'inactive' THEN 'no'
  ELSE 'unknown'
END

-- Generic form
CASE
  WHEN n.age < 18 THEN 'minor'
  WHEN n.age < 65 THEN 'adult'
  ELSE 'senior'
END
```

---

## Functions

### Aggregating
```cypher
count(n)                       count(*)                count(DISTINCT n.prop)
sum(n.prop)                    avg(n.prop)
min(n.prop)                    max(n.prop)
collect(n.prop)                collect(DISTINCT n.prop)
stDev(n.prop)                  stDevP(n.prop)
percentileCont(n.prop, 0.9)    percentileDisc(n.prop, 0.9)
```

### Scalar
```cypher
id(n)             elementId(n)
labels(n)         type(r)           properties(n)      keys(n)
size(x)           length(path)
coalesce(a, b)    nullIf(a, b)
head(list)        last(list)        tail(list)
timestamp()       randomUUID()
toBoolean(x)      toBooleanOrNull(x)
toFloat(x)        toFloatOrNull(x)
toInteger(x)      toIntegerOrNull(x)
toString(x)
startNode(r)      endNode(r)
```

### String
```cypher
toLower(s)     toUpper(s)
trim(s)        ltrim(s)         rtrim(s)          btrim(s)
left(s, n)     right(s, n)
substring(s, start)             substring(s, start, len)
replace(s, search, repl)        split(s, delim)
reverse(s)     size(s)          normalize(s)
```

### Mathematical — numeric
```cypher
abs(n)    ceil(n)    floor(n)    round(n)    sign(n)    isNaN(n)
rand()    -- random float [0, 1)
```

### Mathematical — logarithmic / trigonometric
```cypher
sqrt(n)   log(n)   log10(n)   exp(n)   e()   pi()
sin(x)    cos(x)   tan(x)     asin(x)  acos(x)  atan(x)  atan2(y, x)
degrees(x)   radians(x)   haversin(x)
```

### List
```cypher
size(list)
range(start, end)               range(start, end, step)
reverse(list)                   tail(list)

-- Comprehension
[x IN list WHERE x > 0]         -- filter
[x IN list | x * 2]             -- transform
[x IN list WHERE x > 0 | x * 2] -- filter + transform

-- Reduce
reduce(acc = 0, x IN list | acc + x)

-- Type-converting
toBooleanList(list)    toFloatList(list)    toIntegerList(list)    toStringList(list)
```

### Map / property access
```cypher
n.prop                          -- static property access
n['prop']                       -- dynamic property access
{key: value, key2: value2}      -- map literal
n {.prop1, .prop2}              -- map projection (subset)
n {.*}                          -- map projection (all props)
```

---

## Indexes

### B-tree index (auto-used by planner)
```cypher
CREATE INDEX my_idx IF NOT EXISTS FOR (n:Label) ON (n.prop)
CREATE INDEX my_idx IF NOT EXISTS FOR (n:Label) ON (n.prop1, n.prop2)  -- composite
DROP INDEX my_idx IF EXISTS
SHOW INDEXES
```

### Vector index (manual lookup)
```cypher
CREATE VECTOR INDEX vec_idx IF NOT EXISTS FOR (n:Label) ON (n.embedding)
  OPTIONS {indexConfig: {`vector.dimensions`: 1536, `vector.similarity_function`: 'cosine'}}

CALL db.index.vector.queryNodes('vec_idx', 10, $queryVector) YIELD node, score
RETURN node.id, score ORDER BY score DESC
```

---

## APOC Plugin (available)

```cypher
-- Collection utilities
apoc.coll.max([1, 2, 3])                 -- → 3
apoc.coll.min([1, 2, 3])                 -- → 1
apoc.coll.flatten([[1,2],[3]])            -- → [1,2,3]
apoc.coll.indexOf(list, value)
apoc.coll.sort(list)
apoc.coll.distinct(list)

-- Batch processing
CALL apoc.periodic.iterate(
  'MATCH (n:Building) RETURN n',
  'SET n.processed = true',
  {batchSize: 1000, parallel: false}
)

-- Type conversion
apoc.convert.toJson({key: val})
```

---

## Type Predicate Expressions

```cypher
n.prop IS :: INTEGER
n.prop IS :: FLOAT
n.prop IS :: STRING
n.prop IS :: BOOLEAN
n.prop IS :: LIST
n.prop IS :: MAP
n.prop IS :: DATE
n.prop IS NOT :: INTEGER
```

---

## Common Patterns

### Count nodes by label
```cypher
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC
```

### Relationship distribution
```cypher
MATCH (n)-[r]->() RETURN type(r) AS rel, count(r) AS cnt ORDER BY cnt DESC
```

### Conditional property set
```cypher
WITH n, CASE WHEN n.x IS NULL THEN 0 ELSE n.x END AS x
SET n.x = x
```

### Collect then process
```cypher
MATCH (n:Building)
WITH collect(n) AS nodes
UNWIND nodes AS n
RETURN n.id
```

### Fuzzy name match
```cypher
MATCH (n:Building)
WHERE toLower(n.name) CONTAINS toLower($term)
RETURN n.id, n.name
```

### Inline all literal values (no $params in REPL)
```cypher
-- Correct for Neo4j Browser / REPL
MATCH (n:Building) WHERE n.measured_height > 20 RETURN n.id, n.measured_height

-- $params only valid when using a driver (Python neo4j, etc.)
MATCH (n:Building {id: $id}) RETURN n
```

### Avoid full label scans for range filters
```cypher
-- Bad (full scan):
MATCH (b:Building) WHERE b.center_x > 566000 RETURN b

-- Good (use index or spatial procedure):
MATCH (b:Building) WHERE b.measured_height > 20 RETURN b   -- uses B-tree index on measured_height
CALL spatial.bbox('features', {x:566000,y:5932000}, {x:567000,y:5933000}) YIELD node  -- uses R-tree
```

---

## Neo4j Spatial Plugin — R-tree Procedures

Source: <https://neo4j.com/labs/neo4j-spatial/5/api/>

The Neo4j Spatial plugin provides R-tree (and other) spatial indexes via `spatial.*` procedures.
These are **plugin procedures** — not built-in Cypher — and must be called with `CALL … YIELD`.
This project uses a **WKT layer** named `'features'` containing all `Building` and `CityObject` nodes indexed by their `bbox_wkt` property (2D POLYGON in the dataset CRS, ETRS89 UTM32).

> **Rule:** always use `spatial.bbox`, `spatial.intersects`, or `spatial.withinDistance` as the entry
> point for any query that filters nodes by geographic location. Never substitute a full label scan
> with a `WHERE` filter on coordinate properties.

---

### Spatial Query Procedures

These are the primary procedures for querying the R-tree index.

#### `spatial.bbox` — bounding-box query

```cypher
CALL spatial.bbox(layerName :: STRING, min :: ANY, max :: ANY) YIELD node :: NODE
```

Returns all nodes whose indexed geometry falls within the axis-aligned bounding rectangle.

`min` / `max` accept any of:

- Map: `{x: <float>, y: <float>}` (projected CRS) or `{longitude: <float>, latitude: <float>}` (WGS84)
- Neo4j `point({x: …, y: …})`
- WKT coordinate string

```cypher
-- All features in a UTM32 rectangle
CALL spatial.bbox('features', {x: 566500.0, y: 5932800.0}, {x: 567000.0, y: 5933200.0}) YIELD node
RETURN node.id, labels(node) AS types, node.measured_height AS height
ORDER BY height DESC

-- Buildings only
CALL spatial.bbox('features', {x: 566500.0, y: 5932800.0}, {x: 567000.0, y: 5933200.0}) YIELD node
WHERE node:Building
RETURN node.id, node.measured_height
```

#### `spatial.intersects` — geometry intersection

```cypher
CALL spatial.intersects(layerName :: STRING, geometry :: ANY) YIELD node :: NODE
```

Returns all nodes whose indexed geometry intersects the given shape.

`geometry` accepts: WKT string, Neo4j point, map `{x,y}` / `{latitude,longitude}`.

```cypher
-- Features intersecting a WKT polygon
CALL spatial.intersects('features',
     'POLYGON((566500 5932800, 567000 5932800, 567000 5933200, 566500 5933200, 566500 5932800))')
YIELD node
RETURN node.id, labels(node) AS types

-- Features intersecting a reference building's bbox
MATCH (ref:Building {id: 'DEHHALKA4IB000Cv'})
CALL spatial.intersects('features', ref.bbox_wkt) YIELD node
WHERE node.id <> ref.id
RETURN node.id, labels(node) AS types
```

#### `spatial.withinDistance` — radius query

```cypher
CALL spatial.withinDistance(layerName :: STRING, coordinate :: ANY, distanceInKm :: FLOAT)
YIELD node :: NODE, distance :: FLOAT
```

Returns nodes within `distanceInKm` kilometres from `coordinate`, ordered by proximity.
Also yields `distance` (in km) for each result.

`coordinate` accepts: map `{x,y}` / `{latitude,longitude}`, Neo4j point, node, WKT.

```cypher
-- Buildings within 0.5 km of a point (UTM32 coords → convert to km-based distance)
CALL spatial.withinDistance('features', {x: 566750.0, y: 5933000.0}, 0.5) YIELD node, distance
WHERE node:Building
RETURN node.id, node.measured_height, round(distance * 1000) AS distance_m
ORDER BY distance ASC

-- Nearest 10 features to a point
CALL spatial.withinDistance('features', {x: 566750.0, y: 5933000.0}, 5.0) YIELD node, distance
RETURN node.id, labels(node) AS types, distance
ORDER BY distance ASC LIMIT 10
```

#### `spatial.closest` — nearest within threshold

```cypher
CALL spatial.closest(layerName :: STRING, coordinate :: ANY, distanceInKm :: FLOAT) YIELD node :: NODE
```

Like `withinDistance` but does not yield `distance`. Useful when only the nodes are needed.

#### `spatial.cql` — ECQL filter

```cypher
CALL spatial.cql(layerName :: STRING, ecql :: STRING) YIELD node :: NODE
```

Applies an OGC ECQL (Extended Common Query Language) filter against the layer.

```cypher
CALL spatial.cql('features', "BBOX(bbox_wkt, 566500, 5932800, 567000, 5933200)") YIELD node
RETURN node.id
```

---

### Layer Management Procedures

#### Create layers

```cypher
-- WKT layer (used in this project): reads geometry from a node string property
CALL spatial.addWKTLayer(name :: STRING, nodePropertyName :: STRING, indexConfig = "" :: STRING)
YIELD node :: NODE

-- Example: create the project's feature layer
CALL spatial.addWKTLayer('features', 'bbox_wkt')

-- Simple point layer (lat/lon properties)
CALL spatial.addPointLayerXY(name :: STRING, xProperty :: STRING, yProperty :: STRING,
     indexType = "rtree" :: STRING, crsName = "" :: STRING, indexConfig = "" :: STRING)
YIELD node :: NODE

-- Native Neo4j point layer
CALL spatial.addNativePointLayer(name :: STRING, indexType = "rtree" :: STRING,
     crsName = "" :: STRING, indexConfig = "" :: STRING)
YIELD node :: NODE

-- Generic layer (specify encoder class)
CALL spatial.addLayer(name :: STRING, type :: STRING, encoderConfig :: STRING,
     indexConfig = "" :: STRING)
YIELD node :: NODE
```

Index type options: `"rtree"` (default), `"geohash"`, `"hilbert"`, `"zorder"`

#### Inspect layers

```cypher
-- List all spatial layers
CALL spatial.layers() YIELD name, signature

-- Get layer root node
CALL spatial.layer(name :: STRING) YIELD node :: NODE

-- Get layer metadata (geometry type, CRS, attributes)
CALL spatial.layerMeta(name :: STRING)
YIELD name, geometryType, crs, hasComplexAttributes, extraAttributes

-- Get layer bounding box
CALL spatial.getLayerBoundingBox(name :: STRING)
YIELD minX, minY, maxX, maxY, crs

-- Get feature count in layer
CALL spatial.getFeatureCount(name :: STRING) YIELD count :: INTEGER

-- Get feature attributes with class types
CALL spatial.getFeatureAttributes(name :: STRING) YIELD name, className

-- List all available layer types / encoders
CALL spatial.layerTypes()
YIELD id, encoder, layer, index, crsName, defaultEncoderConfig

-- List all spatial procedures with signatures
CALL spatial.procedures() YIELD name, signature
```

#### Remove layers

```cypher
CALL spatial.removeLayer(name :: STRING)
```

---

### Node Index Management

```cypher
-- Add a single node to a layer
CALL spatial.addNode(layerName :: STRING, node :: NODE) YIELD node :: NODE

-- Add multiple nodes (batch — preferred for large datasets)
CALL spatial.addNodes(layerName :: STRING, nodes :: LIST<NODE>) YIELD count :: INTEGER

-- Example: index all features with a bbox_wkt property
MATCH (n) WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NOT NULL
WITH collect(n) AS nodes
CALL spatial.addNodes('features', nodes) YIELD count
RETURN count AS indexed

-- Remove a single node from the index (does not delete the node)
CALL spatial.removeNode(layerName :: STRING, node :: NODE) YIELD nodeId :: STRING

-- Remove multiple nodes from the index
CALL spatial.removeNodes(layerName :: STRING, nodes :: LIST<NODE>) YIELD count :: INTEGER
```

---

### WKT / Geometry Management

```cypher
-- Add a raw WKT string as a new geometry node in the layer
CALL spatial.addWKT(layerName :: STRING, geometry :: STRING) YIELD node :: NODE

-- Add multiple WKT strings
CALL spatial.addWKTs(layerName :: STRING, geometry :: LIST<STRING>) YIELD node :: NODE

-- Update the geometry of an existing node
CALL spatial.updateWKT(layerName :: STRING, node :: NODE, geometry :: STRING) YIELD node :: NODE
```

---

### Geometry Conversion Functions

These are **functions** (not procedures) — use them inside expressions.

```cypher
-- Convert geometry to Neo4j native geometry type
spatial.asGeometry(geometry :: ANY) :: ANY

-- Convert geometry to a Map representation
spatial.asMap(object :: ANY) :: ANY

-- Decode an indexed node back to Neo4j native geometry using the layer's encoder
spatial.decodeGeometry(layerName :: STRING, node :: NODE) :: ANY

-- Extract attributes from a node using the layer's encoder
spatial.extractAttributes(layerName :: STRING, node :: NODE) :: MAP

-- Convert a Neo4j point or point array to WKT
spatial.neo4jGeometryToWkt(data :: ANY) :: STRING

-- Convert the indexed geometry of a node in a layer to WKT
spatial.nodeAsWKT(layerName :: STRING, node :: NODE) :: STRING

-- Convert a WKT string to a GeoJSON structure
spatial.wktToGeoJson(wkt :: STRING) :: ANY
```

Usage examples:
```cypher
-- Read the indexed WKT of a node
MATCH (b:Building {id: 'DEHHALKA4IB000Cv'})
RETURN spatial.nodeAsWKT('features', b) AS wkt

-- Convert bbox_wkt to GeoJSON
MATCH (b:Building {id: 'DEHHALKA4IB000Cv'})
RETURN spatial.wktToGeoJson(b.bbox_wkt) AS geojson

-- Get geometry as a map
MATCH (b:Building {id: 'DEHHALKA4IB000Cv'})
RETURN spatial.asMap(spatial.decodeGeometry('features', b)) AS geom
```

---

### Import Procedures

```cypher
-- Import an OSM file; creates a layer named after the file
CALL spatial.importOSM(uri :: STRING) YIELD count :: INTEGER

-- Import an OSM file into an existing layer
CALL spatial.importOSMToLayer(layerName :: STRING, uri :: STRING) YIELD count :: INTEGER

-- Import a Shapefile; creates a layer named after the file
CALL spatial.importShapefile(uri :: STRING) YIELD count :: INTEGER

-- Import a Shapefile into an existing layer
CALL spatial.importShapefileToLayer(layerName :: STRING, uri :: STRING) YIELD count :: INTEGER
```

---

### Coordinate Input Formats (all query procedures accept these)

| Format | Example |
| ------ | ------- |
| Map — projected (UTM, etc.) | `{x: 566750.0, y: 5933000.0}` |
| Map — geographic | `{longitude: 9.993, latitude: 53.546}` |
| Neo4j point | `point({x: 566750.0, y: 5933000.0})` |
| WKT string | `'POINT(566750.0 5933000.0)'` |

---

### Quick-Reference: Most-Used Spatial Calls (this project)

```cypher
-- Setup: create WKT layer (once, idempotent when layer already exists)
CALL spatial.addWKTLayer('features', 'bbox_wkt')

-- Setup: batch-index all features
MATCH (n) WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NOT NULL
WITH collect(n) AS nodes
CALL spatial.addNodes('features', nodes) YIELD count RETURN count

-- Query: bounding box
CALL spatial.bbox('features', {x: <minX>, y: <minY>}, {x: <maxX>, y: <maxY>}) YIELD node
WHERE node:Building   -- optional: filter by type
RETURN node.id, node.measured_height ORDER BY node.measured_height DESC

-- Query: intersection with a WKT polygon
CALL spatial.intersects('features', '<WKT>') YIELD node
RETURN node.id, labels(node) AS types

-- Query: radius search (distance in km)
CALL spatial.withinDistance('features', {x: <cx>, y: <cy>}, <radiusKm>) YIELD node, distance
RETURN node.id, round(distance * 1000) AS distance_m ORDER BY distance ASC

-- Inspect: list layers and feature count
CALL spatial.layers() YIELD name, signature
CALL spatial.getFeatureCount('features') YIELD count
```
