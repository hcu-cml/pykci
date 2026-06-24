# Query Benchmark: pykci vs 3DCityKG vs 3DCityDB v5

**Date:** 2026-06-19
**Dataset:** `input/citygml/hamburg_citygml2_lod2_2025_roofmats.gml` (6.37 GB, CityGML 2.0 LoD2,
**388,267 buildings**, of which **194,799 (50.2 %)** carry an ML-predicted roof material).
**Task:** answer **20 natural-language questions** (10 *citizen*, 10 *expert* — see
[`queries_citizen.txt`](queries_citizen.txt) / [`queries_expert.txt`](queries_expert.txt)) on
**each** of the three databases, and record — per query, per database — whether Claude could
solve it, how many tries it took, how hard it was, what the query looks like, whether the
result is correct, and how fast it ran.

The same dataset was ingested into each tool **separately** (one DB online at a time — pykci and
3DCityKG share Neo4j ports). The exact queries are in [`run_pykci.sh`](run_pykci.sh),
[`run_citykg.sh`](run_citykg.sh), [`run_3dcitydb.sh`](run_3dcitydb.sh); raw outputs + per-query
timings are in `results_pykci.txt`, `results_citykg.txt`, `results_3dcitydb.txt`.

## Database container versions (benchmark run **2026-06-19**)

| Tool | Docker image (tag) | Image digest / built | DB engine version |
|------|--------------------|----------------------|-------------------|
| **pykci** | project image `docker-neo4j`, built from [`docker/Dockerfile.neo4j`](../../../docker/Dockerfile.neo4j) on base **`neo4j:2025.10.1`** (+ APOC, Spatial, GDS plugins) | base `neo4j:2025.10.1` | **Neo4j 2025.10.1** (Cypher 5/25) |
| **3DCityKG** | **`tumgis/3dcitykg:latest`** | `sha256:f41ba89f0843…` (image built 2025-10-27) | bundled **Neo4j 5.26.14** + APOC (embedded) |
| **3DCityDB** (store) | **`3dcitydb/3dcitydb-pg:latest`** | `sha256:4f439a5d0bc3…` (image built 2026-03-06) | **PostgreSQL 18.3 / PostGIS 3.6.2**; 3DCityDB schema **v5.1.3** |
| **3DCityDB** (importer) | **`3dcitydb/citydb-tool:latest`** | `sha256:c2e367dae1eb…` (image built 2026-05-31) | **citydb-tool v1.3.2** |

Query clients: `cypher-shell` (Neo4j tools) and `psql` (3DCityDB), each invoked via `docker exec`.
Host: Docker 28.5.1 on Windows 11 / WSL2 (see [`../comparison_2026-06-19.md`](../comparison_2026-06-19.md) §2 for full host specs).

---

## 1. Headline result

**All three databases returned identical, correct answers to all 18 answerable questions.** The
data — not the tool — makes 2 of the 20 questions unanswerable (see §3). What differs sharply is
**how hard it was for me (Claude) to author a correct query**, **how verbose** the query is, and
**how fast** it runs.

| | **pykci** (Neo4j) | **3DCityKG** (Neo4j) | **3DCityDB v5** (PostGIS) |
|---|---|---|---|
| Language | Cypher | Cypher | SQL |
| Queries correct (of 18) | **18 ✓** | **18 ✓** | **18 ✓** |
| Final-query first-try success | 16/18 | 18/18 | 18/18 |
| **Schema-discovery queries needed first** | **~2** | ~7 | ~3 |
| Avg authoring difficulty (1–5) | 2.1 | 3.9 | **2.0** |
| Avg query length | **~290 chars** | ~460 chars (max ~1060) | ~230 chars **+ ~745-char shared CTE** |
| Avg query wall-clock | 1.82 s | 3.55 s | 2.82 s |
| − fixed client startup | ~1.45 s | ~1.45 s | ~0.20 s |
| **= net DB query time** | **~0.37 s** | **~2.10 s** | **~2.62 s** |
| Spatial support used | in-graph R-tree / coords | **none** (coords reconstructed) | **PostGIS GIST + geometry** |

