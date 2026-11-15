"""db-llm/ollama_session.py: what the two interactive query scripts share. The scripts need
LangChain; this module does not, so it is loaded from its file and tested on its own."""

import importlib.util

import pytest


@pytest.fixture(scope="module")
def session(repo):
    path = repo / "db-llm" / "ollama_session.py"
    spec = importlib.util.spec_from_file_location("ollama_session", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_connection_is_the_one_of_the_pipeline(session, monkeypatch):
    from pykci import runtime

    assert session.connect is runtime.connect
    monkeypatch.setenv("NEO4J_URI", "bolt://example.invalid:1234")
    assert session.neo4j_uri() == "bolt://example.invalid:1234"


def test_rows_are_printed_as_a_table(session, capsys):
    session.print_rows([{"id": "b1", "height": 12.5}, {"id": "building_2", "height": None}])
    assert capsys.readouterr().out.splitlines() == [
        "  id          height",
        "  ------------------",
        "  b1          12.5  ",
        "  building_2  None  ",
    ]


def test_only_the_first_fifty_rows_are_printed(session, capsys):
    session.print_rows([{"n": i} for i in range(60)])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 + 50 + 1
    assert lines[-1] == "  ... (60 rows total, showing first 50)"


def test_no_rows(session, capsys):
    session.print_rows([])
    assert capsys.readouterr().out == "  (no results)\n"


def test_a_query_returns_its_rows_and_the_time(session, fake_graph):
    driver = fake_graph({"RETURN 1 AS one": [{"one": 1}]})
    rows, seconds = session.run_cypher(driver, "RETURN 1 AS one")
    assert rows == [{"one": 1}] and seconds >= 0
    assert driver.queries == ["RETURN 1 AS one"]


def test_an_unreachable_ollama_is_reported(session, capsys):
    assert session.check_ollama("http://127.0.0.1:9", "some-model") is False
    out = capsys.readouterr().out
    assert "Ollama not reachable at http://127.0.0.1:9" in out and "ollama serve" in out


def test_the_scripts_take_their_helpers_from_the_shared_module(repo):
    for script in ("query_ollama_full.py", "query_ollama_min.py"):
        text = (repo / "db-llm" / script).read_text(encoding="utf-8")
        assert "from ollama_session import" in text
        for name in ("def check_ollama", "def run_cypher", "def print_rows"):
            assert name not in text
