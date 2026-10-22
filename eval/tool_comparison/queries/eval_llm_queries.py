"""
Batch natural-language query runner for the pykci query benchmark.

Sends the 10 citizen and 10 expert questions of this folder through a LangChain
GraphCypherQAChain (question -> Cypher -> answer) against a pykci graph in Neo4j
and records, per question, the generated Cypher, the answer, the number of
result rows, the time and any error. With --solutions it also grades each
generated query against the reference query of the same number in
queries_citizen_solutions.txt / queries_expert_solutions.txt (see README.md).

Read-only database access
    Every query, generated or reference, runs in a READ session
    (default_access_mode=READ_ACCESS), so Neo4j refuses any write the model
    generates. Connect with a read-only database user as well where your Neo4j
    edition has roles (Enterprise or Aura: a user with the built-in `reader`
    role). Community Edition has no roles, so there the READ session is the only
    guard: run the benchmark against a copy of the graph you can afford to lose.

Connection (the same variables as the pipeline scripts, see pykci_runtime.py)
    NEO4J_URI (or the older NEO4J_URL), NEO4J_USER, NEO4J_PASSWORD; defaults
    bolt://localhost:7687, neo4j / neo4jneo4j. --database picks the database.

LLM backends (pip install -e ".[llm]")
    ollama  a local model served by Ollama (--ollama-url or OLLAMA_BASE_URL,
            default http://localhost:11434; --model or DEFAULT_MODEL)
    claude  the Anthropic API through langchain-anthropic (needs
            ANTHROPIC_API_KEY; the schema, the questions and the result rows
            leave the machine)

Usage (from the repository root):
    python eval/tool_comparison/queries/eval_llm_queries.py --backend ollama --model gemma4:31b
    python eval/tool_comparison/queries/eval_llm_queries.py --ollama-url http://<ollama-host>:11434 --solutions
    python eval/tool_comparison/queries/eval_llm_queries.py --backend claude --model claude-sonnet-4-6 --solutions
    python eval/tool_comparison/queries/eval_llm_queries.py --only C02,E07 --solutions
    python eval/tool_comparison/queries/eval_llm_queries.py --solutions-only   # no LLM: check the reference queries

Output: a JSON report (default eval/results/llm_queries_<model>_<timestamp>.json)
and, with --solutions, a Markdown grading summary beside it (same name, .md).
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from neo4j import READ_ACCESS, GraphDatabase, Query

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # the repository root

from pykci_provenance import (
    AUTHORITATIVE,
    CROWD_SOURCED,
    DERIVED,
    LAYERS,
    ML_PREDICTED,
    RECONSTRUCTED,
    SENSED,
)
from pykci_runtime import neo4j_auth, neo4j_uri

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_OLLAMA_MODEL = os.getenv("DEFAULT_MODEL", "gemma4:31b")
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

SOLUTION_FILES = (("C", "queries_citizen_solutions.txt"), ("E", "queries_expert_solutions.txt"))

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _load_doc(filename):
    """A repository doc for the system prompt, braces escaped for ChatPromptTemplate."""
    p = ROOT / filename
    return p.read_text(encoding="utf-8").replace("{", "{{").replace("}", "}}") if p.exists() else ""


ROOF_MATERIAL_NOTE = (
    "ROOF MATERIALS are ML predictions (ingest_roof_materials.py), not official data. They are "
    "scalars on Building: predicted_roof_material_<i> (material name; i = 0 is the primary, "
    "ranked by coverage), predicted_roof_material_<i>_coverage (share of the roof, 0..1), "
    "predicted_roof_material_count, and predictedroofmaterial (the primary name). There is no "
    "RoofMaterial node. Read rank i with b['predicted_roof_material_' + toString(i)]. Only part "
    "of the buildings have a prediction: take shares and percentages over the buildings that "
    "have one, and never count a missing prediction as a material or as 'none'.\n"
)


def cypher_system_prompt():
    graph_md, cypher_md = _load_doc("GRAPH.md"), _load_doc("CYPHER.md")
    return (
        "You are a Neo4j Cypher expert working with a CityGML knowledge graph.\n\n"
        "CRITICAL: The R-tree spatial layer is named 'features'.\n"
        "Use: spatial.bbox('features',...) / spatial.withinDistance('features',...)\n\n"
        + ("=== DATABASE DOCUMENTATION ===\n" + graph_md + "\n=== END ===\n\n" if graph_md else "")
        + ("=== CYPHER REFERENCE ===\n" + cypher_md + "\n=== END ===\n\n" if cypher_md else "")
        + "=== LIVE SCHEMA ===\n{schema}\n=== END ===\n\n"
        + ROOF_MATERIAL_NOTE + "\n"
        "RULES:\n"
        "1. Inline all literal values, no $param placeholders.\n"
        "2. Return ONLY the Cypher query, no markdown fences, no explanation.\n"
        "3. No window functions (OVER): use collect()+UNWIND for percentages.\n"
        "4. UNION requires identical column names in every branch.\n"
        "5. Coordinate system is ETRS89 UTM32 (metres).\n"
        "6. Read only: never CREATE, MERGE, SET, DELETE or REMOVE; the database refuses writes.\n"
    )


# Key hints per provenance layer; the layer names are pykci_provenance.LAYERS, so the
# answers use the same wording as the exporters and the clients.
_LAYER_KEYS = (
    (AUTHORITATIVE, ("un-prefixed feature keys such as measured_height, storeys_above_ground, "
                     "function_code, roof_type_code, ground_z, center_x/center_y, bbox_*")),
    (CROWD_SOURCED, "keys starting with osm_ or addr_"),
    (ML_PREDICTED, "keys starting with predicted_ or pred_, and predictedroofmaterial"),
    (RECONSTRUCTED, "keys starting with lod3_"),
    (SENSED, "keys starting with live_"),
    (DERIVED, ("keys a node lists in derived_keys, nodes with source_type 'derived', and "
               "whatever the query itself computed (counts, averages, grid cells, scores)")),
)


def qa_system_prompt(top_k):
    layers = "\n".join(f"- {LAYERS[layer]}. Keys: {keys}" for layer, keys in _LAYER_KEYS)
    return (
        "You answer questions about a pykci city knowledge graph (Neo4j) from the query result "
        "you are given and nothing else.\n"
        "The graph mixes sources of different trust (GRAPH.md, 'Provenance & Multi-Source Design "
        "Principle'). Say which source each value you report comes from; the property name "
        "tells it:\n"
        f"{layers}\n"
        "Rules:\n"
        "1. Only authoritative values are official facts. Call ML-predicted values predictions, "
        "OpenStreetMap values crowd-sourced, LoD3 values reconstructed and live values sensed.\n"
        "2. The coverage of a predicted roof material is the share of the roof the classifier "
        "assigned to it, not a calibrated probability.\n"
        "3. Predicted roof materials cover only part of the buildings. A building without a "
        "prediction has an unknown material, not 'none': give shares relative to the buildings "
        "that have a prediction, and say so.\n"
        f"4. You see at most the first {int(top_k)} rows of the result; say so when the answer "
        "depends on rows you do not see.\n"
        "5. If the result is empty, answer 'No results found.' and do not guess.\n"
        "Be concise."
    )


# ---------------------------------------------------------------------------
# Question sets (numbered like the sections of the *_solutions.txt files)
# ---------------------------------------------------------------------------

CITIZEN_QUESTIONS = [
    ("C01", "How many buildings are in the dataset and what is the average building height?"),
    ("C02", "What are the most common roof materials and their percentage share?"),
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
    ("E05", "For each building function type (extracted from the first part of the description before the comma) with at least 500 buildings that have a roof material prediction, show the count per primary roof material as a percentage."),
    ("E06", "Divide Hamburg into 2 km grid cells. For each cell with at least 50 buildings that have roof material predictions, show what percentage have dark roofs (tar_paper or metal) versus light roofs (concrete or glass)."),
    ("E07", "For buildings with roof material predictions, classify them by total coverage completeness: complete (sum of coverages >= 0.95), good (0.75-0.95), partial (0.50-0.75), low (below 0.50). Show count and average coverage per class."),
    ("E08", "Find 500 metre grid cells containing at least 3 buildings taller than 30 metres. Show the cluster location, building count, average height, and max height ordered by count descending."),
    ("E09", "Calculate a floor area ratio proxy per 1 km grid cell. For each building estimate the footprint as (bbox_max_x - bbox_min_x) times (bbox_max_y - bbox_min_y) and multiply by storeys_above_ground to get gross floor area. Aggregate per cell and compute the ratio of total GFA to total footprint."),
    ("E10", "Group all buildings by ground elevation zone: below sea level (ground_z < 0), low-lying (0 to 5m), mid elevation (5 to 15m), elevated (15 to 30m), high ground (above 30m). For each zone show building count, average ground elevation, average building height, and maximum building height."),
]

QUESTION_SETS = (("citizen", CITIZEN_QUESTIONS), ("expert", EXPERT_QUESTIONS))

# ---------------------------------------------------------------------------
# Read-only Neo4j access
# ---------------------------------------------------------------------------


def run_read(driver, database, cypher, timeout):
    """Run one query in a READ session; rows as dicts (like Neo4jGraph.query)."""
    with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
        return [r.data() for r in session.run(Query(cypher, timeout=timeout))]


def make_readonly_graph(uri, auth, database, timeout):
    """A langchain Neo4jGraph whose queries all run in READ sessions.

    It also keeps the last query and its full result: the chain hands only the
    first top_k rows to the LLM, while the grading compares all of them."""
    from langchain_neo4j import Neo4jGraph

    class ReadOnlyNeo4jGraph(Neo4jGraph):
        last_query = None
        last_rows = None

        def query(self, query, params=None, session_params=None):
            self.last_query, self.last_rows = query, None
            # Passing session_params makes Neo4jGraph run the query in a session
            # opened with these parameters (instead of driver.execute_query).
            session_params = {**(session_params or {}), "default_access_mode": READ_ACCESS}
            rows = super().query(query, params, session_params)
            self.last_rows = rows
            return rows

    return ReadOnlyNeo4jGraph(url=uri, username=auth[0], password=auth[1],
                              database=database, timeout=timeout)


# ---------------------------------------------------------------------------
# Reference queries and grading
# ---------------------------------------------------------------------------

_SECTION = re.compile(r"^//\s*(\d+)\.\s")


def load_solutions():
    """{'C01': cypher, ..., 'E10': cypher}: one query per '// N. title' section."""
    solutions = {}
    for prefix, filename in SOLUTION_FILES:
        current = None
        for line in (HERE / filename).read_text(encoding="utf-8").splitlines():
            m = _SECTION.match(line.strip())
            if m:
                current = f"{prefix}{int(m.group(1)):02d}"
                solutions[current] = []
            elif current and line.strip() and not line.lstrip().startswith("//"):
                solutions[current].append(line.rstrip())
    return {qid: "\n".join(lines) for qid, lines in solutions.items() if lines}


def _norm(value, digits):
    """Comparable form of one result value: numbers rounded, lists and maps unordered."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return tuple(sorted((_norm(v, digits) for v in value), key=repr))
    if isinstance(value, dict):
        return tuple(sorted(((str(k), _norm(v, digits)) for k, v in value.items()), key=repr))
    return str(value)


