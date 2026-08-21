"""
Batch LLM query evaluator — runs NL equivalents of the citizen and expert
queries through a LangChain GraphCypherQAChain and records timing, generated
Cypher, answers, and errors.

Usage:
    python eval_llm_queries.py --backend ollama --ollama-url http://192.168.4.70:11434 --model gemma4:31b
    python eval_llm_queries.py --backend claude --model claude-sonnet-4-6
"""

import argparse
import json
import os
import time
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_neo4j import GraphCypherQAChain, Neo4jGraph
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable

NEO4J_URL      = os.getenv("NEO4J_URL",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jneo4j")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL   = os.getenv("DEFAULT_MODEL",   "gemma4:31b")

# ---------------------------------------------------------------------------
# Context documents
# ---------------------------------------------------------------------------

def _load(filename):
    p = Path(__file__).parent / filename
    return p.read_text(encoding="utf-8").replace("{", "{{").replace("}", "}}") if p.exists() else ""

_GRAPH_MD  = _load("GRAPH.md")
_CYPHER_MD = _load("CYPHER.md")

CYPHER_SYSTEM = (
    "You are a Neo4j Cypher expert working with a CityGML knowledge graph.\n\n"
    "CRITICAL: The R-tree spatial layer is named 'features'.\n"
    "Use: spatial.bbox('features',...) / spatial.withinDistance('features',...)\n\n"
    + ("=== DATABASE DOCUMENTATION ===\n" + _GRAPH_MD  + "\n=== END ===\n\n" if _GRAPH_MD  else "")
    + ("=== CYPHER REFERENCE ===\n"       + _CYPHER_MD + "\n=== END ===\n\n" if _CYPHER_MD else "")
    + "=== LIVE SCHEMA ===\n{schema}\n=== END ===\n\n"
    "RULES:\n"
    "1. Inline all literal values — no $param placeholders.\n"
    "2. Return ONLY the Cypher query, no markdown fences, no explanation.\n"
    "3. No window functions (OVER) — use collect()+UNWIND for percentages.\n"
    "4. UNION requires identical column names in every branch.\n"
    "5. Coordinate system is ETRS89 UTM32 (metres).\n"
)

CYPHER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", CYPHER_SYSTEM),
    ("human", "{question}\n\nCypher:"),
])

QA_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You answer questions about a Neo4j CityGML graph. "
     "The query result is authoritative — use it directly. "
     "Be concise. If empty, say 'No results found.'"),
    ("human", "Query result:\n{context}\n\nQuestion: {question}\n\nAnswer:"),
])

# ---------------------------------------------------------------------------
# Query sets
# ---------------------------------------------------------------------------

CITIZEN_QUESTIONS = [
    ("C01", "How many buildings are in the dataset and what is the average building height?"),
    ("C02", "What are the most common roof materials and their percentage share of all buildings?"),
    ("C03", "What are the 20 tallest buildings in Hamburg?"),
    ("C04", "How many buildings have each roof shape such as flat roof, gable roof, hip roof?"),
    ("C05", "Which buildings have a flat roof and are at least 5 metres tall? Show the top 50 by height."),
    ("C06", "How many buildings are there per number of storeys above ground?"),
    ("C07", "Which buildings have glass roofs? Show the top 50 ordered by glass coverage."),
    ("C08", "How many buildings are taller than 50 metres? List them with height and description."),
    ("C09", "Which buildings have more than one predicted roof material?"),
    ("C10", "Show all buildings in HafenCity with their height and roof materials. HafenCity is approximately between center_x 564500 and 567500, center_y 5930800 and 5932800."),
]