**Bottom line for an LLM-driven query layer:** pykci's condensed domain schema was by far the
easiest and most concise to query (a building's height, roof type, material, storeys, and
location are direct node properties). 3DCityDB was a close second — standard SQL with a clean
PostGIS `envelope` geometry, once a property-pivot CTE is built. 3DCityKG was dramatically harder:
its 1:1 citygml4j object graph forced multi-hop traversals for every attribute and a 9-hop path
just to read a coordinate, requiring extensive reverse-engineering before a single question could
be answered.

---

## 2. How "difficulty" and "tries" were scored

- **Tries** = number of times the *final* query was executed before it returned a correct result
  (1 = worked on first execution). This does **not** count the exploratory *schema-discovery*
  queries run beforehand — those are reported separately because they dominate the real effort.
- **Schema-discovery queries** = exploratory queries needed to learn where/how each attribute is
  stored before the benchmark queries could be written.
- **Authoring difficulty (1–5)**: 1 = single property + simple aggregate; 2 = filter/group/CASE;
  3 = multi-step (grid math, cross-tab, composite score); 4 = needs mode/dominant or one deep
  traversal; 5 = combines deep traversal(s) + aggregation + reconstructed coordinates.
- **Correctness**: cross-validated across all three tools (they must agree) and against the
  source-data distributions; pykci's domain schema served as the reference.
- **Exec time**: wall-clock of the CLI call (`cypher-shell` / `psql`), so it includes a fixed
  client-startup overhead. That overhead was measured directly (§6.1) and is **subtracted** to
  report the **net DB query time** — the engine-to-engine figure of interest. No query was
  specially indexed or warmed beyond each tool's defaults.

---

## 3. Data-feasibility findings (apply to all three tools equally)

Two questions cannot be answered from this dataset — a property of the **data**, correctly
detected on every tool, *not* a tool weakness:

- **Citizen Q9 "buildings with mixed roof materials"** — every building has **exactly one**
  `PredictedRoofMaterial` value (verified: max materials per building = 1 on all three). "Mixed"
  is not representable.
- **Expert Q7 "roof-material coverage completeness (`material_cov`)"** — the data has **no**
  per-material coverage field; the only roof attribute is the single `PredictedRoofMaterial`
  string (5 classes: `roof_tiles`, `tar_paper`, `concrete`, `metal`, `glass`). Verified: 0
  attributes matching `*cov*` on all three.

Each tool answered these as *feasibility checks* (returning the evidence of infeasibility) rather
than fabricating an answer. The remaining **18 questions are fully answerable** everywhere.

---

## 4. Per-database experience

### 4.1 pykci (Neo4j, condensed domain schema) — easiest

Every building attribute is a **direct property** on the `Building` node: `measured_height`,
`storeys_above_ground`, `roof_type_code`, `function_code`, `ground_z`, `center_x/center_y`,
`bbox_*`, the generic attribute `predictedroofmaterial`, and an **auto-generated `description`**
(`"Detached House, 9.6 m tall, 2 storeys, Gable Roof roof, at (x, y)"`) that even carries the
human-readable building type. Schema discovery was ~2 queries (list keys, value distributions).

- 16/18 queries worked on the first execution.
- **C2** (material share) needed a 2nd try — Neo4j's *implicit grouping* rejects a carried scalar
  (`tot`) next to `count(*)`; fixed by pre-aggregating into a list.
- **E2** (dominant roof material per 1 km cell) needed a 2nd try — a genuine **logic bug** I
  caught by sanity-checking: `apoc.coll.sortMaps` sorts **descending**, so my `[-1]` picked the
  *least* common material ("glass" everywhere). Fixed to `[0]`. (This is the value of verifying
  results, not just running them.)
- Fastest queries (avg 1.82 s, ~0.4 s once the ~1.4 s cypher-shell JVM start is removed) and the
  most concise/readable Cypher.

### 4.2 3DCityKG (Neo4j, verbatim citygml4j object graph) — hardest

The graph is a 1:1 image of the parsed Java objects (89.5 M nodes). Nothing is a simple property:

