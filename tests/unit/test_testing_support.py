"""The test support library itself (pykci.testing): the gates of the suite rest on it, so its
own behaviour is pinned here: what counts as repeated code
(pykci.testing.clones), which names a Cypher text holds (pykci.testing.cypher), how a
snapshot is compared and recorded (pykci.testing.snapshot), which modules count as tested
(pykci.testing.coverage), the command checks (pykci.testing.commands), what the documents
mention (pykci.testing.docs), the one-connection rule (pykci.testing.connection) and the
guards of the test database (pykci.testing.database)."""

import json
import textwrap

import pytest

from pykci.testing import clones, commands, coverage, cypher, database, snapshot


def _write(root, files: dict):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


# ---- clones ---------------------------------------------------------------------------------

BODY = """
    def {name}(values):
        \"\"\"{doc}\"\"\"
        total = 0
        for v in values:
            if v is not None:
                total += float(v)
        return total / max(len(values), 1)
"""


def test_identical_function_in_two_files_is_a_clone(tmp_path):
    root = _write(tmp_path, {"a.py": BODY.format(name="mean", doc="one"), "sub/b.py": BODY.format(name="_avg", doc="two")})
    (clone,) = clones.find({"repo": root})
    assert clone.kind == "identical"
    assert {str(p) for p in clone.places} == {"repo:a.py:2 mean", "repo:sub/b.py:2 _avg"}


def test_clone_across_two_source_trees(tmp_path):
    one = _write(tmp_path / "one", {"a.py": BODY.format(name="mean", doc="")})
    two = _write(tmp_path / "two", {"b.py": BODY.format(name="mean", doc="")})
    (clone,) = clones.find({"first": one, "second": two})
    assert {p.source for p in clone.places} == {"first", "second"}
    assert clones.find({"first": one}) == []            # each tree alone is clean


def test_short_functions_and_different_bodies_are_not_clones(tmp_path):
    root = _write(tmp_path, {
        "a.py": "def log(m):\n    print(m)\n\ndef f(x):\n    return [v * 2 for v in x if v]\n",
        "b.py": "def log(m):\n    print(m)\n\ndef f(x):\n    return [v + 1 for v in x if v]\n"})
    assert clones.find({"repo": root}) == []


def test_copied_and_edited_function_is_similar(tmp_path):
    edited = BODY.format(name="mean", doc="").replace("max(len(values), 1)", "max(len(values), 2)")
    root = _write(tmp_path, {"a.py": BODY.format(name="mean", doc=""), "b.py": edited})
    (clone,) = clones.find({"repo": root})
    assert clone.kind == "similar" and clone.ratio >= 0.9


def test_repeated_table_is_a_clone(tmp_path):
    table = "LAYERS = {'a': 'Authoritative', 'b': 'Crowd-sourced', 'c': 'Predicted', 'd': 'Derived', 'e': 'Sensed', 'f': 'More'}\n"
    root = _write(tmp_path, {"a.py": table, "b.py": table.replace("LAYERS", "_COPY")})
    (clone,) = clones.find({"repo": root})
    assert clone.kind == "table" and {p.name for p in clone.places} == {"LAYERS", "_COPY"}


def test_excluded_and_skipped_paths_are_not_read(tmp_path):
    body = BODY.format(name="mean", doc="")
    root = _write(tmp_path, {"a.py": body, "output/generated.py": body, ".venv/lib/x.py": body})
    assert clones.find({"repo": root}, exclude={"repo": ("output/",)}) == []
    assert len(clones.find({"repo": root})) == 1          # .venv is always skipped, output/ only on request


def test_report_names_every_place(tmp_path):
    root = _write(tmp_path, {"a.py": BODY.format(name="mean", doc=""), "b.py": BODY.format(name="mean", doc="")})
    text = clones.report(clones.find({"repo": root}))
    assert "repo:a.py:2 mean" in text and "repo:b.py:2 mean" in text and "one home" in text
    assert clones.report([]) == "no repeated code"


