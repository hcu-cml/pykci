#!/usr/bin/env bash
###############################################################################
# CityGML -> Database mapping benchmark  (single-script, reproducible)
#
# Compares the mapping time + resulting database of three tools on ONE CityGML
# 2.0 file, each run from an EMPTY database, repeated 3x and averaged, under an
# IDENTICAL enforced resource envelope:  8 logical CPUs (cpuset 0-7) + 20 GB RAM
# per component (whether a tool uses it all is up to the tool).
#
#   * pykci (\mytoolname) -> Neo4j           (containerized: docker/Dockerfile.ingest)
#   * 3DCityKG            -> Neo4j (embedded) (tumgis/3dcitykg, case=0 map-only)
#   * 3DCityDB v5         -> PostgreSQL/PostGIS (3dcitydb/3dcitydb-pg + citydb-tool)
#
# After the runs it measures each DB (disk size, node/edge or table/row counts,
# spatial-index share). Databases are NOT deleted afterwards (left queryable).
#
# This reproduces eval/tool_comparison/comparison_2026-06-19.md.
#
# USAGE (from anywhere):
#   bash eval/tool_comparison/run_benchmark.sh [path/to/file.gml]
# Env overrides: RUNS=3  CPUSET=0-7  MEM=20g  SRID=25832
#
# REQUIREMENTS: Docker (Desktop on Windows/WSL2, or native Linux) + bash.
# NOTE: pykci's Neo4j and 3DCityKG's bundled Neo4j both use ports 7474/7687, so
#       the script runs the tools sequentially and stops one Neo4j before the
#       other. PostgreSQL (5432) is independent.
###############################################################################
set -uo pipefail
export MSYS_NO_PATHCONV=1            # stop Git Bash from mangling in-container paths

# ----------------------------------------------------------------- config ---
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GML_REL="${1:-input/citygml/hamburg_citygml2_lod2_2025.gml}"   # relative to repo root, under input/
RUNS="${RUNS:-3}"
CPUSET="${CPUSET:-0-7}"
MEM="${MEM:-20g}"
SRID="${SRID:-25832}"
NEO4J_PASS="neo4jneo4j"
GML_SUB="${GML_REL#input/}"                                    # path under input/
GML_NAME="$(basename "$GML_REL")"

# Host path helper: docker -v needs Windows-style paths on Docker Desktop.
hp(){ if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1" | tr '\\' '/'; else echo "$1"; fi; }

TS="$(date +%Y%m%d_%H%M%S)"
RESULTS="$REPO/eval/tool_comparison/results_$TS"
mkdir -p "$RESULTS"
TIM="$RESULTS/timings.txt"; : > "$TIM"
SUMMARY="$RESULTS/summary.txt"

log(){ echo; echo "################ $* ################"; }
avg(){ awk '{s+=$1;n++} END{if(n)printf "%.2f",s/n}'; }
# max working-set (GiB) from a memlog whose lines contain "<container>=<X.YGiB|ZMiB>"
maxmem(){ awk -v key="$1" '{ for(i=1;i<=NF;i++){ if($i ~ "^"key"="){ v=$i; sub("^"key"=","",v);
  g=0; if(v ~ /GiB$/){sub(/GiB$/,"",v);g=v+0} else if(v ~ /MiB$/){sub(/MiB$/,"",v);g=(v+0)/1024} else if(v ~ /GB$/){sub(/GB$/,"",v);g=(v+0)*1000/1024}
  if(g>m)m=g } } } END{printf "%.2f",m}' "$2"; }

# Poll working-set memory of one or more containers every 2s until the FIRST
# (sentinel) container disappears.  Args: <outfile> <sentinel> [more containers...]
poll_until(){ local out="$1" sentinel="$2"; shift 2; local conts="$sentinel $*"; : > "$out"
  local i; for i in $(seq 1 180); do docker ps --format '{{.Names}}'|grep -q "^${sentinel}$" && break; sleep 1; done
  while docker ps --format '{{.Names}}'|grep -q "^${sentinel}$"; do local line="$(date +%s)" c u
    for c in $conts; do u=$(docker stats --no-stream --format '{{.MemUsage}}' "$c" 2>/dev/null | sed 's#/.*##' | tr -d ' '); line="$line ${c}=${u:-NA}"; done
    echo "$line" >> "$out"; sleep 2
  done; }