- Node label is the full class name `` `org.citygml4j.model.citygml.building.Building` ``.
- `measuredHeight` → `-[:measuredHeight]->(Length{value})`; `roofType` → `-[:roofType]->(Code{value})`.
- `function` → `-[:function]->(ChildList)-[:elementData]->(__ARRAY__)-[:ARRAY_MEMBER]->(Code{value})`.
- `PredictedRoofMaterial` → `-[:genericAttribute]->(ChildList)-[*]->(StringAttribute{name,value})`.
- **Coordinates** (no `center`/`ground_z` exist) → a **9-hop** path
  `Building→boundedBy→envelope→lowerCorner→value→elementData→__ARRAY__`, where x/y/z are the
  node properties `` `ARRAY_MEMBER[0/1/2]` ``.
- Every value is a **string** → `toFloat`/`toInteger` everywhere; there is **no `description`**,
  so building "use type" had to come from the raw function code.

This required **~7 schema-discovery queries** to reverse-engineer before any benchmark query could
be written. Once the traversal paths were known, all 18 ran correctly on the first execution — but
the authoring burden is the highest by far, and queries are the slowest (avg 3.55 s, up to 5.9 s
for the envelope+generic-attribute grid aggregations). The R-tree did **not** persist (same
transaction-memory failure as the mapping benchmark), so no spatial index was available; the
spatial queries still worked by reconstructing coordinates from the envelope.

### 4.3 3DCityDB v5 (PostgreSQL/PostGIS, relational) — middle

Attributes are rows in `citydb.property` (`function`/`roofType`/`PredictedRoofMaterial` →
`val_string`, `storeysAboveGround` → `val_int`, measured height → a nested `value` row with
`val_double` + uom). The key enabler: **`feature.envelope` is a real PostGIS geometry** (GIST-
indexed), so all spatial/grid/elevation work is trivial PostGIS (`ST_Centroid`, `ST_ZMin`,
`ST_XMin/Max`). Schema discovery was ~3 queries (building `objectclass_id=901`; property
name/value columns; the height-as-nested-`value` nuance).

I built one reusable `bld` CTE that pivots the per-building properties (`max(...) FILTER (WHERE
name=...)`) and exposes envelope-derived `cx/cy/ground_z/dx/dy`. With that, all 18 queries were
straightforward and worked first-try; SQL's `mode() WITHIN GROUP` (dominant material, E2),
window functions (share %, C2), `FILTER` (heat-island, E6) and `array_agg` (cluster samples, E8)
made the expert queries notably cleaner than the Cypher equivalents. The CTE re-pivots the
property table on every query (inlined deliberately — no precomputed view — so the timing is
honest), giving avg 2.82 s.

### 4.4 The two pykci retries (C2, E2): a *loud* error vs. a *silent* wrong answer

pykci's only two non-first-try queries failed for fundamentally different reasons — a
distinction that matters for any LLM-driven querying layer.

**C2 — "most common roof material + %": rejected by the database (loud).** The first attempt
carried the total count `tot` as a scalar and reused it beside `count(*)` in the same `RETURN`:

```cypher
WITH collect(b.predictedroofmaterial) AS mats, count(*) AS tot
UNWIND mats AS m
RETURN m AS material, count(*) AS n, round(100.0*count(*)/tot,1) AS pct   -- rejected
```

Neo4j returned `42I18: invalid reference to implicitly grouped expressions … tot`. In Cypher,
when a clause mixes an aggregate (`count(*)`) with non-aggregated terms, the non-aggregated
terms become the *implicit grouping keys*; here the only key is `m`, so `tot` (neither a key nor
inside an aggregate) is illegal. The fix pre-aggregates per material, takes the total over the
collected rows, then divides — so the final `RETURN` contains no aggregate:

```cypher
WITH b.predictedroofmaterial AS m, count(*) AS n
WITH collect({m:m,n:n}) AS rows, sum(n) AS tot
UNWIND rows AS r RETURN r.m AS material, r.n AS n, round(100.0*r.n/tot,1) AS pct ORDER BY n DESC;
```