# ---- cypher ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text, labels, rels", [
    ("MATCH (b:Building)-[:HAS_BOUNDARY]->(s:BoundarySurface) RETURN b", {"Building", "BoundarySurface"}, {"HAS_BOUNDARY"}),
    ("MATCH (:Dataset {id: $id})<-[:PART_OF]-(n) RETURN n", {"Dataset"}, {"PART_OF"}),
    ("MERGE (n:Entity:CityObject {id: row.id})", {"Entity", "CityObject"}, set()),
    ("MATCH (a)-[r:HAS_POLYGON|HAS_SURFACE_MEMBER*1..3]->(p) RETURN p", set(), {"HAS_POLYGON", "HAS_SURFACE_MEMBER"}),
    ("MATCH (n) WHERE n:Building OR n:CityObject SET n:Entity", {"Building", "CityObject", "Entity"}, set()),
    ("MATCH (b:Building {name: 'a:b (c:d)'}) RETURN b.id AS id", {"Building"}, set()),      # text in quotes is no label
    ("RETURN {key: value, other: 1} AS m", set(), set()),                                   # a map is no label
])
def test_names_in_cypher(text, labels, rels):
    assert cypher.names_in(text) == (labels, rels)


def test_scan_reads_plain_and_formatted_strings_and_skips_run_time_labels(tmp_path):
    root = _write(tmp_path, {"m.py": '''
        LABEL = "Thing"
        A = "MATCH (b:Building)-[:PART_OF]->(d:Dataset) RETURN b"
        def q(label, key):
            return f"MATCH (n:{label} {{id: $id}})-[:HAS_PART]->(p:Part) WHERE p.{key} = 1 RETURN p"
        HELP = "not a query: (see:Above) and [x:Y]"
    '''})
    found = cypher.scan([root / "m.py"])
    assert set(found["labels"]) == {"Building", "Dataset", "Part"}
    assert set(found["relationships"]) == {"PART_OF", "HAS_PART"}
    assert found["labels"]["Building"] == ["m.py:3"]


# ---- snapshot -------------------------------------------------------------------------------

def test_snapshot_matches(tmp_path, monkeypatch):
    monkeypatch.delenv(snapshot.ENV_UPDATE, raising=False)
    path = tmp_path / "s.json"
    path.write_text(snapshot.dump({"labels": {"Building": 1}, "keys": ["a", "b"]}), encoding="utf-8")
    snapshot.check(path, {"keys": ["a", "b"], "labels": {"Building": 1}})


def test_snapshot_difference_says_what_changed(tmp_path, monkeypatch):
    monkeypatch.delenv(snapshot.ENV_UPDATE, raising=False)
    path = tmp_path / "s.json"
    path.write_text(snapshot.dump({"labels": {"Building": 1, "Road": 2}, "keys": {"Building": ["id"]}}), encoding="utf-8")
    with pytest.raises(AssertionError) as error:
        snapshot.check(path, {"labels": {"Building": 3, "Tree": 1}, "keys": {"Building": ["id", "height"]}})
    message = str(error.value)
    assert "~ /labels/Building: 1 -> 3" in message          # changed
    assert "- /labels/Road" in message                      # gone
    assert "+ /labels/Tree" in message                      # new
    assert "+ /keys/Building: ['height']" in message        # new entry of a list
    assert snapshot.ENV_UPDATE in message                   # and how to record it


