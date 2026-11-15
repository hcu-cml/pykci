"""One way to reach the database: pykci.runtime.

A module that names a Bolt address or a password itself ignores NEO4J_URI / NEO4J_USER /
NEO4J_PASSWORD and the --neo4j-* options of the command line: run against a test or a
second database it would still read, or write, the one on localhost:7687. So no source
file outside pykci/runtime.py may hold a connection literal, and every module that opens a
driver gets it from runtime.connect() (the checks are pykci.testing.connection)."""

import pytest

from pykci import runtime
from pykci.testing import connection
from pykci.testing.clones import python_files

# where a connection literal is the point: the defaults themselves, and the documentation of
# how to start a test database
ALLOWED_FILES = {"src/pykci/runtime.py", "src/pykci/testing/database.py", "src/pykci/testing/connection.py"}


def _sources(repo):
    return [path for folder in ("src/pykci", "eval", "db-llm") for path in python_files(repo / folder)
            if path.relative_to(repo).as_posix() not in ALLOWED_FILES]


def test_no_connection_literal_outside_runtime(repo):
    found = connection.literals(_sources(repo))
    assert not found, ("connection literals outside pykci/runtime.py (use runtime.connect(), "
                       "neo4j_uri(), neo4j_auth()): " + "; ".join(found))


def test_drivers_come_from_runtime(repo):
    """GraphDatabase.driver() is called in one place, runtime.connect()."""
    found = connection.driver_calls(_sources(repo))
    assert not found, "GraphDatabase.driver() outside pykci/runtime.py (use runtime.connect()): " + "; ".join(found)


@pytest.mark.parametrize("variable, value", [("NEO4J_URI", "bolt://example.invalid:1234"),
                                             ("NEO4J_URL", "bolt://example.invalid:4321")])
def test_environment_names_the_connection(monkeypatch, variable, value):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_URL", raising=False)
    monkeypatch.setenv(variable, value)
    assert runtime.neo4j_uri() == value


def test_defaults_without_environment(monkeypatch):
    for name in ("NEO4J_URI", "NEO4J_URL", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert runtime.neo4j_uri() == "bolt://localhost:7687"
    assert runtime.neo4j_auth() == ("neo4j", "neo4jneo4j")


def test_connection_arguments_default_to_the_environment(monkeypatch):
    import argparse

    monkeypatch.setenv("NEO4J_URI", "bolt://example.invalid:1234")
    monkeypatch.setenv("NEO4J_USER", "someone")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    parser = argparse.ArgumentParser()
    runtime.add_connection_arguments(parser)
    args = parser.parse_args([])
    assert (args.uri, args.user, args.password) == ("bolt://example.invalid:1234", "someone", "secret")
    args = parser.parse_args(["--uri", "bolt://other:1", "--user", "u", "--password", "p"])
    assert (args.uri, args.user, args.password) == ("bolt://other:1", "u", "p")


def test_connect_passes_what_is_given_and_fills_the_rest_from_the_environment(monkeypatch):
    """connect() without a server: the driver is created lazily, so its address and
    credentials can be read back without a connection."""
    monkeypatch.setenv("NEO4J_URI", "bolt://from-env.invalid:1111")
    monkeypatch.setenv("NEO4J_USER", "env-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-password")
    calls = []

    class Fake:
        @staticmethod
        def driver(uri, auth, **options):
            calls.append((uri, auth, options))
            return "driver"

    import neo4j
    monkeypatch.setattr(neo4j, "GraphDatabase", Fake)
    assert runtime.connect() == "driver"
    assert runtime.connect("bolt://given:2", "u", "p", **runtime.NO_NOTICES) == "driver"
    assert runtime.connect(password="only-this", max_connection_pool_size=7) == "driver"
    assert calls == [
        ("bolt://from-env.invalid:1111", ("env-user", "env-password"), {}),
        ("bolt://given:2", ("u", "p"), {"notifications_min_severity": "OFF"}),
        ("bolt://from-env.invalid:1111", ("env-user", "only-this"), {"max_connection_pool_size": 7}),
    ]