(SQL needs no such rewrite — `sum(count(*)) OVER ()` does it in one pass, which is why 3DCityDB's
C2 was first-try.)

**E2 — "1 km grid + dominant material": ran fine but was silently wrong.** The first attempt
executed with no error yet reported `glass` as the dominant material in *every* cell — impossible,
since glass is only 1.7 % of roofs. The cause: `apoc.coll.sortMaps(freq,'count')` sorts
**descending** by default, so taking `[-1]` selected the *least* frequent material rather than the
most frequent. The fix is `[0]` (→ `roof_tiles`, correct). This bug was caught **only by sanity-
checking the result**, not by execution success.

**Takeaway:** C2 failed *loudly* (the engine refused it), E2 failed *silently* (it returned a
plausible-looking table). Only result-plausibility verification catches the E2 class of error —
the basis for the recommendation in §9.5.

---

## 5. Master results table

Difficulty (D, 1–5) and Tries (T) are for query **authoring/execution**; ✓ = result correct
(cross-validated). "—" for C9/E7 = data-infeasible (correctly detected everywhere).

| # | Question | pykci D/T | 3DCityKG D/T | 3DCityDB D/T | Correct (all) | Answer |
|---|----------|:--------:|:-----------:|:-----------:|:-------------:|--------|
| C1 | City at a glance (count, avg h, avg storeys) | 1 / 1 | 3 / 1 | 1 / 1 | ✓ | 388,267 · 7.7 m · 1.7 |
| C2 | Most common roof material + % | 2 / **2** | 4 / 1 | 2 / 1 | ✓ | roof_tiles 55.8 % |
| C3 | 20 tallest buildings | 1 / 1 | 3 / 1 | 1 / 1 | ✓ | 158.5 m top |
| C4 | Roof-shape popularity | 2 / 1 | 3 / 1 | 2 / 1 | ✓ | Flat 169,964 · Gable 162,368 |
| C5 | Flat roofs > 5 m (solar) | 1 / 1 | 3 / 1 | 1 / 1 | ✓ | 70,162 |
| C6 | Buildings per storey count | 1 / 1 | 2 / 1 | 1 / 1 | ✓ | 1-storey 244,389 |
| C7 | Glass roofs | 1 / 1 | 4 / 1 | 1 / 1 | ✓ | 3,232 |
| C8 | High-rises > 50 m | 1 / 1 | 3 / 1 | 1 / 1 | ✓ | 149 |
| C9 | **Mixed roof materials** | — | — | — | n/a | **infeasible (1 material/bldg)** |
| C10 | HafenCity bbox | 2 / 1 | 5 / 1 | 2 / 1 | ✓ | 783 bldgs · 24.2 m avg |
| E1 | Typology by use type | 2 / 1 | 4 / 1 | 2 / 1 | ✓ | Detached House 207,236 |
| E2 | 1 km grid + dominant material | 4 / **2** | 5 / 1 | 3 / 1 | ✓ | roof_tiles dominant |
| E3 | Solar score 0–7 | 3 / 1 | 5 / 1 | 3 / 1 | ✓ | many score 7 |
| E4 | Floor-to-floor anomaly | 2 / 1 | 3 / 1 | 2 / 1 | ✓ | 68,545 |
| E5 | Material × use type cross-tab | 3 / 1 | 5 / 1 | 3 / 1 | ✓ | consistent |
| E6 | Heat-island dark-roof / 2 km | 3 / 1 | 5 / 1 | 3 / 1 | ✓ | top cell 72.1 % |
| E7 | **Material coverage (`material_cov`)** | — | — | — | n/a | **infeasible (no field)** |
| E8 | High-rise clusters / 500 m | 3 / 1 | 5 / 1 | 3 / 1 | ✓ | top cell 48 tall |
| E9 | FAR proxy / 1 km | 3 / 1 | 5 / 1 | 3 / 1 | ✓ | HafenCity FAR 4.6 |
| E10 | Ground-elevation zones | 2 / 1 | 4 / 1 | 2 / 1 | ✓ | −5 m … 88.7 m |