def test_missing_snapshot_fails_until_recorded(tmp_path, monkeypatch):
    path = tmp_path / "new" / "s.json"
    monkeypatch.delenv(snapshot.ENV_UPDATE, raising=False)
    with pytest.raises(AssertionError, match="no snapshot"):
        snapshot.check(path, {"a": 1})
    monkeypatch.setenv(snapshot.ENV_UPDATE, "1")
    snapshot.check(path, {"b": 2, "a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    assert path.read_text(encoding="utf-8").index('"a"') < path.read_text(encoding="utf-8").index('"b"')   # sorted


# ---- coverage -------------------------------------------------------------------------------

def test_untested_modules(tmp_path):
    package = _write(tmp_path / "pkg", {"__init__.py": "", "alpha.py": "", "beta.py": "", "sub/__init__.py": "",
                                        "sub/gamma.py": "", "generated/skip.py": ""})
    tests = _write(tmp_path / "tests", {"test_a.py": "from pkg import alpha\nimport pkg.alpha\n",
                                        "test_g.py": 'run_module("pkg.sub.gamma")\n'})
    assert coverage.modules(package, "pkg", exclude=("generated/",)) == ["pkg.alpha", "pkg.beta", "pkg.sub.gamma"]
    assert coverage.untested(package, "pkg", tests, exclude=("generated/",)) == ["pkg.beta"]


# ---- commands -------------------------------------------------------------------------------

def test_command_targets(tmp_path):
    assert commands.target_problem("module", "pykci.cli") is None
    assert "not importable" in commands.target_problem("module", "pykci.no_such_module")
    (tmp_path / "tool.py").write_text("", encoding="utf-8")
    assert commands.target_problem("path", "tool.py", tmp_path) is None
    assert "is not in" in commands.target_problem("path", "missing.py", tmp_path)
    assert "unknown kind" in commands.target_problem("script", "x")


def test_undocumented_commands(tmp_path):
    doc = tmp_path / "CLI.md"
    doc.write_text("| `ingest-citygml` | ... |\n| `info` | ... |\n", encoding="utf-8")
    assert commands.undocumented(["ingest-citygml", "info"], doc) == {}
    assert commands.undocumented(["ingest-citygml", "new-command"], doc) == {"CLI.md": ["new-command"]}


# ---- database guards ------------------------------------------------------------------------

class _Session:
    def __init__(self, nodes, marked):
        self.nodes, self.marked, self.queries = nodes, marked, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.queries.append(query)
        value = self.marked if "labels(m)" in query else self.nodes
        return type("R", (), {"single": lambda self_: {"n": value}, "consume": lambda self_: None})()


class _Driver:
    def __init__(self, nodes, marked):
        self.sess = _Session(nodes, marked)

    def session(self):
        return self.sess


@pytest.mark.parametrize("uri", ["bolt://localhost:7687", "bolt://graph:7687", "neo4j://db.example.org:7687",
                                 "bolt://localhost"])
def test_usual_port_of_a_real_graph_is_refused(uri):
    with pytest.raises(database.UnsafeDatabase):
        database.check_uri(uri)


def test_other_ports_are_accepted():
    database.check_uri("bolt://localhost:17687")
    database.check_uri("bolt://127.0.0.1:7688")


def test_settings_come_only_from_the_test_variables(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://real-graph:7687")       # the pipeline's own variable is ignored
    monkeypatch.delenv(database.ENV_URI, raising=False)
    assert database.settings() is None
    monkeypatch.setenv(database.ENV_URI, "bolt://localhost:17687")
    monkeypatch.setenv(database.ENV_PASSWORD, "pw")
    s = database.settings()
    assert (s.uri, s.user, s.password) == ("bolt://localhost:17687", "neo4j", "pw")
    assert s.env == {"NEO4J_URI": "bolt://localhost:17687", "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": "pw"}
    assert s.cli == ["--neo4j-uri", "bolt://localhost:17687", "--neo4j-user", "neo4j", "--neo4j-password", "pw"]
    monkeypatch.setenv(database.ENV_URI, "bolt://localhost:7687")
    with pytest.raises(database.UnsafeDatabase):
        database.settings()


def test_a_graph_the_tests_did_not_create_is_never_claimed():
    driver = _Driver(nodes=17_000_000, marked=0)
    with pytest.raises(database.UnsafeDatabase, match="not wiped"):
        database.claim(driver)
    with pytest.raises(database.UnsafeDatabase):
        database.wipe(driver)
    assert not any("DELETE" in q or "CREATE" in q for q in driver.sess.queries)


def test_an_empty_database_is_claimed_with_the_marker():
    driver = _Driver(nodes=0, marked=0)
    database.claim(driver)
    assert any(f"CREATE (:{database.MARKER}" in q for q in driver.sess.queries)


def test_a_marked_database_is_accepted_as_it_is():
    driver = _Driver(nodes=500, marked=1)
    database.claim(driver)
    assert not any("CREATE" in q for q in driver.sess.queries)


# ---- docs and connection --------------------------------------------------------------------

def test_words_and_undocumented_names(tmp_path):
    from pykci.testing import docs

    (tmp_path / "GRAPH.md").write_text("`(:Building)-[:HAS_BOUNDARY]->(:BoundarySurface)` and Dataset", encoding="utf-8")
    (tmp_path / "MORE.md").write_text("OsmFeature nodes", encoding="utf-8")
    known = docs.words([tmp_path / "GRAPH.md", tmp_path / "MORE.md"])
    assert {"Building", "HAS_BOUNDARY", "BoundarySurface", "Dataset", "OsmFeature"} <= known
    found = {"Building": ["a.py:1"], "NewLabel": ["b.py:2", "b.py:3", "b.py:4", "b.py:5"]}
    assert docs.undocumented(found, known) == {"NewLabel": ["b.py:2", "b.py:3", "b.py:4"]}


def test_connection_literals_and_driver_calls(tmp_path):
    from pykci.testing import connection

    root = _write(tmp_path, {
        "bad.py": '''
            """Docstrings may show bolt://localhost:7687 as an example."""
            from neo4j import GraphDatabase
            URI = "bolt://localhost:7687"
            AUTH = ("neo4j", "neo4jneo4j")
            def run():
                return GraphDatabase.driver(URI, auth=AUTH)
        ''',
        "good.py": '''
            from pykci.runtime import connect
            HELP = "Neo4j Bolt URI, for example bolt://localhost:7687"
            def run():
                return connect()
        '''})
    assert connection.literals([root / "bad.py"]) == ["bad.py:4 'bolt://localhost:7687'", "bad.py:5 'neo4jneo4j'"]
    assert connection.driver_calls([root / "bad.py"]) == ["bad.py:7"]
    assert connection.literals([root / "good.py"]) == [] and connection.driver_calls([root / "good.py"]) == []


def test_console_script_lookup():
    assert commands.console_script("pykci") == "pykci.cli:main"
    assert commands.console_script("no-such-command-anywhere") is None


def test_coverage_check_names_the_missing_modules(tmp_path):
    package = _write(tmp_path / "pkg", {"__init__.py": "", "alpha.py": ""})
    tests = _write(tmp_path / "tests", {"test_x.py": "import os\n"})
    with pytest.raises(AssertionError, match="modules without a test: pkg.alpha"):
        coverage.check(package, "pkg", tests)
    _write(tmp_path / "tests", {"test_alpha.py": "import pkg.alpha\n"})
    coverage.check(package, "pkg", tests)


def test_orphan_snapshots(tmp_path):
    data = tmp_path / "data"
    for name in ("graph/used.json", "graph/unused.json", "fixtures/input.json"):
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_text("{}", encoding="utf-8")
    tests = _write(tmp_path / "tests", {"test_a.py": 'snapshot.check(data_dir / "graph" / "used.json", result)\n'})
    assert snapshot.orphans(data, tests) == ["graph/unused.json"]      # fixtures are inputs, not snapshots


def test_undefined_names_are_found(tmp_path):
    """pykci.testing.lint: a call to a function whose import was forgotten."""
    from pykci.testing import lint

    root = _write(tmp_path, {
        "moved.py": "def batches(rows, size):\n    return [rows[i:i + size] for i in range(0, len(rows), size)]\n",
        "caller_ok.py": "from moved import batches\n\ndef run(rows):\n    return batches(rows, 2)\n",
        "caller_broken.py": "def run(rows):\n    return batches(rows, 2)\n"})
    found = lint.undefined_names(root)
    assert len(found) == 1 and "caller_broken.py:2" in found[0] and "batches" in found[0]
