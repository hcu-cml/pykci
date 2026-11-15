"""
Neo4j + Ollama via LangChain — interactive query interface.

Usage:
    python query_ollama_full.py
    python query_ollama_full.py --model mistral
    python query_ollama_full.py --model codellama --no-spatial

Dependencies:
    pip install langchain langchain-community langchain-ollama langchain-neo4j neo4j
"""

import argparse
import time
from pathlib import Path

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_neo4j import GraphCypherQAChain, Neo4jGraph
from langchain_ollama import ChatOllama
from neo4j.exceptions import ServiceUnavailable

from ollama_session import (DEFAULT_MODEL, OLLAMA_BASE_URL, check_ollama, connect, neo4j_auth,
                            neo4j_uri, print_rows, run_cypher)

# ---------------------------------------------------------------------------
# Load GRAPH.md as session context
# ---------------------------------------------------------------------------

def _load_file(filename: str) -> str:
    path = Path(__file__).parent.parent / filename
    if not path.exists():
        return ""
    # Escape {{ }} so ChatPromptTemplate doesn't treat them as variable placeholders
    return path.read_text(encoding="utf-8").replace("{", "{{").replace("}", "}}")

_INSTRUCTIONS_BLOCK = _load_file("GRAPH.md")
_CYPHER_REF_BLOCK   = _load_file("CYPHER.md")

_CYPHER_SYSTEM = (
    "You are a Neo4j Cypher expert working with a CityGML knowledge graph.\n\n"
    "!!! CRITICAL — READ BEFORE WRITING ANY QUERY !!!\n"
    "The R-tree spatial layer in this database is named 'features' — NOT 'buildings'.\n"
    "Always write: spatial.bbox('features', ...) / spatial.intersects('features', ...) /\n"
    "spatial.withinDistance('features', ...). Using any other layer name will cause a runtime error.\n\n"
    + ("=== DATABASE DOCUMENTATION ===\n" + _INSTRUCTIONS_BLOCK + "\n=== END DOCUMENTATION ===\n\n"
       if _INSTRUCTIONS_BLOCK else "")
    + ("=== CYPHER REFERENCE ===\n" + _CYPHER_REF_BLOCK + "\n=== END CYPHER REFERENCE ===\n\n"
       if _CYPHER_REF_BLOCK else "")
    + "=== LIVE DATABASE SCHEMA ===\n{schema}\n=== END SCHEMA ===\n\n"
    "RULES — follow these exactly:\n"
    "1. Spatial layer is named 'features'. Never use 'buildings' or any other name. Examples:\n"
    "     CALL spatial.bbox('features', {{x: <min_x>, y: <min_y>}}, {{x: <max_x>, y: <max_y>}}) YIELD node\n"
    "     CALL spatial.withinDistance('features', {{x: <cx>, y: <cy>}}, <distKm>) YIELD node, distance\n"
    "     CALL spatial.intersects('features', '<wkt>') YIELD node\n"
    "2. For any location-based query always use the spatial procedures above — never a label scan with WHERE on coordinates.\n"
    "3. Inline ALL literal values — no $param placeholders.\n"
    "4. Return only the Cypher query, no markdown fences, no explanation.\n"
    "5. Coordinate system is ETRS89 UTM32 (metres).\n"
    "6. UNION requires identical return column names in every branch. Never use UNION to combine\n"
    "   counts of different things. Use WITH chaining instead:\n"
    "     MATCH (n) WITH count(n) AS nodes\n"
    "     MATCH ()-[r]->() RETURN nodes, count(r) AS relationships\n"
    "7. To count both nodes and relationships in one query, always use WITH chaining (rule 6), never UNION."
)

# System message carries all context; human message is the question only.
CYPHER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _CYPHER_SYSTEM),
    ("human", "{question}\n\nCypher:"),
])

QA_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You answer questions about a Neo4j graph database.\n"
     "The query result is authoritative — use it directly.\n"
     "If the result is an empty list, say 'No results found.'\n"
     "If the result is a boolean or count, state it plainly.\n"
     "Never say 'I don't know' when a result is present."),
    ("human", "Query result:\n{context}\n\nQuestion: {question}\n\nAnswer:"),
])

# ---------------------------------------------------------------------------
# Chain builders
# ---------------------------------------------------------------------------


def build_qa_chain(graph: Neo4jGraph, llm: ChatOllama) -> GraphCypherQAChain:
    """High-level chain: question → Cypher → answer in plain English."""
    return GraphCypherQAChain.from_llm(
        llm=llm,
        graph=graph,
        cypher_prompt=CYPHER_PROMPT,
        qa_prompt=QA_PROMPT,
        top_k=500,
        verbose=False,
        return_intermediate_steps=True,
        allow_dangerous_requests=True,
    )


