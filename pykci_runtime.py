"""
Runtime settings shared by the pykci pipeline scripts.

Two things live here so that other programs (the pykci web client, CI jobs,
containers) can drive the scripts without editing them:

1. **Neo4j connection from the environment.** `NEO4J_URI` (or the older
   `NEO4J_URL` used by db-llm/), `NEO4J_USER` and `NEO4J_PASSWORD` override the
   historical defaults `bolt://localhost:7687` / `neo4j` / `neo4jneo4j`. Unset
   variables keep the defaults, so running a script by hand is unchanged.

2. **Machine-readable progress events.** With `PYKCI_PROGRESS=1` (or the
   `--progress` flag of `pykci_cli.py`) the scripts print one line per event:

       PYKCI_PROGRESS {"stage": "ingest.parse", "done": 12000, "unit": "features", ...}

   A caller parses lines starting with `PYKCI_PROGRESS ` from stdout and treats
   every other line as a human log. Without the variable `progress()` is a no-op,
   so the scripts' normal output is byte-identical to before.
"""

import json
import os
import time

DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "neo4jneo4j"

PROGRESS_PREFIX = "PYKCI_PROGRESS "


def neo4j_uri() -> str:
    return os.environ.get("NEO4J_URI") or os.environ.get("NEO4J_URL") or DEFAULT_NEO4J_URI


def neo4j_auth() -> tuple[str, str]:
    return (os.environ.get("NEO4J_USER", DEFAULT_NEO4J_USER),
            os.environ.get("NEO4J_PASSWORD", DEFAULT_NEO4J_PASSWORD))


def progress_enabled() -> bool:
    return os.environ.get("PYKCI_PROGRESS", "").lower() in ("1", "true", "yes", "jsonl")


def progress(stage: str, done=None, total=None, unit: str = None, message: str = None, **extra):
    """Emit one progress event (no-op unless PYKCI_PROGRESS is set).

    stage   : dotted step name, e.g. 'ingest.parse', 'osm.match', 'tiles.write'
    done    : units completed so far (None for a stage start/end marker)
    total   : units expected, when known (lets a caller show a percentage)
    unit    : what is counted ('features', 'tiles', 'buildings', …)
    message : short human-readable status
    extra   : any further JSON-serialisable detail (file name, layer, …)
    """
    if not progress_enabled():
        return
    event = {"stage": stage, "t": round(time.time(), 3)}
    if done is not None:
        event["done"] = done
    if total is not None:
        event["total"] = total
    if unit:
        event["unit"] = unit
    if message:
        event["message"] = message
    event.update(extra)
    # ASCII-escaped so the line survives any console/pipe encoding (Windows cp1252)
    print(PROGRESS_PREFIX + json.dumps(event, default=str), flush=True)