def _frozen(counter):
    return tuple(sorted(counter.items(), key=repr))


def compare_results(expected, generated, digits, max_pairs=2_000_000):
    """Compare two result sets ignoring row order, column names and column order.

    A row is the multiset of its values. 'exact': both results hold the same rows.
    Otherwise rows are paired one-to-one when they are equal or one row's values
    are a subset of the other's (one side has extra or fewer columns):
    'match' pairs every row of both, 'covers' pairs every reference row but the
    generated result has more rows, 'partial' pairs some, 'mismatch' none."""
    exp = [Counter(_norm(v, digits) for v in row.values()) for row in expected]
    gen = [Counter(_norm(v, digits) for v in row.values()) for row in generated]
    grade = {"rows_expected": len(exp), "rows_generated": len(gen)}
    if Counter(map(_frozen, exp)) == Counter(map(_frozen, gen)):
        return {"status": "exact", **grade, "rows_matched": len(exp)}
    if not exp:
        return {"status": "mismatch", **grade, "rows_matched": 0}

    free = {}
    for j, row in enumerate(gen):
        free.setdefault(_frozen(row), []).append(j)
    used, matched, rest = set(), 0, []
    for row in exp:
        js = free.get(_frozen(row))
        if js:
            used.add(js.pop())
            matched += 1
        else:
            rest.append(row)
    scanned = len(rest) * len(gen) <= max_pairs
    if scanned:
        for row in rest:
            for j, other in enumerate(gen):
                if j not in used and (row <= other or other <= row):
                    used.add(j)
                    matched += 1
                    break
    if matched == len(exp):
        status = "match" if len(gen) == len(exp) else "covers"
    else:
        status = "partial" if matched else "mismatch"
    grade = {"status": status, **grade, "rows_matched": matched}
    if not scanned:
        grade["note"] = "subset pairing skipped (too many rows); only equal rows counted"
    return grade


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def make_llm(args):
    if args.backend == "claude":
        from langchain_anthropic import ChatAnthropic
        model = args.model or DEFAULT_CLAUDE_MODEL
        return ChatAnthropic(model=model, temperature=0), model, None
    from langchain_ollama import ChatOllama
    model = args.model or DEFAULT_OLLAMA_MODEL
    base_url = args.ollama_url or OLLAMA_BASE_URL
    return ChatOllama(model=model, temperature=0, num_ctx=args.num_ctx, base_url=base_url), model, base_url


