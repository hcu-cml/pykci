"""
What the two query scripts (query_ollama_full.py, query_ollama_min.py) share: where Neo4j and
Ollama are, the check that the model is there, running a query and printing its rows.

The Neo4j connection is the one of the pipeline (pykci.runtime): NEO4J_URI (or the older
NEO4J_URL), NEO4J_USER and NEO4J_PASSWORD, with the same defaults. Importing this module
makes the pykci package importable: from a checkout it is found in ../src; a container needs
the package (pykci/runtime.py, pykci/llm_output.py) next to the scripts.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pykci.runtime import connect, neo4j_auth, neo4j_uri  # noqa: E402,F401

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek-r1:14b")


def check_ollama(base_url: str, model: str) -> bool:
    """Whether Ollama answers at `base_url` and has `model`; says what to do otherwise."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
        available = [m["name"] for m in data.get("models", [])]  # full names, with tag
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
    """(rows, seconds) of one query."""
    with driver.session() as session:
        t0 = time.perf_counter()
        rows = [dict(r) for r in session.run(query)]
        elapsed = time.perf_counter() - t0
    return rows, elapsed


def print_rows(rows: list[dict]) -> None:
    """A plain table of the first 50 rows."""
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
