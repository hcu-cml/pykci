"""
Runtime settings shared by the pykci pipeline scripts.

Three things live here so that other programs (CI jobs, containers, applications)
can drive the scripts without editing them, and so that no script
repeats them:

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

3. **One way to connect.** `add_connection_arguments(parser)` gives a command the
   `--uri` / `--user` / `--password` options with the environment's values as
   defaults, and `connect()` opens the driver for them (or for the environment).

Commands also call `console()` first thing, so that their output never fails on a
console that cannot show a character.
"""

import json
import os
import sys
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


# Driver options commands pass to connect(). Queries that probe optional structure (a label
# or property a given city may not have) make Neo4j send a notification per query.
NO_SCHEMA_NOTICES = {"notifications_disabled_classifications": {"DEPRECATION", "UNRECOGNIZED"}}
NO_NOTICES = {"notifications_min_severity": "OFF"}


def add_connection_arguments(parser) -> None:
    """--uri / --user / --password, defaulting to the connection of the environment."""
    user, password = neo4j_auth()
    parser.add_argument("--uri", default=neo4j_uri(),
                        help="Neo4j Bolt URI (env NEO4J_URI; default %(default)s)")
    parser.add_argument("--user", default=user, help="Neo4j user (env NEO4J_USER; default %(default)s)")
    parser.add_argument("--password", default=password, help="Neo4j password (env NEO4J_PASSWORD)")


def connect(uri: str | None = None, user: str | None = None, password: str | None = None, *,
            verify: bool = False, **driver_options):
    """A Neo4j driver for the given connection; what is not given comes from the
    environment. `driver_options` go to GraphDatabase.driver() (NO_SCHEMA_NOTICES,
    NO_NOTICES, max_connection_pool_size, …); `verify` checks the connection at once."""
    from neo4j import GraphDatabase

    env_user, env_password = neo4j_auth()
    driver = GraphDatabase.driver(uri or neo4j_uri(), auth=(user or env_user, password or env_password),
                                  **driver_options)
    if verify:
        driver.verify_connectivity()
    return driver


def console(encoding: str | None = None) -> None:
    """Make printing safe on any console. The pipeline prints arrows and umlauts; on a
    console that cannot encode them (Windows cp1252, a pipe without an encoding) a print
    would raise UnicodeEncodeError and end the command. After this call such characters
    are replaced instead. `encoding` also sets the encoding (reports meant to be read as
    UTF-8). A command calls this before it prints anything; library code never does."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            if encoding:
                stream.reconfigure(encoding=encoding, errors="replace")
            else:
                stream.reconfigure(errors="replace")


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