def build_cypher_chain(schema: str, llm: ChatOllama):
    """Low-level chain: question → raw Cypher only (no execution)."""
    return CYPHER_PROMPT | llm | StrOutputParser()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------


def repl(qa_chain, cypher_chain, schema: str, driver, args) -> None:
    print("\nNeo4j + Ollama via LangChain")
    print(f"Model : {args.model}   Neo4j : {neo4j_uri()}   ctx : 65536 tokens")

    # Show which context files are loaded and their sizes
    def _ctx_status(label: str, block: str) -> str:
        if not block:
            return f"  [MISSING] {label}"
        chars = len(block)
        tokens_est = chars // 4
        return f"  [ok] {label} — {chars:,} chars (~{tokens_est:,} tokens)"

    print("Context loaded into system prompt:")
    print(_ctx_status("GRAPH.md    ", _INSTRUCTIONS_BLOCK))
    print(_ctx_status("CYPHER.md   ", _CYPHER_REF_BLOCK))
    total_est = (len(_INSTRUCTIONS_BLOCK) + len(_CYPHER_REF_BLOCK)) // 4
    print(f"  Total system prompt estimate: ~{total_est:,} tokens of 65,536 available")

    # Show schema summary so user can verify the DB is populated
    with driver.session() as s:
        counts = s.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt "
            "ORDER BY cnt DESC"
        ).data()
    if counts:
        summary = "  ".join(f"{r['label']}({r['cnt']})" for r in counts[:8])
        print(f"DB schema : {summary}")
    else:
        print("DB schema : [empty — run ingest_citygml.py first]")

    print("Commands: :cypher <question>   :quit / :exit   or just type a question\n")

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

        # :cypher mode — show generated Cypher and run it directly
        if line.startswith(":cypher "):
            question = line[len(":cypher "):].strip()
            print("Generating Cypher...")
            t0 = time.perf_counter()
            cypher = cypher_chain.invoke({"schema": schema, "question": question})
            gen_time = time.perf_counter() - t0

            cypher = cypher.strip().strip("```").strip()
            print(f"\n```cypher\n{cypher}\n```\n")

            try:
                rows, exec_time = run_cypher(driver, cypher)
                print_rows(rows)
                print(f"\nGeneration time : {gen_time:.3f} s")
                print(f"Query time      : {exec_time:.3f} s")
            except Exception as exc:
                print(f"[error] {exc}")
            print()
            continue

        # Default — full QA chain
        print("Thinking...")
        try:
            t0 = time.perf_counter()
            result = qa_chain.invoke({"query": line})
            elapsed = time.perf_counter() - t0

            steps = result.get("intermediate_steps", [])
            if steps:
                generated = steps[0].get("query", "")
                if generated:
                    print(f"\n```cypher\n{generated.strip()}\n```\n")

            print(result["result"])
            print(f"\nQuery time: {elapsed:.3f} s\n")

        except Exception as exc:
            print(f"[error] {exc}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Neo4j + Ollama via LangChain")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name")
    parser.add_argument("--no-spatial", action="store_true",
                        help="Use default Cypher prompt (no spatial-index rules)")
    args = parser.parse_args()

    # -- Check Ollama --
    if not check_ollama(OLLAMA_BASE_URL, args.model):
        return

    llm = ChatOllama(model=args.model, temperature=0, num_ctx=65536, base_url=OLLAMA_BASE_URL)

    # -- Connect to Neo4j --
    try:
        user, password = neo4j_auth()
        graph = Neo4jGraph(url=neo4j_uri(), username=user, password=password)
        graph.refresh_schema()
    except ServiceUnavailable:
        print(f"[error] Cannot reach Neo4j at {neo4j_uri()}.")
        print("        Start Neo4j and try again.")
        return

    driver = connect()

    cypher_prompt = None if args.no_spatial else CYPHER_PROMPT
    qa_chain = GraphCypherQAChain.from_llm(
        llm=llm,
        graph=graph,
        cypher_prompt=cypher_prompt,
        qa_prompt=QA_PROMPT,
        top_k=500,
        verbose=False,
        return_intermediate_steps=True,
        allow_dangerous_requests=True,
    )
    cypher_chain = build_cypher_chain(graph.schema, llm)

    # -- Run REPL --
    try:
        repl(qa_chain, cypher_chain, graph.schema, driver, args)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
