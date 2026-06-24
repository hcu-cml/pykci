# CityGML → Database Mapping Benchmark: pykci vs 3DCityKG vs 3DCityDB v5

**Date:** 2026-06-19
**Input:** `input/citygml/hamburg_citygml2_lod2_2025.gml` — CityGML 2.0, LoD2, Hamburg
**Author of run:** automated benchmark harness (scripts archived in scratchpad; see §10)

---

## 1. Executive summary

Three CityGML-to-database mappers were each run **3×** on the **same 6.93 GB** CityGML 2.0
file, from an **empty database**, under an **identical, enforced resource envelope** (8 logical
CPU cores + 20 GB RAM per tool). Wall-clock mapping time was measured per run and averaged;
afterwards each resulting database was measured for on-disk size, element counts, and the
share devoted to spatial indexing.

| Tool | Backend | Avg mapping time | Peak RAM | DB size (data) | Graph / table counts | Spatial index |
|------|---------|-----------------:|---------:|---------------:|----------------------|---------------|
| **pykci** (`\mytoolname`) | Neo4j | **834.5 s** (13.9 min) | ~12.2 GiB | **11.4 GiB** store | 15.16 M nodes / 21.25 M edges | ✅ R-tree: 7,767 nodes + 396,033 edges + 2 POINT idx |
| **3DCityKG** | Neo4j (embedded) | **872.2 s** (14.5 min) | **~19.7 GiB** | **18.3 GiB** store | 89.49 M nodes / 94.02 M edges | ❌ **0** (R-tree persist failed, all 3 runs) |
| **3DCityDB v5** | PostgreSQL/PostGIS | **625.5 s** (10.4 min) | **~9.7 GiB** | **10.2 GiB** logical | 27.0 M rows in 4 populated tables (54 total) | ✅ 2 GIST (data) + spatial_ref_sys (8,500) |

**Headline findings**