EXPERT_QUESTIONS = [
    ("E01", "Show the average, minimum, maximum building height and average floor-to-floor height per building type. Only include types with at least 100 buildings. Order by number of buildings descending."),
    ("E02", "Divide Hamburg into 1 km grid cells using toInteger(center_x/1000)*1000 and toInteger(center_y/1000)*1000. For each cell with at least 30 buildings show the building count, average height, max height, and how many buildings of each roof material exist."),
    ("E03", "Score buildings for solar panel suitability: flat roof = 3 points, concrete or tar_paper roof material = 2 points each, metal = 1 point, glass = minus 2 points, height >= 10m = 1 point, storeys >= 3 = 1 point. List the top 50 buildings with score >= 5."),
    ("E04", "Find buildings where the floor-to-floor height (measured_height divided by storeys_above_ground) is less than 2 metres or greater than 8 metres. These are likely data anomalies or special structures."),
    ("E05", "For each building function type (extracted from the first part of the description before the comma) with at least 500 buildings that have a roof material prediction, show the count per roof material as a percentage."),
    ("E06", "Divide Hamburg into 2 km grid cells. For each cell with at least 50 buildings that have roof material predictions, show what percentage have dark roofs (tar_paper or metal) versus light roofs (concrete or glass)."),
    ("E07", "For buildings with roof material predictions, classify them by total coverage completeness: complete (sum of coverages >= 0.95), good (0.75-0.95), partial (0.50-0.75), low (below 0.50). Show count and average coverage per class."),
    ("E08", "Find 500 metre grid cells containing at least 3 buildings taller than 30 metres. Show the cluster location, building count, average height, and max height ordered by count descending."),
    ("E09", "Calculate a floor area ratio proxy per 1 km grid cell. For each building estimate the footprint as (bbox_max_x - bbox_min_x) times (bbox_max_y - bbox_min_y) and multiply by storeys_above_ground to get gross floor area. Aggregate per cell and compute the ratio of total GFA to total footprint."),
    ("E10", "Group all buildings by ground elevation zone: below sea level (ground_z < 0), low-lying (0 to 5m), mid elevation (5 to 15m), elevated (15 to 30m), high ground (above 30m). For each zone show building count, average ground elevation, average building height, and maximum building height."),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_queries(chain, questions, driver, label):
    results = []
    for qid, question in questions:
        print(f"  [{qid}] {question[:70]}...")
        t0 = time.perf_counter()
        try:
            res = chain.invoke({"query": question})
            elapsed = time.perf_counter() - t0
            steps = res.get("intermediate_steps", [])
            cypher = steps[0].get("query", "") if steps else ""
            answer = res.get("result", "")
            status = "ok"
            error  = None
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            cypher  = ""
            answer  = ""
            status  = "error"
            error   = str(exc)

        record = {
            "id": qid, "set": label, "question": question,
            "cypher": cypher.strip(), "answer": answer.strip(),
            "time_s": round(elapsed, 2), "status": status, "error": error,
        }
        results.append(record)
        tag = "ok" if status == "ok" else "ERR"
        print(f"       [{tag}] {elapsed:.1f}s  —  {answer[:80] if answer else error}")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend",    choices=["ollama","claude"], default="ollama")
    parser.add_argument("--model",      default=None)
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--output",     default=None)
    args = parser.parse_args()

    # -- LLM --
    if args.backend == "claude":
        from langchain_anthropic import ChatAnthropic
        model = args.model or "claude-sonnet-4-6"
        llm = ChatAnthropic(model=model, temperature=0)
        print(f"Backend: Claude ({model})")
    else:
        from langchain_ollama import ChatOllama
        base_url = args.ollama_url or OLLAMA_BASE_URL
        model    = args.model or DEFAULT_MODEL
        llm = ChatOllama(model=model, temperature=0, num_ctx=32768, base_url=base_url)
        print(f"Backend: Ollama  model={model}  url={base_url}")

    # -- Neo4j --
    try:
        graph = Neo4jGraph(url=NEO4J_URL, username=NEO4J_USER, password=NEO4J_PASSWORD)
        graph.refresh_schema()
    except ServiceUnavailable as e:
        print(f"[error] Neo4j unreachable: {e}")
        return

    driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASSWORD))

    chain = GraphCypherQAChain.from_llm(
        llm=llm, graph=graph,
        cypher_prompt=CYPHER_PROMPT, qa_prompt=QA_PROMPT,
        top_k=500, verbose=False,
        return_intermediate_steps=True, allow_dangerous_requests=True,
    )

    t_total = time.perf_counter()
    all_results = []

    print("\n=== CITIZEN QUERIES ===")
    t_c = time.perf_counter()
    all_results += run_queries(chain, CITIZEN_QUESTIONS, driver, "citizen")
    citizen_time = time.perf_counter() - t_c

    print("\n=== EXPERT QUERIES ===")
    t_e = time.perf_counter()
    all_results += run_queries(chain, EXPERT_QUESTIONS, driver, "expert")
    expert_time = time.perf_counter() - t_e

    total_time = time.perf_counter() - t_total
    driver.close()

    # -- Summary --
    ok      = sum(1 for r in all_results if r["status"] == "ok")
    errors  = sum(1 for r in all_results if r["status"] == "error")
    avg_t   = sum(r["time_s"] for r in all_results) / len(all_results)

    print(f"\n{'='*60}")
    print(f"Total queries : {len(all_results)}  (ok: {ok}, errors: {errors})")
    print(f"Citizen set   : {round(citizen_time,1)}s  avg/query: {round(citizen_time/len(CITIZEN_QUESTIONS),1)}s")
    print(f"Expert set    : {round(expert_time,1)}s  avg/query: {round(expert_time/len(EXPERT_QUESTIONS),1)}s")
    print(f"Total runtime : {round(total_time,1)}s  avg/query: {round(avg_t,1)}s")

    # -- Save report --
    output_path = args.output or f"eval/results/eval_{model.replace(':','_').replace('/','_')}_{int(time.time())}.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    report = {
        "model": model, "backend": args.backend,
        "ollama_url": args.ollama_url,
        "total_queries": len(all_results), "ok": ok, "errors": errors,
        "citizen_time_s": round(citizen_time, 2),
        "expert_time_s":  round(expert_time,  2),
        "total_time_s":   round(total_time,   2),
        "avg_time_s":     round(avg_t, 2),
        "queries": all_results,
    }
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved  : {output_path}")


if __name__ == "__main__":
    main()
