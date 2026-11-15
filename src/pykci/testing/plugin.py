"""The pytest plugin of pykci's suite (enabled in pyproject.toml with `-p pykci.testing.plugin`).

Markers
    neo4j   the test needs the throwaway database (pykci.testing.database). Without
            PYKCI_TEST_NEO4J_URI it is skipped; with PYKCI_TEST_REQUIRE_NEO4J=1 a missing
            database fails the run, so a continuous-integration run cannot pass by skipping.
    slow    takes more than a few seconds

Fixtures
    neo4j_settings   where the test database is (uri, user, password, .env, .cli)
    neo4j_driver     a driver to it, claimed for testing (session scope)
    empty_graph      the same driver on a wiped database (per test)
    run_module       run a module as a command (`python -m <module> …`) against the test
                     database and return the CompletedProcess
"""

import os
import subprocess
import sys

import pytest

from pykci.testing import database


def pytest_configure(config):
    config.addinivalue_line("markers", "neo4j: needs the throwaway Neo4j (PYKCI_TEST_NEO4J_URI)")
    config.addinivalue_line("markers", "slow: takes more than a few seconds")


def pytest_collection_modifyitems(config, items):
    # anything that uses the database fixtures carries the marker, so `-m "not neo4j"` selects
    # exactly the tests that run without a database
    for item in items:
        if {"neo4j_settings", "neo4j_driver", "empty_graph", "run_module"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.neo4j)


@pytest.fixture(scope="session")
def neo4j_settings():
    s = database.settings()
    if s is None:
        if database.required():
            pytest.fail(f"{database.ENV_REQUIRE} is set but {database.ENV_URI} is not")
        pytest.skip(f"no test database ({database.ENV_URI} is not set)")
    return s


@pytest.fixture(scope="session")
def neo4j_driver(neo4j_settings):
    try:
        driver = database.connect(neo4j_settings)
    except Exception as exc:  # noqa: BLE001 (any connection failure)
        if database.required():
            pytest.fail(f"test database unreachable: {exc}")
        pytest.skip(f"test database unreachable: {exc}")
    database.claim(driver)
    yield driver
    driver.close()


@pytest.fixture
def empty_graph(neo4j_driver):
    database.wipe(neo4j_driver)
    return neo4j_driver


def run(args, *, env=None, cwd=None, timeout=900) -> subprocess.CompletedProcess:
    """Run a command with UTF-8 output captured (the pipeline prints arrows and umlauts)."""
    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **(env or {})}
    return subprocess.run([str(a) for a in args], env=full_env, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture(scope="session")
def run_module(neo4j_settings, tmp_path_factory):
    """run_module("pykci.ingest_citygml", path, …) → CompletedProcess, on the test database.
    `ok=True` (default) asserts exit status 0 and shows the end of the output otherwise.
    The commands write their reports relative to the working directory; unless `cwd` is
    given that is a scratch folder, so a test run leaves nothing in the repository."""
    scratch = tmp_path_factory.mktemp("commands")

    def _run(module, *args, cwd=None, timeout=900, ok=True):
        result = run([sys.executable, "-m", module, *args], env=neo4j_settings.env, cwd=cwd or scratch,
                     timeout=timeout)
        if ok:
            tail = (result.stdout + result.stderr)[-2000:]
            assert result.returncode == 0, f"{module} exits {result.returncode}: {tail}"
        return result
    return _run
