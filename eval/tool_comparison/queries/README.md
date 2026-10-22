# Query benchmark: questions, reference queries, LLM runner

| File | What it is |
| ---- | ---------- |
| `queries_citizen.txt`, `queries_expert.txt` | The 10 citizen and 10 expert questions |
| `queries_citizen_solutions.txt`, `queries_expert_solutions.txt` | Reference Cypher, one query per numbered question |
| `run_pykci.sh`, `run_citykg.sh`, `run_3dcitydb.sh`, `results_*.txt` | The 2026-06-19 tool comparison, see [`QUERY_BENCHMARK.md`](QUERY_BENCHMARK.md) |
| `open_llm/` | Results of open models (Gemma, Mistral, Qwen) on the questions |
| `eval_llm_queries.py` | Batch runner: the questions through an LLM, optionally graded against the reference queries |

## Reference queries

The `*_solutions.txt` queries run on a graph built by the pipeline in this repository:
`ingest_citygml.py` (Building properties, the `RoofType` hub) and
`ingest_roof_materials.py` (the ML-predicted roof materials, see
[`ROOF_MATERIAL_INGESTION.md`](../../../ROOF_MATERIAL_INGESTION.md)). Roof materials are
scalars on the `Building` node, not a separate node:

| Key | Meaning |
| --- | ------- |
| `predicted_roof_material_<i>` | material name, ranked by coverage; `_0` is the primary material |
| `predicted_roof_material_<i>_coverage` | share of the roof pixels the classifier gave that material (0..1) |
| `predicted_roof_material_count` | number of predicted materials of the building |

The predictions cover only part of the buildings (about half of Hamburg's). Following the
provenance rules in [`GRAPH.md`](../../../GRAPH.md) ("Provenance & Multi-Source Design
Principle"), a building without a prediction has an unknown material, not "no material", so the
queries take material shares over the buildings that have a prediction. C02 and E05 use the
primary material; C07 to C10, E02, E03, E06 and E07 use every predicted material of a building.

C09 (mixed materials) and E07 (coverage completeness) were infeasible in the 2026-06-19 run,
whose input carried a single roof-material attribute per building. With the predictions
ingested by `ingest_roof_materials.py` (several materials with coverage) they are answerable.

Check the reference queries on your graph before spending LLM time (no LLM needed):

```bash
python eval/tool_comparison/queries/eval_llm_queries.py --solutions-only
```

## Batch LLM runner

`eval_llm_queries.py` sends each question through LangChain's `GraphCypherQAChain`
(question to Cypher to answer) and records the generated Cypher, the answer, the number of
result rows, the time and any error in a JSON report.

```bash
pip install -e ".[llm]"        # langchain, langchain-neo4j, langchain-ollama, langchain-anthropic

# local model through Ollama (default http://localhost:11434)
python eval/tool_comparison/queries/eval_llm_queries.py --backend ollama --model gemma4:31b --solutions
# Anthropic API (needs ANTHROPIC_API_KEY; schema, questions and result rows leave the machine)
python eval/tool_comparison/queries/eval_llm_queries.py --backend claude --model claude-sonnet-4-6 --solutions
# a few questions only
python eval/tool_comparison/queries/eval_llm_queries.py --only C02,E07 --solutions
```

Connection settings are the ones of the pipeline scripts (`pykci_runtime.py`): `NEO4J_URI`
(or the older `NEO4J_URL`), `NEO4J_USER`, `NEO4J_PASSWORD`, default
`bolt://localhost:7687` with `neo4j` / `neo4jneo4j`. The Ollama URL comes from `--ollama-url`
or `OLLAMA_BASE_URL`, the default Ollama model from `--model` or `DEFAULT_MODEL`.

| Option | Default | Effect |
| ------ | ------- | ------ |
| `--backend ollama\|claude` | `ollama` | LLM backend |
| `--model` | `gemma4:31b` / `claude-sonnet-4-6` | Model name |
| `--num-ctx` | `65536` | Ollama context window (`GRAPH.md` + `CYPHER.md` take about 30k tokens) |
| `--database` | `neo4j` | Neo4j database |
| `--top-k` | `500` | Result rows handed to the LLM for the answer |
| `--timeout` | `300` | Transaction timeout (s) for every query |
| `--only C02,E07` | all 20 | Run only these questions |
| `--solutions` | off | Grade the generated queries (below) |
| `--solutions-only` | off | Run only the reference queries, no LLM |
| `--round` | `1` | Decimals numbers are rounded to before comparing |
| `--output` | `eval/results/llm_queries_<model>_<timestamp>.json` | JSON report path |

**Read-only.** Every query, generated or reference, runs in a Neo4j READ session, so the
database refuses any write the model generates (the run records it as an error).
`GraphCypherQAChain` will not start without `allow_dangerous_requests=True`; the runner sets it
only together with these READ sessions. Where your Neo4j edition has roles (Enterprise, Aura),
also connect with a read-only user (the built-in `reader` role). Community Edition has no roles,
so there the READ session is the only guard: run the benchmark against a copy of the graph.

**Provenance-aware answers.** The answer prompt names the source layers of
`pykci_provenance.py` (authoritative, crowd-sourced, ML-predicted, reconstructed, sensed,
derived) and how to recognise them by key, and asks the model to say where each value comes
from, to present predictions as predictions, and to treat a missing prediction as unknown.

## Grading (`--solutions`)

For each question the runner runs the reference query of the same number (C01 is section 1 of
`queries_citizen_solutions.txt`, E01 section 1 of `queries_expert_solutions.txt`) read-only and
compares its full result with the full result of the generated query (not only the `--top-k`
rows the LLM saw):

- numbers are rounded to `--round` decimals, lists and maps compared without order;
- a row is the multiset of its values: column names and column order are ignored;
- the rows are compared without order.

| Grade | Meaning |
| ----- | ------- |
| `exact` | same rows |
| `match` | same number of rows, each paired with a row whose values contain, or are contained in, its own (one side has extra or fewer columns) |
| `covers` | every reference row paired, the generated result has more rows |
| `partial` | some reference rows paired |
| `mismatch` | no reference row paired |
| `error` / `no_query` | the generated query failed, or the model produced none |
| `reference_error` | the reference query failed on this graph |

The JSON report holds the grade of each question with both row counts and a sample of each
result; a Markdown summary with one line per question is written beside it (same name, `.md`).

The grade is a screening aid, not a verdict. A `LIMIT` over tied values can pick different
rows, a percentage written as 0.558 instead of 55.8 counts as different, and subset pairing
can pair a row that shares only one value with the reference. It also does not judge the
answer text (for example whether it names the source of each value). Read the answers and the
Cypher in the report before drawing conclusions.
