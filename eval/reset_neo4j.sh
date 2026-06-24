#!/usr/bin/env bash
# Reset Neo4j to an empty database: recreate the neo4j service with fresh volumes,
# then block until Bolt is ready. Leaves ollama untouched.
set -euo pipefail

cd "$(dirname "$0")/../docker"

echo ">> Stopping and removing neo4j container + data volumes …"
docker compose stop neo4j >/dev/null 2>&1 || true
docker compose rm -f neo4j >/dev/null 2>&1 || true
docker volume rm docker_neo4j_data docker_neo4j_logs >/dev/null 2>&1 || true

echo ">> Starting fresh neo4j …"
docker compose up -d neo4j >/dev/null

echo -n ">> Waiting for neo4j to become healthy "
for i in $(seq 1 60); do
  status=$(docker inspect --format '{{.State.Health.Status}}' neo4j 2>/dev/null || echo "starting")
  if [ "$status" = "healthy" ]; then
    echo " OK (healthy)"
    # Bolt may lag the HTTP healthcheck slightly; give it a moment.
    sleep 3
    exit 0
  fi
  echo -n "."
  sleep 3
done

echo " FAILED: neo4j did not become healthy in time"
exit 1