cs(){ docker exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASS" --format plain "$1" 2>/dev/null; }

# -------------------------------------------------------------- preflight ---
log "PREFLIGHT"
[ -f "$REPO/$GML_REL" ] || { echo "ERROR: GML not found at $REPO/$GML_REL"; exit 1; }
docker version >/dev/null 2>&1 || { echo "ERROR: Docker not available"; exit 1; }
echo "repo            : $REPO"
echo "gml             : $GML_REL  ($(wc -c < "$REPO/$GML_REL") bytes)"
echo "runs/tool       : $RUNS"
echo "fair envelope   : cpuset=$CPUSET  memory=$MEM per component"
echo "results dir     : $RESULTS"
{
  echo "# Benchmark environment  ($TS)"
  echo "gml: $GML_REL  bytes=$(wc -c < "$REPO/$GML_REL")"
  echo "srid: $SRID  envelope: cpuset=$CPUSET mem=$MEM  runs=$RUNS"
  echo "docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  echo "docker mem: $(docker info --format '{{.MemTotal}}' 2>/dev/null)"
  uname -a 2>/dev/null
} > "$RESULTS/env.txt"

# ---------------------------------------------------------------- images ---
log "PULL / BUILD IMAGES"
docker pull tumgis/3dcitykg:latest
docker pull 3dcitydb/3dcitydb-pg
docker pull 3dcitydb/citydb-tool
docker build -f "$REPO/docker/Dockerfile.ingest" -t pykci-ingest "$(hp "$REPO")"

# ---- neo4j reset to EMPTY + constrain to the fair envelope -----------------
reset_neo4j(){
  bash "$REPO/eval/reset_neo4j.sh"
  docker update --cpuset-cpus "$CPUSET" --memory "$MEM" --memory-swap "$MEM" neo4j >/dev/null 2>&1
  docker restart neo4j >/dev/null            # restart so the JVM detects the cgroup cap
  local i; for i in $(seq 1 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' neo4j 2>/dev/null)" = healthy ] && break; sleep 3; done
  sleep 3
}

###############################################################################
log "TOOL 1/3 — pykci (containerized)  ->  Neo4j"
###############################################################################
for r in $(seq 1 "$RUNS"); do
  echo ">> pykci run $r/$RUNS : reset empty neo4j, then timed ingest"
  reset_neo4j
  poll_until "$RESULTS/pykci_run${r}.mem" pykci_ingest neo4j &
  POLL=$!
  docker rm -f pykci_ingest >/dev/null 2>&1
  t0=$(date +%s.%N)
  docker run --rm --name pykci_ingest --network container:neo4j \
    --cpuset-cpus "$CPUSET" --memory "$MEM" --memory-swap "$MEM" \
    -v "$(hp "$REPO/input"):/input:ro" \
    pykci-ingest "/input/$GML_SUB" > "$RESULTS/pykci_run${r}.log" 2>&1
  rc=$?; t1=$(date +%s.%N)
  kill "$POLL" 2>/dev/null; wait "$POLL" 2>/dev/null
  el=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
  echo "pykci|run$r|elapsed_s=$el|rc=$rc|parser_peak_gib=$(maxmem pykci_ingest "$RESULTS/pykci_run${r}.mem")|neo4j_peak_gib=$(maxmem neo4j "$RESULTS/pykci_run${r}.mem")" | tee -a "$TIM"
done

log "MEASURE pykci Neo4j (last run is kept)"
{
  echo "## pykci (Neo4j)"
  echo "nodes:";  cs "MATCH (n) RETURN count(n) AS nodes;"
  echo "edges:";  cs "MATCH ()-[r]->() RETURN count(r) AS edges;"
  echo "spatial-index nodes (RTree internal + SpatialLayer):"; cs "MATCH (n) WHERE size(labels(n))=0 OR n:SpatialLayer RETURN count(n) AS spatial_nodes;"
  echo "spatial-index edges (RTREE_*):"; cs "MATCH ()-[r:RTREE_ROOT|RTREE_CHILD|RTREE_REFERENCE|RTREE_METADATA]->() RETURN count(r) AS spatial_edges;"
  echo "spatial (POINT) indexes:"; cs "SHOW INDEXES YIELD type WHERE type='POINT' RETURN count(*) AS point_indexes;"
  echo "store + tx-log disk:"; docker exec neo4j sh -c 'du -sh /data/databases/neo4j /data/transactions/neo4j' 2>/dev/null
} | tee "$RESULTS/measure_pykci.txt"

echo ">> stopping project Neo4j to free ports 7474/7687 for 3DCityKG (volume preserved)"
( cd "$REPO/docker" && docker compose stop neo4j >/dev/null 2>&1 )

###############################################################################
log "TOOL 2/3 — 3DCityKG  ->  embedded Neo4j  (case=0, map-only)"
###############################################################################
RUNCONF="$RESULTS/citykg_run.conf"
cat > "$RUNCONF" <<EOF
case = 0
db.path = "output/neo4jDB"
db.name = "neo4j"
db.batch.size = 1000
mapper.dataset.paths = [ "$GML_REL" ]
mapper.exclude.vertex.classes = []
mapper.exclude.edge.types = [ "this\$0", "associableClass", "cityGMLClass", "gMLClass", "module", "parent" ]
mapper.concurrent.timeout = 360000
mapper.toplevel.batch.size = 10
neo4j.config.file = "config/neo4j.conf"
neo4j.plugin.path = "plugins"
neo4j.rtree.store = true
neo4j.rtree.img.path = "output/img"
citygml.version = "v2_0"
cityjson = false
EOF
for r in $(seq 1 "$RUNS"); do
  echo ">> 3DCityKG run $r/$RUNS : fresh empty embedded DB, then timed map"
  docker rm -f citykg_map >/dev/null 2>&1
  docker volume rm citykg_out >/dev/null 2>&1; docker volume create citykg_out >/dev/null
  poll_until "$RESULTS/citykg_run${r}.mem" citykg_map &
  POLL=$!
  t0=$(date +%s.%N)
  docker run --rm --name citykg_map \
    --cpuset-cpus "$CPUSET" --memory "$MEM" --memory-swap "$MEM" \
    -v "$(hp "$RUNCONF"):/home/gradle/src/3dcitykg/config/run.conf:ro" \
    -v "$(hp "$REPO/input"):/home/gradle/src/3dcitykg/input:ro" \
    -v "citykg_out:/home/gradle/src/3dcitykg/output" \
    tumgis/3dcitykg:latest sh -c 'gradle run --no-daemon' > "$RESULTS/citykg_run${r}.log" 2>&1
  rc=$?; t1=$(date +%s.%N)
  kill "$POLL" 2>/dev/null; wait "$POLL" 2>/dev/null
  el=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
  rtree=$(grep -c "memory pool limit was exceeded" "$RESULTS/citykg_run${r}.log" 2>/dev/null)
  echo "3dcitykg|run$r|elapsed_s=$el|rc=$rc|peak_gib=$(maxmem citykg_map "$RESULTS/citykg_run${r}.mem")|rtree_persist_errors=$rtree" | tee -a "$TIM"
done

log "MEASURE 3DCityKG Neo4j (query container on kept volume, ports 7475/7688)"
docker rm -f citykg_query >/dev/null 2>&1
docker run -d --name citykg_query --cpuset-cpus "$CPUSET" --memory 4g \
  -p 7475:7474 -p 7688:7687 -v citykg_out:/home/gradle/src/3dcitykg/output \
  --entrypoint neo4j tumgis/3dcitykg:latest console >/dev/null
for i in $(seq 1 45); do docker exec citykg_query cypher-shell --format plain "RETURN 1;" >/dev/null 2>&1 && break; sleep 2; done
ckg(){ docker exec citykg_query cypher-shell --format plain "$1" 2>/dev/null; }
{
  echo "## 3DCityKG (embedded Neo4j)"
  echo "nodes:"; ckg "MATCH (n) RETURN count(n) AS nodes;"
  echo "edges:"; ckg "MATCH ()-[r]->() RETURN count(r) AS edges;"
  echo "spatial (RTree) node labels present:"; ckg "MATCH (n) UNWIND labels(n) AS l WITH l WHERE toLower(l) CONTAINS 'rtree' RETURN l, count(*) AS n;"
  echo "(none above => R-tree did not persist; see rtree_persist_errors in timings.txt)"
  echo "store + tx-log disk:"; docker run --rm -v citykg_out:/out alpine du -sh /out/neo4jDB/data/databases/neo4j /out/neo4jDB/data/transactions/neo4j 2>/dev/null
} | tee "$RESULTS/measure_citykg.txt"

###############################################################################
log "TOOL 3/3 — 3DCityDB v5  ->  PostgreSQL/PostGIS"
###############################################################################
docker network create cdbnet >/dev/null 2>&1 || true
setup_cdb(){
  docker rm -f cdb cdb_import >/dev/null 2>&1
  docker volume rm cdb_data >/dev/null 2>&1; docker volume create cdb_data >/dev/null
  docker run -d --name cdb --network cdbnet --cpuset-cpus "$CPUSET" --memory "$MEM" --memory-swap "$MEM" \
    -p 5432:5432 -e POSTGRES_PASSWORD=changeMe -e SRID="$SRID" \
    -v "cdb_data:/var/lib/postgresql" 3dcitydb/3dcitydb-pg >/dev/null
  # wait for the FINAL server (init scripts done): 2nd "ready to accept connections"
  local i logs ready
  for i in $(seq 1 120); do logs="$(docker logs cdb 2>&1)"
    ready="$(echo "$logs" | grep -c 'ready to accept connections')"
    echo "$logs" | grep -q 'PostgreSQL init process complete' && [ "$ready" -ge 2 ] && break
    sleep 2
  done
  # confirm TCP reachability over the network (same path the importer uses)
  for i in $(seq 1 30); do docker run --rm --network cdbnet 3dcitydb/3dcitydb-pg pg_isready -h cdb -p 5432 -U postgres >/dev/null 2>&1 && break; sleep 2; done
}
for r in $(seq 1 "$RUNS"); do
  echo ">> 3DCityDB run $r/$RUNS : fresh empty postgres, then timed import"
  setup_cdb
  poll_until "$RESULTS/cdb_run${r}.mem" cdb_import cdb &
  POLL=$!
  docker rm -f cdb_import >/dev/null 2>&1
  t0=$(date +%s.%N)
  docker run -i --rm --name cdb_import --network cdbnet \
    --cpuset-cpus "$CPUSET" --memory "$MEM" --memory-swap "$MEM" \
    -v "$(hp "$REPO/input"):/input:ro" \
    3dcitydb/citydb-tool import citygml \
      -H cdb -P 5432 -d postgres -S citydb -u postgres --db-password=changeMe \
      "/input/$GML_SUB" > "$RESULTS/3dcitydb_run${r}.log" 2>&1
  rc=$?; t1=$(date +%s.%N)
  kill "$POLL" 2>/dev/null; wait "$POLL" 2>/dev/null
  el=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
  echo "3dcitydb|run$r|elapsed_s=$el|rc=$rc|pg_peak_gib=$(maxmem cdb "$RESULTS/cdb_run${r}.mem")|importer_peak_gib=$(maxmem cdb_import "$RESULTS/cdb_run${r}.mem")" | tee -a "$TIM"
done

log "MEASURE 3DCityDB (kept run)"
pg(){ docker exec cdb psql -U postgres -d postgres -P pager=off "$@"; }
{
  echo "## 3DCityDB v5 (PostgreSQL)"
  echo "logical DB size:"; pg -c "SELECT pg_size_pretty(pg_database_size('postgres')) AS db_size, pg_database_size('postgres') AS bytes;"
  echo "base tables per schema:"; pg -c "SELECT table_schema, count(*) AS tables FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema') GROUP BY table_schema ORDER BY tables DESC;"
  echo "top tables by rows:"; pg -c "SELECT schemaname, relname, n_live_tup AS rows, pg_size_pretty(pg_total_relation_size(relid)) AS size FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 8;"
  echo "total indexes (non-system):"; pg -c "SELECT count(*) FROM pg_indexes WHERE schemaname NOT IN ('pg_catalog','information_schema');"
  echo "SPATIAL (GIST) indexes:"; pg -c "SELECT count(*) AS gist_spatial_indexes FROM pg_indexes WHERE indexdef ILIKE '%USING gist%';"
  pg -c "SELECT schemaname, tablename, indexname FROM pg_indexes WHERE indexdef ILIKE '%USING gist%' ORDER BY tablename;"
  echo "spatial_ref_sys rows:"; pg -c "SELECT count(*) AS spatial_ref_sys_rows FROM spatial_ref_sys;"
} | tee "$RESULTS/measure_3dcitydb.txt"

# -- restart project Neo4j so pykci's graph is live again for querying --------
echo ">> restarting project Neo4j (pykci graph) for final live state"
( cd "$REPO/docker" && docker compose up -d neo4j >/dev/null 2>&1 )

###############################################################################
log "SUMMARY"
###############################################################################
{
  echo "Benchmark $TS  —  $GML_NAME  ($(wc -c < "$REPO/$GML_REL") bytes), $RUNS runs, envelope cpuset=$CPUSET mem=$MEM"
  echo
  printf "%-12s %10s %10s %10s %12s\n" TOOL run1 run2 run3 AVG_s
  for tool in pykci 3dcitykg 3dcitydb; do
    vals=$(grep -E "^${tool}\|" "$TIM" | grep -oE "elapsed_s=[0-9.]+" | cut -d= -f2)
    printf "%-12s" "$tool"; for v in $vals; do printf " %10s" "$v"; done
    printf " %12s\n" "$(echo "$vals" | avg)"
  done
  echo
  echo "Per-run detail + peak RAM: $TIM"
  echo "DB measurements: $RESULTS/measure_*.txt"
  echo
  echo "Live databases (NOT deleted):"
  echo "  pykci     Neo4j      bolt://localhost:7687  (neo4j/$NEO4J_PASS)"
  echo "  3DCityKG  Neo4j      query container 'citykg_query' on 7475/7688 (auth disabled)"
  echo "  3DCityDB  PostgreSQL localhost:5432  (postgres/changeMe, schema citydb)"
  echo "  NOTE: pykci & 3DCityKG Neo4j share default ports; only one served at a time."
} | tee "$SUMMARY"

echo; echo "DONE. Full results in: $RESULTS"
