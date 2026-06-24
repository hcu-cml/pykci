"""
Neo4j + Ollama — minimal-context query interface.

A lean counterpart to query_ollama_full.py, built for small open models on a
laptop (no GPU / Apple Silicon). Instead of injecting all of GRAPH.md + CYPHER.md
(~24k tokens) into the system prompt every call, it embeds a hand-distilled
~700-token schema below (MINIMAL_CONTEXT). That lets you run a small num_ctx
(default 8192) so the KV cache fits in RAM and prompt processing stays fast.

Differences from query_ollama_full.py:
  * One LLM call per question (generate Cypher), not two — no English-summary
    round-trip unless you pass --summarize.
  * Strips <think>...</think> blocks (deepseek-r1 etc.) and ``` fences from the
    generated Cypher before running it.
  * num_ctx is a CLI flag (--ctx), default 8192.

Usage:
    python query_ollama_min.py
    python query_ollama_min.py --model qwen2.5-coder:7b --ctx 16384
    python query_ollama_min.py --summarize

Dependencies:
    pip install langchain-ollama neo4j
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request

from langchain_ollama import ChatOllama
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable

# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------

NEO4J_URL = os.getenv("NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek-r1:14b")

# ---------------------------------------------------------------------------
# Minimal embedded context — the bare minimum the model needs to write Cypher
# for this CityGML graph. Keep this tight; it is the whole point of this script.
# ---------------------------------------------------------------------------

MINIMAL_CONTEXT = """\
You are a Neo4j Cypher expert for a CityGML knowledge graph.
CRS: ETRS89 / UTM32, units = metres.

NODES (label: key properties)
  Building          : id, measured_height (m), center_x, center_y, bbox_wkt
  BuildingPart      : id, measured_height  (reached only via HAS_BUILDING_PART; NOT a :Building)
  CityObject        : id, bbox_wkt  (shared label on Bridge, Tunnel, Road, Railway,
                      SolitaryVegetationObject, WaterBody, LandUse, CityFurniture,
                      ReliefFeature, CityObjectGroup, GenericCityObject — each also
                      carries its own module label)
  BoundarySurface   : id, surface_type  (RoofSurface / WallSurface / GroundSurface ...)
  BuildingFunction  : value          RoofType : value
  District / City   : name
  GeometryPolygon / GeometryRing(pos_list) / GeometrySolid / GeometryMultiSurface
  Dataset           : id, name
  Entity            : index plumbing only — IGNORE in queries.

RELATIONSHIPS
  (Building)-[:PART_OF]->(Dataset)
  (Building)-[:HAS_FUNCTION]->(BuildingFunction)
  (Building)-[:HAS_ROOF_TYPE]->(RoofType)
  (Building)-[:HAS_BOUNDARY]->(BoundarySurface)
  (Building)-[:HAS_BUILDING_PART]->(BuildingPart)
  (Building|CityObject)-[:LOCATED_IN]->(District)-[:LOCATED_IN]->(City)
  (BoundarySurface)-[:HAS_POLYGON]->(GeometryPolygon)-[:HAS_EXTERIOR_RING]->(GeometryRing)

SPATIAL (R-tree layer is named 'features' — NEVER 'buildings'):
  CALL spatial.bbox('features', {x:<minX>,y:<minY>}, {x:<maxX>,y:<maxY>}) YIELD node
  CALL spatial.withinDistance('features', {x:<cx>,y:<cy>}, <distKm>) YIELD node, distance
  CALL spatial.intersects('features', '<wkt>') YIELD node
  Use these for ALL location queries — never a label scan with WHERE on coordinates.

CYPHER SYNTAX (Neo4j 5/25)
  Match    : MATCH (b:Building) WHERE b.measured_height > 20 RETURN b.id, b.measured_height
  Traverse : MATCH (b:Building)-[:HAS_FUNCTION]->(f:BuildingFunction) RETURN f.value, count(b) AS n
  Var-len  : MATCH (d:District)<-[:LOCATED_IN*1..2]-(b) RETURN b.id
  Optional : OPTIONAL MATCH (b)-[:HAS_ROOF_TYPE]->(rt) RETURN b.id, rt.value
  Multi-label endpoint: MATCH (n) WHERE (n:Building OR n:CityObject) AND n.bbox_wkt IS NOT NULL ...
  Filters  : =  <>  <  >  <=  >=  | IN [..] | IS NULL / IS NOT NULL
             STARTS WITH / ENDS WITH / CONTAINS | =~ 'regex'
  Case-insensitive text: WHERE toLower(b.id) CONTAINS toLower('haus')
  Existence: use '<expr> IS NOT NULL' or the subquery 'WHERE EXISTS { (a)-[:R]->(b) }'.
             The exists() FUNCTION is removed in Neo4j 5 — never write exists(n) / exists(n.prop).
  Aggregate: count(x) count(DISTINCT x) avg() sum() min() max() collect(x)
             Non-aggregated RETURN/WITH items are the grouping key (no GROUP BY clause).
  Pipeline : MATCH (b:Building) WITH b ORDER BY b.measured_height DESC LIMIT 5 RETURN b.id
  Page     : ORDER BY <expr> [DESC]   SKIP n   LIMIT n
  Null-safe: coalesce(b.measured_height, 0)

