# ── Start background services (Neo4j + Ollama) ──────────────────────────────
docker compose up -d neo4j ollama

# ── Pull LLM model (first time only, ~9 GB) ──────────────────────────────────
docker compose exec ollama ollama pull deepseek-r1:14b
# Lighter alternative (~4.7 GB):
# docker compose exec ollama ollama pull deepseek-r1:7b

# ── Start interactive query session ──────────────────────────────────────────
docker compose run --rm llm-query
# Override model:
# docker compose run --rm llm-query --model deepseek-r1:7b

# ── Stop all services ────────────────────────────────────────────────────────
docker compose down

# ── Reset Neo4j only (empty DB, keep downloaded LLM model) ───────────────────
docker compose down
docker volume rm docker_neo4j_data docker_neo4j_logs

# ── Remove everything including the LLM model download ───────────────────────
docker compose down -v

# ── Build Neo4j image only ───────────────────────────────────────────────────
docker build -t neo4j -f Dockerfile.neo4j .

# ── Dump the database ────────────────────────────────────────────────────────
docker exec -it neo4j neo4j-admin server stop
docker exec -it neo4j neo4j-admin database dump neo4j --to-path=/var/lib/neo4j/
docker cp neo4j:/var/lib/neo4j/neo4j.dump .
mv neo4j.dump "neo4j-$(date +%Y-%m-%dT%H-%M-%S).dump"