- **3DCityDB v5 was fastest** (~25% faster than pykci, ~28% faster than 3DCityKG) **and
  leanest** (~9.7 GiB peak vs 3DCityKG's ~19.7 GiB).
- **pykci** mapped a complete graph **including a working spatial R-tree** in ~834 s using the
  least RAM of the two Neo4j tools.
- **3DCityKG** mapped all 388,267 buildings but its final **"Storing RTrees in the database"
  transaction failed with "memory pool limit exceeded" on all three runs** — so under default
  configuration on a dataset this large it produces a graph **with no persistent spatial
  index**. It also pushed peak RAM to the 20 GB ceiling.
- **pykci produces by far the most compact graph.** For the *same* 388,267 buildings it builds
  **15.16 M nodes / 21.25 M edges** vs 3DCityKG's **89.49 M / 94.02 M** — **5.9× fewer nodes** and
  **4.4× fewer edges** (≈ 94 graph elements per building vs ≈ 473). pykci maps a *condensed
  semantic schema* (one node per domain concept), whereas 3DCityKG materialises the full citygml4j
  Java object model 1:1 (every `SurfaceProperty`/`ChildList`/`ArrayList` wrapper becomes a node).
- Counts are **not cross-tool comparable** as absolutes (graph nodes vs relational rows), but the
  contrast is telling: the same city is encoded as 89.5 M nodes (3DCityKG), 27.0 M rows
  (3DCityDB), or **15.2 M nodes (pykci)** — the leanest representation.
- **Fairness re-check (§5.1):** pykci was also re-run **fully containerized** (Docker image, same
  envelope, same bind-mount read path as the other tools). This cost only +4.6 % (834.5 s →
  873.1 s) and **left the ranking unchanged** — fully containerized, pykci (873.1 s) ties
  3DCityKG (872.2 s), and 3DCityDB (625.5 s) stays fastest.

---

## 2. Test environment

| | |
|---|---|
| CPU | AMD Ryzen 7 9800X3D — 8 cores / 16 logical processors |
| RAM | 61.6 GB |
| OS | Windows 11 Education 26200 |
| Docker | 28.5.1 (Docker Desktop, WSL2 backend); 30.2 GB allocated to Docker |
| Neo4j (pykci) | project image, Neo4j 2025.10.1, APOC + Spatial plugins |
| Neo4j (3DCityKG) | bundled in `tumgis/3dcitykg:latest`, Neo4j 5.26.14 + APOC (embedded) |
| 3DCityDB | `3dcitydb/3dcitydb-pg` (PostgreSQL 18.3, PostGIS 3.6.2), `3dcitydb/citydb-tool` v1.3.2, 3DCityDB schema v5.1.3 |

### Input dataset

| | |
|---|---|
| File size | 6,927,484,219 bytes (6.45 GiB / 6.93 GB) |
| Format | CityGML 2.0, LoD2 |
| CRS | `urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH` → **EPSG:25832** (ETRS89 / UTM zone 32N), 3D |
| Top-level features | **388,267 buildings** (all three tools agree on this count) |
| Boundary surfaces | 4,533,530 (wall/roof/ground) |

---

## 3. Fairness methodology — equal CPU & RAM

Each tool was given the **same hardware envelope**, enforced via Docker cgroup limits and
Windows CPU affinity. *Whether a tool fully utilises the envelope is up to its implementation*
(as requested).

- **CPU — identical 8 logical cores.** Every container was pinned with `--cpuset-cpus=0-7`.
  pykci's host-side Python parser (the only non-containerised component) was pinned to the same
  cores 0-7 via Windows processor affinity (mask `0xFF`). For the two multi-process tools
  (pykci = Python parser + Neo4j; 3DCityDB = importer + PostgreSQL) **both processes share the
  same 8 cores**, so component count gives no CPU advantage. Cores 8–15 were left for the OS,
  Docker, and the harness.
- **RAM — identical 20 GB per component.** Every container was capped with
  `--memory=20g --memory-swap=20g`. The Neo4j container was **restarted under the cap** so its
  JVM detected 20 GB (not the host's 30 GB) at start. 20 GB was chosen because it is the
  smallest round cap that fits **3DCityKG's default mapper heap of `-Xmx16g`** plus off-heap
  overhead — i.e. it lets **every tool run at its out-of-the-box configuration with no
  retuning**.
- **Runs were strictly sequential** — only one tool (and its database) was active at any time,
  so no run competed with another for CPU, RAM, or disk.
- **No tuning.** Default configuration for every tool: pykci default ingest; 3DCityKG defaults
  (case changed only to *map-only*, see §4); 3DCityDB `import citygml` with default
  `--index-mode=keep` (indexes live during import).

### Caveats (documented, not corrected)

1. **pykci's Python parser memory could not be cgroup-capped** (it runs on the host). Its peak
   working set was *measured* instead and stayed at **~0.5 GiB** — far below the 20 GB budget —
   so the lack of a hard cap conferred no advantage. **Addressed:** §5.1 re-runs pykci fully
   containerized (hard 20 GB cgroup cap); parser RAM was unchanged at ~0.5 GiB.
2. **I/O path differs.** In the primary runs pykci read the 6.93 GB file natively from the Windows
   filesystem; 3DCityKG and 3DCityDB read it through a Docker Desktop bind mount (slower
   sequential I/O on WSL2). **Addressed:** §5.1 re-runs pykci through the same bind mount; this
   cost +4.6 % and did not change the ranking.
3. **Co-located vs separate DB.** 3DCityKG runs its mapper and Neo4j **in one JVM/container**;
   pykci and 3DCityDB run the mapper and DB as **separate processes**. This is intrinsic to each
   tool's architecture.
4. **"Peak RAM" = peak working set** (resident, non-reclaimable) sampled from `docker stats`,
   plus the host-process working set for pykci's parser. The cgroup `memory.peak` high-water
   (which includes reclaimable file-cache) was higher — e.g. 3DCityKG's cgroup peak ≈ 19.8 GiB
   including cache. Working set is the meaningful "RAM the tool needs" figure.

---

## 4. Setup & commands per tool (reproducible)

### pykci (`\mytoolname`) → project Neo4j

```bash
# empty DB: recreate the neo4j volume, then constrain + restart so the JVM sees 20 GB
bash eval/reset_neo4j.sh
docker update --cpuset-cpus 0-7 --memory 20g --memory-swap 20g neo4j
docker restart neo4j           # JVM now detects the 20 GB cgroup cap
# map (Python pinned to cores 0-7):
py ingest_citygml.py input/citygml/hamburg_citygml2_lod2_2025.gml
```

### 3DCityKG → bundled embedded Neo4j

`config/run.conf` set to **map-only** (`case = 0`, `citygml.version = "v2_0"`,
`cityjson = false`, `mapper.dataset.paths = ["input/citygml/hamburg_citygml2_lod2_2025.gml"]`).
The image's `CMD` is `gradle run && neo4j console`; the mapping happens during `gradle run`
(embedded Neo4j at `output/neo4jDB`), so the command was overridden to just `gradle run`,
which exits at mapping completion (wall-clockable). Each run used a **fresh empty named volume**.

```bash
docker run --rm --cpuset-cpus 0-7 --memory 20g --memory-swap 20g \
  -v d:/citykg_bench/run.conf:/home/gradle/src/3dcitykg/config/run.conf:ro \
  -v d:/PROJEKTE/llm-kg/input:/home/gradle/src/3dcitykg/input:ro \
  -v citykg_out:/home/gradle/src/3dcitykg/output \
  tumgis/3dcitykg:latest sh -c 'gradle run --no-daemon'
```

### 3DCityDB v5 → PostgreSQL/PostGIS

A user-defined bridge network was used (`--network host` is unusable on Docker Desktop/Windows).
The `3dcitydb-pg` image is PostgreSQL 18+, which requires the volume at `/var/lib/postgresql`
(not `…/data`). Readiness waited for the **second** "ready to accept connections" (the final
server after init scripts) to avoid a connection-refused race.

```bash
docker network create cdbnet
# empty DB:
docker run -d --name cdb --network cdbnet --cpuset-cpus 0-7 --memory 20g --memory-swap 20g \
  -p 5432:5432 -e POSTGRES_PASSWORD=changeMe -e SRID=25832 \
  -v cdb_data:/var/lib/postgresql 3dcitydb/3dcitydb-pg
# import (default --index-mode=keep):
docker run -i --rm --name cdb_import --network cdbnet \
  --cpuset-cpus 0-7 --memory 20g --memory-swap 20g \
  -v d:/PROJEKTE/llm-kg/input/citygml:/data \
  3dcitydb/citydb-tool import citygml \
    -H cdb -P 5432 -d postgres -S citydb -u postgres --db-password=changeMe \
    /data/hamburg_citygml2_lod2_2025.gml
```

---

## 5. Mapping time (3 runs + average)

| Tool | Run 1 (s) | Run 2 (s) | Run 3 (s) | **Average (s)** | Average (min) |
|------|----------:|----------:|----------:|----------------:|--------------:|
| **3DCityDB v5** | 621.59 | 626.95 | 627.99 | **625.51** | 10.43 |
| **pykci** | 844.09 | 833.90 | 825.49 | **834.49** | 13.91 |
| **3DCityKG** | 867.02 | 861.31 | 888.13 | **872.15** | 14.54 |

Run-to-run variance was small (≤ ~3%), confirming deterministic, stable timings. All runs
mapped the full 388,267 buildings; all 3DCityDB runs and pykci runs completed cleanly. All
3DCityKG runs reported `BUILD SUCCESSFUL` but with the R-tree-persist error described in §8.
Wall-clock includes each tool's own process/JVM startup (negligible for pykci/3DCityDB;
~5–10 s gradle/JVM start for 3DCityKG) and, for pykci and 3DCityKG, the time spent building
their spatial R-tree (which **succeeded** for pykci, **failed at commit** for 3DCityKG).

### 5.1 Fairness re-check — pykci fully containerized

The runs above used pykci's parser as a **host process** reading the GML **natively** (caveats §3.1
and §3.2). To remove that asymmetry, pykci's ingest was also packaged as a Docker image
([`docker/Dockerfile.ingest`](../../docker/Dockerfile.ingest)) and re-run **3×** under the
**same envelope** (cpuset 0-7, 20 GB), reading the GML **through a bind mount** (identical I/O
path to 3DCityKG/3DCityDB) and reaching Neo4j via a shared network namespace
(`--network container:neo4j`). The Neo4j container is the same separately-pinned 20 GB instance.