RULES
  1. Inline ALL literal values — no $param placeholders.
  2. Building height is b.measured_height. NULLs exist — filter IS NOT NULL when comparing.
  3. UNION needs identical column names in every branch. To return both node and
     relationship counts use WITH chaining, never UNION:
       MATCH (n) WITH count(n) AS nodes
       MATCH ()-[r]->() RETURN nodes, count(r) AS relationships
  4. Output ONLY the Cypher query. No markdown fences, no explanation, no <think>.
"""

# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def clean_cypher(raw: str) -> str:
    """Strip reasoning blocks, markdown fences, and prose around the query."""
    text = _THINK_RE.sub("", raw)
    # Drop any unclosed leading <think> remnant (model hit token limit mid-think)
    if "<think>" in text and "</think>" not in text:
        text = text.split("<think>", 1)[0]
    # Prefer a fenced block if present
    fence = re.search(r"```(?:cypher)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    text = text.replace("```", "").strip()
    # Keep from the first Cypher keyword onward (drops any leftover prose)
    m = re.search(r"\b(MATCH|CALL|WITH|MERGE|CREATE|UNWIND|RETURN)\b", text, re.IGNORECASE)
    if m:
        text = text[m.start():]
    return text.strip()


# ---------------------------------------------------------------------------
# Ollama / Neo4j helpers
# ---------------------------------------------------------------------------


def check_ollama(base_url: str, model: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
        available = [m["name"] for m in data.get("models", [])]
        if model not in available:
            print(f"[warn] Model '{model}' not found locally. Available: {available}")
            print(f"       Run:  ollama pull {model}")
            return False
        return True
    except urllib.error.URLError:
        print(f"[error] Ollama not reachable at {base_url}.")
        print("        Start it with:  ollama serve  (or open the Ollama app)")
        return False


def run_cypher(driver, query: str) -> tuple[list[dict], float]:
    with driver.session() as session:
        t0 = time.perf_counter()
        rows = [dict(r) for r in session.run(query)]
        elapsed = time.perf_counter() - t0
    return rows, elapsed


def print_rows(rows: list[dict]) -> None:
    if not rows:
        print("  (no results)")
        return
    keys = list(rows[0].keys())
    widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    header = "  " + "  ".join(k.ljust(widths[k]) for k in keys)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows[:50]:
        print("  " + "  ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys))
    if len(rows) > 50:
        print(f"  ... ({len(rows)} rows total, showing first 50)")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

SUMMARY_SYS = (
    "Answer the question from the query result. The result is authoritative. "
    "If it is an empty list say 'No results found.' Be concise."
)


def repl(llm, driver, args) -> None:
    ctx_tokens = len(MINIMAL_CONTEXT) // 4
    print("\nNeo4j + Ollama (minimal context)")
    print(f"Model : {args.model}   ctx : {args.ctx} tokens   "
          f"embedded context : ~{ctx_tokens} tokens")
    with driver.session() as s:
        counts = s.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
        ).data()
    if counts:
        print("DB    : " + "  ".join(f"{r['label']}({r['cnt']})" for r in counts[:8]))
    else:
        print("DB    : [empty — run ingest_citygml.py first]")
    print("Type a question, or :quit to exit.\n")

    while True:
        try:
            line = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in (":quit", ":exit", "quit", "exit"):
            break

        t0 = time.perf_counter()
        try:
            raw = llm.invoke(
                [("system", MINIMAL_CONTEXT), ("human", f"{line}\n\nCypher:")]
            ).content
        except Exception as exc:
            print(f"[error] LLM call failed: {exc}\n")
            continue
        gen_time = time.perf_counter() - t0

        cypher = clean_cypher(raw)
        print(f"\n```cypher\n{cypher}\n```\n")

        try:
            rows, exec_time = run_cypher(driver, cypher)
        except Exception as exc:
            print(f"[error] {exc}\n")
            continue
        print_rows(rows)

        if args.summarize:
            answer = llm.invoke([
                ("system", SUMMARY_SYS),
                ("human", f"Query result:\n{rows[:50]}\n\nQuestion: {line}\n\nAnswer:"),
            ]).content
            answer = _THINK_RE.sub("", answer).strip()
            print(f"\n{answer}")

        print(f"\nGeneration : {gen_time:.2f} s   Query : {exec_time:.3f} s\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Neo4j + Ollama, minimal context")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name")
    parser.add_argument("--ctx", type=int, default=8192, help="num_ctx (context window)")
    parser.add_argument("--summarize", action="store_true",
                        help="Add a second LLM call to phrase the answer in English")
    args = parser.parse_args()

    if not check_ollama(OLLAMA_BASE_URL, args.model):
        return

    llm = ChatOllama(model=args.model, temperature=0, num_ctx=args.ctx,
                     base_url=OLLAMA_BASE_URL)

    try:
        driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
    except ServiceUnavailable:
        print(f"[error] Cannot reach Neo4j at {NEO4J_URL}. Start Neo4j and retry.")
        return

    try:
        repl(llm, driver, args)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
