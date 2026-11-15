"""A throwaway Neo4j for integration tests.

Integration tests ingest, export and delete, so they must never reach a real graph. The
rules, enforced here and nowhere else:

  1. Opt-in: the database is named by PYKCI_TEST_NEO4J_URI (with _USER / _PASSWORD). The
     NEO4J_URI of the pipeline is never used, so a shell that points at a real graph
     cannot make the tests run there.
  2. Port 7687, the usual port of a real graph, is refused.
  3. A database that holds nodes is only accepted when it carries the marker node
     (:PykciTestDatabase), which claim() writes into an empty database. A graph that
     was not created by the tests has no marker and is left alone.

Start one with Docker (plugins: APOC core and Neo4j Spatial, both in docker/plugins):

    docker create --name pykci-test-neo4j -p 127.0.0.1:17687:7687 \\
        -e NEO4J_AUTH=neo4j/pykci-test-only \\
        -e "NEO4J_dbms_security_procedures_unrestricted=apoc.*,spatial.*" \\
        -e "NEO4J_dbms_security_procedures_allowlist=apoc.*,spatial.*" neo4j:2025.10.1
    docker cp docker/plugins/apoc-2025.10.1-core.jar pykci-test-neo4j:/var/lib/neo4j/plugins/
    docker cp docker/plugins/neo4j-spatial-server-plugin-2025.10.0-with-dependencies.jar \\
        pykci-test-neo4j:/var/lib/neo4j/plugins/
    docker start pykci-test-neo4j

    PYKCI_TEST_NEO4J_URI=bolt://localhost:17687 PYKCI_TEST_NEO4J_PASSWORD=pykci-test-only pytest
"""

import os
import re
from dataclasses import dataclass

ENV_URI = "PYKCI_TEST_NEO4J_URI"
ENV_USER = "PYKCI_TEST_NEO4J_USER"
ENV_PASSWORD = "PYKCI_TEST_NEO4J_PASSWORD"
ENV_REQUIRE = "PYKCI_TEST_REQUIRE_NEO4J"     # 1: a missing database fails the run instead of skipping
MARKER = "PykciTestDatabase"


class UnsafeDatabase(RuntimeError):
    """The database named for the tests may be a real graph."""


@dataclass(frozen=True)
class Settings:
    uri: str
    user: str
    password: str

    @property
    def env(self) -> dict[str, str]:
        """The environment that points the pipeline (pykci.runtime) at this database."""
        return {"NEO4J_URI": self.uri, "NEO4J_USER": self.user, "NEO4J_PASSWORD": self.password}

    @property
    def cli(self) -> list[str]:
        """The same as global options of the pykci command line."""
        return ["--neo4j-uri", self.uri, "--neo4j-user", self.user, "--neo4j-password", self.password]


def settings() -> Settings | None:
    """The test database from the environment, or None when none is named."""
    uri = os.environ.get(ENV_URI)
    if not uri:
        return None
    check_uri(uri)
    return Settings(uri, os.environ.get(ENV_USER, "neo4j"), os.environ.get(ENV_PASSWORD, "neo4j"))


def required() -> bool:
    return os.environ.get(ENV_REQUIRE, "").lower() in ("1", "true", "yes")


def check_uri(uri: str) -> None:
    if re.search(r":7687\b", uri) or not re.search(r":\d+", uri):
        raise UnsafeDatabase(f"{ENV_URI}={uri}: port 7687 (also the default without a port) is the "
                             "usual port of a real graph; run the tests against a throwaway "
                             "database on another port")


def connect(s: Settings):
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(s.uri, auth=(s.user, s.password), notifications_min_severity="OFF")
    driver.verify_connectivity()
    return driver


def claim(driver) -> None:
    """Accept the database for testing: empty (the marker is written) or already marked."""
    with driver.session() as sess:
        nodes = sess.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        marked = sess.run("MATCH (m) WHERE $marker IN labels(m) RETURN count(m) AS n",
                          marker=MARKER).single()["n"]
        if nodes and not marked:
            raise UnsafeDatabase(f"the test database holds {nodes} nodes and no (:{MARKER}) node: it was "
                                 "not created by the tests, so it is not wiped")
        if not marked:
            sess.run(f"CREATE (:{MARKER} {{created: datetime()}})").consume()


def reclaim(driver) -> None:
    """Put the marker back after a tool under test emptied the database itself (the eval
    harness wipes with its own query). Only for a driver that claim() accepted before."""
    with driver.session() as sess:
        sess.run(f"MERGE (:{MARKER})").consume()


def wipe(driver) -> None:
    """Empty a claimed database: spatial layers, nodes, indexes and constraints."""
    claim(driver)
    with driver.session() as sess:
        try:
            layers = [r["name"] for r in sess.run("CALL spatial.layers() YIELD name RETURN name")]
        except Exception:  # noqa: BLE001 (a database without the spatial plugin)
            layers = []
        for layer in layers:
            sess.run("CALL spatial.removeLayer($layer)", layer=layer).consume()
        sess.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
        for r in sess.run("SHOW CONSTRAINTS YIELD name RETURN name").data():
            sess.run(f"DROP CONSTRAINT `{r['name']}` IF EXISTS").consume()
        for r in sess.run("SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name").data():
            sess.run(f"DROP INDEX `{r['name']}` IF EXISTS").consume()
        sess.run(f"CREATE (:{MARKER} {{created: datetime()}})").consume()


def census(driver) -> dict:
    """What the graph holds, apart from the marker: nodes per label set, relationships per
    type, property keys per label, indexes, spatial layers. Stable between two runs of the
    same pipeline, so it is what a schema snapshot records."""
    with driver.session() as sess:
        labels = {"|".join(sorted(r["l"])): r["n"] for r in sess.run(
            "MATCH (n) WHERE NOT $marker IN labels(n) RETURN labels(n) AS l, count(*) AS n", marker=MARKER)}
        rels = {r["t"]: r["n"] for r in sess.run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n")}
        keys = {r["l"]: sorted(r["k"]) for r in sess.run(
            "MATCH (n) WHERE NOT $marker IN labels(n) UNWIND labels(n) AS l UNWIND keys(n) AS k "
            "RETURN l, collect(DISTINCT k) AS k", marker=MARKER)}
        rel_keys = {r["t"]: sorted(r["k"]) for r in sess.run(
            "MATCH ()-[r]->() UNWIND keys(r) AS k RETURN type(r) AS t, collect(DISTINCT k) AS k")}
        indexes = sorted(
            f"{r['type']}:{','.join(r['labelsOrTypes'] or [])}:{','.join(r['properties'] or [])}"
            for r in sess.run("SHOW INDEXES YIELD type, labelsOrTypes, properties "
                              "WHERE type <> 'LOOKUP' RETURN type, labelsOrTypes, properties").data())
        try:
            layers = sorted(r["name"] for r in sess.run("CALL spatial.layers() YIELD name RETURN name"))
        except Exception:  # noqa: BLE001
            layers = []
    return {"labels": dict(sorted(labels.items())), "relationships": dict(sorted(rels.items())),
            "keys": dict(sorted(keys.items())), "relationship_keys": dict(sorted(rel_keys.items())),
            "indexes": indexes, "spatial_layers": layers}