| pykci variant | Run 1 (s) | Run 2 (s) | Run 3 (s) | **Average (s)** | Peak RAM (parser) |
|---------------|----------:|----------:|----------:|----------------:|------------------:|
| Host process, native read | 844.09 | 833.90 | 825.49 | **834.49** | 0.49 GiB |
| **Containerized, bind-mount read** | 874.17 | 877.64 | 867.41 | **873.07** | 0.50 GiB |

Containerizing pykci cost **+38.6 s (+4.6 %)**, attributable to the slower Docker Desktop
bind-mount sequential read of the 6.93 GB file (the parser's own RAM was unchanged at ~0.5 GiB,
confirming the host-process memory was never an advantage). **Under fully identical containerized
conditions, pykci (873.1 s) and 3DCityKG (872.2 s) map in essentially the same time** — but pykci
also produces a working spatial R-tree, whereas 3DCityKG does not (§8). 3DCityDB v5 (625.5 s)
remains fastest. This confirms the original ranking holds when the I/O path is equalized.

---

## 6. Peak RAM (working set, within the 20 GB cap)

| Tool | Component peaks | **Tool peak RAM** | Notes |
|------|-----------------|------------------:|-------|
| **3DCityDB v5** | postgres ~3.8 GiB + importer ~6.2 GiB | **~9.7 GiB** | sum of co-sampled peaks (upper bound) |
| **pykci** | Neo4j ~11.7 GiB + Python ~0.5 GiB | **~12.2 GiB** | Neo4j heap pinned at 12 GB in config |
| **3DCityKG** | single JVM (mapper + embedded Neo4j) | **~19.7 GiB** | hit the 20 GB ceiling; default mapper heap `-Xmx16g` |