def ask(chain, graph, qid, label, question):
    graph.last_query = graph.last_rows = None
    t0 = time.perf_counter()
    try:
        res = chain.invoke({"query": question})
        status, error, answer = "ok", None, str(res.get("result", ""))
    except Exception as exc:  # noqa: BLE001  LLM, Cypher syntax, write refused, timeout
        status, error, answer = "error", f"{type(exc).__name__}: {exc}", ""
    elapsed = time.perf_counter() - t0
    rows = graph.last_rows
    record = {
        "id": qid, "set": label, "question": question,
        "cypher": (graph.last_query or "").strip(), "answer": answer.strip(),
        "rows": None if rows is None else len(rows),
        "rows_sample": None if rows is None else rows[:5],
        "time_s": round(elapsed, 2), "status": status, "error": error,
    }
    return record, rows


def grade_record(record, rows, qid, solutions, cache, driver, args):
    if qid not in solutions:
        return {"status": "no_reference"}
    if qid not in cache:
        t0 = time.perf_counter()
        try:
            cache[qid] = (run_read(driver, args.database, solutions[qid], args.timeout), None,
                          time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001  recorded as reference_error
            cache[qid] = (None, f"{type(exc).__name__}: {exc}", time.perf_counter() - t0)
    expected, error, elapsed = cache[qid]
    if error:
        return {"status": "reference_error", "error": error}
    if rows is None:
        return {"status": "error" if record["status"] == "error" else "no_query",
                "rows_expected": len(expected)}
    grade = compare_results(expected, rows, args.round)
    grade.update(reference_time_s=round(elapsed, 2), reference_sample=expected[:5])
    return grade


def selected_questions(only):
    wanted = {q.strip().upper() for q in only.split(",")} if only else None
    sets = []
    for label, questions in QUESTION_SETS:
        chosen = [(qid, q) for qid, q in questions if wanted is None or qid in wanted]
        if chosen:
            sets.append((label, chosen))
    known = {qid for _, qs in QUESTION_SETS for qid, _ in qs}
    if wanted and wanted - known:
        raise SystemExit(f"Unknown question ids: {', '.join(sorted(wanted - known))}")
    return sets


def check_solutions(sets, args):
    """--solutions-only: run the reference queries read-only, no LLM."""
    solutions = load_solutions()
    driver = GraphDatabase.driver(neo4j_uri(), auth=neo4j_auth())
    failed = 0
    try:
        for _, questions in sets:
            for qid, _ in questions:
                if qid not in solutions:
                    print(f"  [{qid}] no reference query")
                    failed += 1
                    continue
                t0 = time.perf_counter()
                try:
                    rows = run_read(driver, args.database, solutions[qid], args.timeout)
                    first = json.dumps(rows[0], default=str, ensure_ascii=False)[:100] if rows else ""
                    print(f"  [{qid}] ok   {time.perf_counter() - t0:6.2f}s  {len(rows):>6} rows  {first}")
                except Exception as exc:  # noqa: BLE001  reported, the next query still runs
                    failed += 1
                    print(f"  [{qid}] ERR  {type(exc).__name__}: {exc}")
    finally:
        driver.close()
    return failed


def write_summary(path, report):
    lines = [
        f"# LLM query run: {report['model']} ({report['backend']})",
        "",
        (f"Run {report['timestamp']}, {report['total_queries']} questions. Generated query results "
         "are graded against the reference queries in queries_citizen_solutions.txt and "
         f"queries_expert_solutions.txt: numbers rounded to {report['round_digits']} decimals, row "
         "order, column names and column order ignored (see README.md). The grade is a screening "
         "aid: read the answers before drawing conclusions."),
        "",
        "| Id | Run | Grade | Rows (reference / generated / paired) | Time (s) |",
        "|----|-----|-------|---------------------------------------|---------:|",
    ]
    for q in report["queries"]:
        g = q.get("grade") or {}
        rows = f"{g.get('rows_expected', '')} / {g.get('rows_generated', '')} / {g.get('rows_matched', '')}"
        lines.append(f"| {q['id']} | {q['status']} | {g.get('status', '')} | {rows} | {q['time_s']} |")
    counts = Counter((q.get("grade") or {}).get("status", "") for q in report["queries"])
    lines += ["", "Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Run the benchmark questions through an LLM against a pykci graph (read-only).")
    parser.add_argument("--backend", choices=["ollama", "claude"], default="ollama")
    parser.add_argument("--model", default=None, help="model name (default per backend)")
    parser.add_argument("--ollama-url", default=None, help=f"Ollama base URL (default {OLLAMA_BASE_URL})")
    parser.add_argument("--num-ctx", type=int, default=65536,
                        help="Ollama context window; GRAPH.md + CYPHER.md need about 30k tokens")
    parser.add_argument("--database", default="neo4j", help="Neo4j database name")
    parser.add_argument("--top-k", type=int, default=500, help="result rows handed to the LLM")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="transaction timeout in seconds for every query")
    parser.add_argument("--only", default=None, help="comma-separated question ids, e.g. C02,E07")
    parser.add_argument("--solutions", action="store_true",
                        help="grade each generated query against its reference query")
    parser.add_argument("--solutions-only", action="store_true",
                        help="run only the reference queries (no LLM) to check them on this graph")
    parser.add_argument("--round", type=int, default=1,
                        help="decimals numbers are rounded to before comparing (default 1)")
    parser.add_argument("--output", default=None, help="JSON report path")
    args = parser.parse_args()

    sets = selected_questions(args.only)
    print(f"Neo4j: {neo4j_uri()}  database={args.database}  (read-only sessions)")
    if args.solutions_only:
        sys.exit(1 if check_solutions(sets, args) else 0)

    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_neo4j import GraphCypherQAChain
        llm, model, base_url = make_llm(args)
    except ImportError as exc:
        raise SystemExit(f"{exc}\nInstall the LLM extra: pip install -e \".[llm]\"")
    print(f"Backend: {args.backend}  model={model}" + (f"  url={base_url}" if base_url else ""))

    try:
        graph = make_readonly_graph(neo4j_uri(), neo4j_auth(), args.database, args.timeout)
    except ValueError as exc:  # Neo4jGraph reports connection and auth failures this way
        raise SystemExit(f"[error] Neo4j: {exc}")
    chain = GraphCypherQAChain.from_llm(
        llm=llm, graph=graph,
        cypher_prompt=ChatPromptTemplate.from_messages(
            [("system", cypher_system_prompt()), ("human", "{question}\n\nCypher:")]),
        qa_prompt=ChatPromptTemplate.from_messages(
            [("system", qa_system_prompt(args.top_k)),
             ("human", "Query result:\n{context}\n\nQuestion: {question}\n\nAnswer:")]),
        top_k=args.top_k, verbose=False, return_intermediate_steps=True,
        # langchain-neo4j refuses to build the chain without this opt-in. It is
        # acceptable here only because every query runs in a READ session (see
        # ReadOnlyNeo4jGraph), so a generated write fails instead of running.
        allow_dangerous_requests=True,
    )

    solutions = load_solutions() if args.solutions else {}
    driver = GraphDatabase.driver(neo4j_uri(), auth=neo4j_auth()) if args.solutions else None
    cache, results, set_times = {}, [], {}
    t_total = time.perf_counter()
    try:
        for label, questions in sets:
            print(f"\n=== {label.upper()} QUERIES ===")
            for qid, question in questions:
                print(f"  [{qid}] {question[:70]}...")
                record, rows = ask(chain, graph, qid, label, question)
                if args.solutions:
                    record["grade"] = grade_record(record, rows, qid, solutions, cache, driver, args)
                results.append(record)
                set_times[label] = set_times.get(label, 0.0) + record["time_s"]
                grade = f"  grade={record['grade']['status']}" if args.solutions else ""
                shown = record["answer"][:80] if record["status"] == "ok" else record["error"][:160]
                print(f"       [{record['status']}] {record['time_s']:.1f}s{grade}  {shown}")
    finally:
        if driver:
            driver.close()
        graph.close()
    total_time = time.perf_counter() - t_total

    ok = sum(1 for r in results if r["status"] == "ok")
    avg_t = sum(r["time_s"] for r in results) / len(results)
    print(f"\n{'=' * 60}")
    print(f"Total queries : {len(results)}  (ok: {ok}, errors: {len(results) - ok})")
    for label, questions in sets:
        print(f"{label:<14}: {set_times[label]:.1f}s  avg/query: {set_times[label] / len(questions):.1f}s")
    print(f"Total runtime : {total_time:.1f}s  avg/query: {avg_t:.1f}s")

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    output = Path(args.output) if args.output else ROOT / "eval" / "results" / f"llm_queries_{slug}_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": stamp, "model": model, "backend": args.backend, "ollama_url": base_url,
        "neo4j_uri": neo4j_uri(), "database": args.database, "top_k": args.top_k,
        "round_digits": args.round,
        "total_queries": len(results), "ok": ok, "errors": len(results) - ok,
        **{f"{label}_time_s": round(t, 2) for label, t in set_times.items()},
        "total_time_s": round(total_time, 2), "avg_time_s": round(avg_t, 2),
        "queries": results,
    }
    output.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"Report saved  : {output}")
    if args.solutions:
        summary = output.with_suffix(".md")
        write_summary(summary, report)
        counts = Counter(r["grade"]["status"] for r in results)
        print("Grades        : " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        print(f"Summary saved : {summary}")


if __name__ == "__main__":
    main()
