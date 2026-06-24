#!/usr/bin/env bash
# Reset project Neo4j to an EMPTY db, then constrain it to the fair envelope
# (8 logical CPUs = cpuset 0-7, 20 GB memory) and RESTART so the JVM detects
# the 20 GB cgroup limit (heap is pinned at 12g in neo4j.conf; pagecache auto).
set -euo pipefail
cd "$(dirname "$0")"

echo ">> [pykci] resetting neo4j to empty ..."
bash /d/PROJEKTE/llm-kg/eval/reset_neo4j.sh

echo ">> [pykci] applying cpuset=0-7, memory=20g ..."
docker update --cpuset-cpus 0-7 --memory 20g --memory-swap 20g neo4j >/dev/null

echo ">> [pykci] restarting neo4j so JVM detects the 20g cap ..."
docker restart neo4j >/dev/null
echo -n ">> waiting for neo4j healthy "
for i in $(seq 1 60); do
  status=$(docker inspect --format '{{.State.Health.Status}}' neo4j 2>/dev/null || echo starting)
  if [ "$status" = "healthy" ]; then echo " OK"; sleep 3; break; fi
  echo -n "."; sleep 3
done

echo ">> [pykci] limits now:"
docker inspect neo4j --format 'CpuSet={{.HostConfig.CpusetCpus}} Mem={{.HostConfig.Memory}}'
echo ">> [pykci] node count (should be 0):"
docker exec neo4j cypher-shell -u neo4j -p neo4jneo4j "MATCH (n) RETURN count(n) AS nodes;"