3DCityKG is by far the most memory-hungry: its default mapper heap is `-Xmx16g` and the
embedded Neo4j runs in the same JVM, so it consumed essentially the entire 20 GB envelope.
3DCityDB and pykci both operated comfortably below half the budget.

---

## 7. Database footprint & element counts

**"Database only" disk** = the DB engine's data store, excluding OS files. For Neo4j this is the
graph store under `/data/databases/neo4j`; transaction logs (WAL-equivalent, transient) are
reported separately. For PostgreSQL it is `pg_database_size()` (excludes server OS files & WAL).

### pykci — Neo4j

- **Store: 11.4 GiB** (12.2 GB; 12,224,503,350 B) · transaction logs: 16.0 GB (transient, prune over time)
- **Nodes: 15,163,965 · Relationships: 21,250,423**
- Main node labels: `BoundarySurface` 4.53 M, `GeometryPolygon` 4.53 M, `GeometryRing` 4.54 M,
  `Building` 388,267, `GeometrySolid` 388,267, `GeometryLineString` 389,197, `TerrainIntersection`
  388,248 (every domain node also carries the shared `Entity` index label).
- Main relationship types: `HAS_BOUNDARY`, `HAS_SURFACE_MEMBER`, `HAS_POLYGON`,
  `HAS_EXTERIOR_RING` (4.53 M each), `PART_OF`/`HAS_LOD_SOLID`/`HAS_FUNCTION`/`HAS_ROOF_TYPE`/
  `LOCATED_IN` (388,267 each).

### 3DCityKG — embedded Neo4j

- **Store: 18.3 GiB** (19,681,939,456 B) · transaction logs: 257 MB
- **Nodes: 89,490,140 · Relationships: 94,023,669**
- Labels are **citygml4j Java class names** — the full object model is materialised: e.g.
  `SurfaceProperty` 9.46 M, `ChildList` 8.81 M, `ArrayList` 5.70 M, `DirectPositionList` 4.92 M,
  `LinearRing` 4.54 M, `MultiSurface`/`Polygon`/`Exterior` 4.53 M each, `Building` 388,267, plus
  full xAL `Address`/`Country`/`Locality`/`Thoroughfare` chains (388,267 each). An internal
  partition label `__PARTITION_INDEX__0` is carried by essentially every node.
- This is why 3DCityKG has ≈ **5.9× the nodes** and **4.4× the edges** of pykci for the same
  data: it is a verbatim 1:1 mapping of the parsed Java objects, whereas pykci uses a condensed
  domain schema.

### 3DCityDB v5 — PostgreSQL

- **Logical DB size: 10.2 GiB** (10,967,488,191 B; `pg_database_size`)
- **54 base tables** across schemas, but only **17 belong to 3DCityDB (`citydb`)**. The other 37
  are **empty** PostGIS extension scaffolding (34 `tiger` geocoder tables, 2 `topology`,
  1 `public.spatial_ref_sys`).