*Minor cross-tool deltas (not errors):* E9's auxiliary building **count** is 484 on 3DCityKG vs
454 on pykci/3DCityDB (3DCityKG's formulation also counts null-storey buildings; the FAR value is
identical). E10's "below sea level" bucket is 600 on 3DCityDB vs 603 on the others (envelope
`ST_ZMin` vs stored lower-corner z differ for 3 buildings exactly at 0 m).

---

## 6. KPI summary

| KPI | pykci | 3DCityKG | 3DCityDB v5 |
|---|--:|--:|--:|
| Answerable questions solved correctly | 18 / 18 | 18 / 18 | 18 / 18 |
| Final-query first-try rate | 89 % (16/18) | 100 % | 100 % |
| Schema-discovery queries before authoring | ~2 | ~7 | ~3 |
| Mean authoring difficulty (1–5) | 2.1 | 3.9 | 2.0 |
| Max authoring difficulty | 4 | 5 | 3 |
| Mean query **wall-clock** | 1.82 s | 3.55 s | 2.82 s |
| − fixed client startup (measured, §6.1) | ~1.45 s | ~1.45 s | ~0.20 s |
| **= mean net DB query time** | **~0.37 s** | **~2.10 s** | **~2.62 s** |
| Max wall-clock / max net DB time | 2.43 / ~1.0 s | 5.89 / ~4.4 s | 4.23 / ~4.0 s |
| Total wall-clock, 20 queries | 41.8 s | 71.1 s | 56.3 s |
| Mean query length (self-contained) | ~290 chars | ~460 chars | ~230 + ~745 (CTE) |
| Needed external lib / special feature | APOC (`frequencies`, `sortMaps`) | APOC + bracket-property access | `mode() WITHIN GROUP`, window fns, PostGIS |
| Spatial index available at query time | yes (R-tree) | **no** (R-tree didn't persist) | yes (GIST) |

### 6.1 Fixed client startup — measured, and why it differs

Each benchmark query was run as a **fresh CLI process** (`docker exec … cypher-shell` /
`docker exec … psql`), so its wall-clock includes a constant per-launch overhead. Timing a
trivial query (`RETURN 1` / `SELECT 1` — essentially *all* startup, no work), 3 runs each:

| Step | Neo4j (`cypher-shell`) | 3DCityDB (`psql`) |
|---|--:|--:|
| `docker exec` plumbing alone (`true`) | 0.20 s | 0.20 s |
| trivial-query wall-clock (steady state) | ~1.45 s (first call 2.9 s cold) | ~0.20 s |
| ⇒ **client startup on top of exec** | **~1.25 s** | **~0.00 s** |

**Why ~1.25 s for the graph DBs but ~0 for the relational DB — it is the query client, not the
engine.** `cypher-shell` is a **Java application**: every invocation cold-starts a JVM
(JRE load, class-loading, JIT) and opens + authenticates a **Bolt** session. `psql` is a tiny
**native C** client (libpq) that starts in milliseconds and connects over a local socket, so its
overhead is essentially just the `docker exec` plumbing. This tax is therefore **identical for
both Neo4j tools** (both use `cypher-shell`) and cancels in the graph-vs-graph comparison.

**It is a harness artifact, not a database property.** A real application uses a **persistent
driver / connection pool** — a kept-open Bolt session (Neo4j) or a JDBC/psycopg pool (PostgreSQL)
— where this startup is paid **once at connect**, not per query, and per-query overhead drops to
≈ 0. Conversely, querying 3DCityDB through a Java/JDBC client would incur a similar JVM start.
Hence the net-DB-time rows above are the fair engine-to-engine comparison: **pykci ~0.37 s
(fastest), 3DCityKG ~2.10 s (deep traversals over 89 M nodes), 3DCityDB ~2.62 s (the per-query
property-pivot CTE over the `property` table)** — note 3DCityDB's *net* cost is actually the
highest, despite a middling wall-clock, because the normalized schema must be re-pivoted each run.

---

## 7. Same question, three languages (C2 — "most common roof material + %")

**pykci** — direct property, 1 line of intent:
```cypher
MATCH (b:Building) WHERE b.predictedroofmaterial IS NOT NULL
WITH b.predictedroofmaterial AS m, count(*) AS n
WITH collect({m:m,n:n}) AS rows, sum(n) AS tot
UNWIND rows AS r RETURN r.m AS material, r.n AS n, round(100.0*r.n/tot,1) AS pct ORDER BY n DESC;
```

**3DCityKG** — 3-hop traversal into the generic-attribute object graph:
```cypher
MATCH (b:`org.citygml4j.model.citygml.building.Building`)-[:genericAttribute]->()-[*1..3]->
      (sa:`org.citygml4j.model.citygml.generics.StringAttribute`)
WHERE sa.name='PredictedRoofMaterial'
WITH sa.value AS m, count(*) AS n WITH collect({m:m,n:n}) AS rows, sum(n) AS tot
UNWIND rows AS r RETURN r.m AS material, r.n AS n, round(100.0*r.n/tot,1) AS pct ORDER BY n DESC;
```

**3DCityDB** — property pivot CTE + a window function:
```sql
WITH bld AS (SELECT f.id, max(p.val_string) FILTER (WHERE p.name='PredictedRoofMaterial') AS material
             FROM citydb.feature f JOIN citydb.property p ON p.feature_id=f.id
             WHERE f.objectclass_id=901 GROUP BY f.id)
SELECT material, count(*) AS n, round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct
FROM bld WHERE material IS NOT NULL GROUP BY material ORDER BY n DESC;
```
All three return: roof_tiles 55.8 %, tar_paper 26.4 %, concrete 11.8 %, metal 4.4 %, glass 1.7 %.

---

## 8. Conclusions

1. **Correctness was never the differentiator** — all three databases hold the same information
   and returned the same answers. The dataset (single roof material, no coverage) capped what any
   tool could answer at 18/20.
2. **pykci is the most query-friendly**, especially for an LLM→Cypher layer: domain-level labels
   and properties (`Building.measured_height`, `.predictedroofmaterial`, `.roof_type_code`) plus a
   ready-made human-readable `description` mean the shortest, most legible queries, the fewest
   schema-discovery queries, and the fastest net query time. Its per-query authoring difficulty
   (2.1) is on par with 3DCityDB (2.0) — SQL's `mode()`/window functions make some expert queries
   as clean as Cypher — but both are far below 3DCityKG (3.9). pykci's only friction was two
   Cypher/APOC idioms (implicit grouping; descending sort).
3. **3DCityDB is a strong relational option**: once the property-pivot CTE is written, SQL's
   `mode()`, window functions and PostGIS geometry made even the expert spatial/statistical
   queries clean and first-try correct. Discovering the property/value layout (and that height is
   a nested `value` row) is the main upfront cost.
4. **3DCityKG is the hardest to query**: the verbatim citygml4j object graph turns every attribute
   into a multi-hop traversal, every value into a string, and every coordinate into a 9-hop path
   with bracketed array-member properties — and offers no `description` and (here) no working
   spatial index. It is faithful to CityGML's structure but ill-suited to ad-hoc or
   LLM-generated querying without a substantial schema-abstraction layer.
5. **Verification matters**: the one silent correctness bug (pykci E2 dominant-material) passed
   syntactically and only surfaced via a sanity check ("glass can't be dominant in every cell").
   Any LLM querying layer should validate result plausibility, not just successful execution.

---

## 9. Reproduction

```bash
# Each tool, one at a time (pykci & 3DCityKG share Neo4j ports):
#  pykci:    ingest roofmats -> project Neo4j, then:
bash eval/tool_comparison/queries/run_pykci.sh        # -> results_pykci.txt
#  3DCityKG: map roofmats (case=0) -> embedded Neo4j volume, start query container, then:
bash eval/tool_comparison/queries/run_citykg.sh       # -> results_citykg.txt
#  3DCityDB: import roofmats -> postgres 'cdb', then:
bash eval/tool_comparison/queries/run_3dcitydb.sh     # -> results_3dcitydb.txt
```
The question prompts are in `queries_citizen.txt` / `queries_expert.txt`; the authored queries are
the runner scripts; raw answers + per-query timings are the `results_*.txt` files.