- **Data rows ≈ 27.0 M**, concentrated in 4 tables:

  | Table (schema `citydb`) | Rows | Total size |
  |---|---:|---:|
  | `property` | 16,420,733 | 4,043 MB |
  | `geometry_data` | 5,310,045 | 3,973 MB |
  | `feature` | 4,921,797 | 2,382 MB |
  | `address` | 388,835 | 41 MB |
  | metadata (`objectclass`, `datatype`, `codelist_entry`, `namespace`, …) | ~640 total | small |

  `feature` = 388,267 buildings + 4,533,530 boundary surfaces (each thematic surface is its own
  feature row). `property` holds feature attributes; `geometry_data` holds the geometries.

---

## 8. Spatial indexing — how much, and what kind

This was a core question: *how much of each database is devoted to spatial indexing?*

### pykci — Neo4j-spatial R-tree (in-graph) ✅

The spatial index is materialised **as graph elements** (a persistent R-tree via the Neo4j
Spatial plugin), plus native point indexes:

- **Spatial-index nodes: 7,767** = 7,766 R-tree index nodes (unlabeled) + 1 `SpatialLayer` node
  → **0.05 %** of all nodes.
- **Spatial-index edges: 396,033** = `RTREE_REFERENCE` 388,267 + `RTREE_CHILD` 7,764 +
  `RTREE_ROOT` 1 + `RTREE_METADATA` 1 → **1.86 %** of all edges.
- Plus **2 native POINT indexes** (`building_location_idx`, `cityobject_location_idx`) — index
  structures, not graph nodes/edges. (Total index objects: 19 — 15 RANGE id-indexes,
  2 POINT spatial, 2 LOOKUP.)
- Result: a **fully built, queryable R-tree** covering all 388,267 building footprints.

### 3DCityKG — in-memory R-tree persisted as graph nodes ❌ (failed)

3DCityKG builds its R-tree **in memory at runtime** and then, in a final step, stores it as
persistent (Java-typed) graph nodes. On this dataset that final transaction **failed on all
three runs**:

```
INFO  citykg.core.db.CityKGDB : |--> Storing RTrees in the database
ERROR citykg.core.db.CityKGDB : Unable to complete transaction.: The memory pool limit was exceeded …
        at citykg.core.db.CityKGDB.storeRTrees(CityKGDB.java:445)
```

The store-R-tree step commits all index nodes in **one transaction**, which exceeds Neo4j's
transaction-memory pool (the bundled `neo4j.conf` sets heap `1G` → ~700 MB tx pool). The
transaction rolled back atomically, so:

- **Spatial-index nodes persisted: 0 · edges: 0.** No R-tree labels exist in the resulting graph.
- All **35 RANGE + 2 LOOKUP** indexes are for `id`/`__UUID__`/`href` reference resolution —
  **none spatial**.
- Consequence: under default configuration, **3DCityKG produced no persistent spatial index** on
  this 388 k-feature dataset. (It would likely succeed with a larger configured Neo4j
  transaction-memory / heap, i.e. with tuning beyond defaults.)

### 3DCityDB v5 — PostGIS GIST indexes ✅

- **8 GIST (spatial) indexes** out of 133 total indexes (**6 %**), totalling **425 MB**.
- Of those 8, **only 2 index the imported data**: `citydb.feature.feature_envelope_spx`
  (feature bounding boxes) and `citydb.geometry_data.geometry_data_spx` (geometries). The other
  **6 GIST indexes are on empty `tiger` geocoder tables** (PostGIS scaffolding, unused here).
- Spatial-reference metadata: **`spatial_ref_sys` = 8,500 rows** (1 table) — the PostGIS catalog
  of coordinate reference systems, supporting all spatial operations. (`geometry_columns` /
  `geography_columns` are views, not tables.)
- Result: **2 functional GIST spatial indexes** covering feature envelopes and geometries.

### Spatial-index comparison

| Tool | Spatial index built? | Form | Size / count | Share |
|------|----------------------|------|--------------|-------|
| **pykci** | ✅ yes | In-graph Neo4j-spatial R-tree + 2 POINT idx | 7,767 nodes + 396,033 edges | 0.05 % nodes / 1.86 % edges |
| **3DCityKG** | ❌ no (failed at commit) | Would be Java-typed R-tree nodes | 0 nodes / 0 edges | 0 % |
| **3DCityDB v5** | ✅ yes | PostGIS GIST | 2 data GIST indexes, 425 MB; + 8,500-row SRS catalog | 8 / 133 indexes (6 %) |

---

## 9. Notable findings & caveats

1. **3DCityKG's spatial R-tree does not persist on large datasets under defaults** — a
   reproducible (3/3) transaction-memory failure. The graph itself is complete; only the spatial
   index is missing. Increasing the bundled Neo4j heap / transaction-memory would be required.
2. **pykci's graph is the most compact** — 15.16 M nodes / 21.25 M edges, i.e. **5.9× fewer nodes
   and 4.4× fewer edges** than 3DCityKG (≈ 94 vs ≈ 473 graph elements per building) and a 38 %
   smaller store. A condensed semantic schema (one node per domain concept) is cheaper to store,
   keeps more of the graph in page cache, and shortens query traversals.
3. **3DCityKG is the heaviest on RAM** (~19.7 GiB, at the cap) and produced the **largest store**
   (18.3 GiB) and **by far the most nodes/edges** (89.49 M / 94.02 M), because it mirrors the
   entire citygml4j Java object graph 1:1 (intermediate Java wrappers become nodes/edges).
4. **3DCityDB v5 was fastest and leanest**, with the smallest logical DB (10.2 GiB); its data
   lives in 4 populated tables (27.0 M rows) within a 17-table schema — most of its 54 base
   tables are empty PostGIS scaffolding.
5. **Counts are not cross-comparable**: a "node" (pykci vs 3DCityKG) and a "row"/"table"
   (3DCityDB) represent different granularities. Use them per-tool, not head-to-head.
6. **Neo4j transaction logs** (16 GB for pykci) are transient WAL that prune over time; they are
   excluded from the "store" figure, mirroring how `pg_database_size` excludes PostgreSQL WAL.
7. **I/O asymmetry** (native read for pykci vs bind-mount for the containerised tools) slightly
   disadvantages the containerised tools on file reads — quantified and removed by the
   containerized re-check in §5.1 (+4.6 %, ranking unchanged).

---

## 10. Final database state & reproduction

After the last run, **no database was deleted** — all three remain available for querying:

- **3DCityDB (PostgreSQL):** container `cdb` on `localhost:5432` (db `postgres`, schema `citydb`,
  user `postgres` / `changeMe`). Independent port — always available.
- **pykci (Neo4j):** project Neo4j on `bolt://localhost:7687` (`neo4j`/`neo4jneo4j`), volume
  `docker_neo4j_data`.
- **3DCityKG (Neo4j):** embedded DB in volume `citykg_out`; start a query container with
  `--entrypoint neo4j tumgis/3dcitykg:latest console` on ports 7475/7688 (auth disabled).
  > Note: pykci's and 3DCityKG's Neo4j both default to ports 7474/7687, so only **one Neo4j can
  > be served at a time** (the 3DCityKG query container is mapped to 7475/7688 to coexist).

**One-command reproduction:** [`eval/tool_comparison/run_benchmark.sh`](run_benchmark.sh) runs the
entire benchmark end-to-end (pull/build images → for each tool: empty DB → map ×3 → measure →
leave DBs live), writing a timestamped `results_<TS>/` folder with `timings.txt`, per-run logs,
memory logs, `measure_*.txt`, and a `summary.txt`. It uses the **containerized** pykci so the
whole run is pure Docker + bash:

```bash
bash eval/tool_comparison/run_benchmark.sh            # default Hamburg file
bash eval/tool_comparison/run_benchmark.sh input/citygml/other.gml   # any CityGML 2.0 file
# overrides: RUNS=3 CPUSET=0-7 MEM=20g SRID=25832
```

**Individual harness scripts** (as actually used for this report) are committed under
[`eval/tool_comparison/harness/`](harness/):
`reset_pykci.sh`, `run_pykci.ps1`, `citykg_run.conf` + `run_citykg.ps1`,
`setup_3dcitydb.ps1` + `run_3dcitydb.ps1`, and the memory pollers `mempoll.sh` / `mempoll2.sh`.
**Raw measurement outputs** are in [`eval/tool_comparison/raw/`](raw/): `timings.txt` (every
run's wall-clock) and the per-tool `measure_*.txt` (counts, indexes, disk, spatial share).
