"""The container images in docker/ still fit the code.

Dockerfile.ingest is the benchmark image of the ingest: it installs three distributions and
copies the package. Dockerfile.query runs a query script from db-llm/. Both are built from
files of this repository, so a change of the code can break them without any test of the code
noticing: a new dependency of the ingest, a script that needs another file. These tests read
the Dockerfiles and compare them with what the code imports."""

import re
import sys

import pytest

from pykci.testing.plugin import run

# import name → distribution name, where they differ
DISTRIBUTION = {"mapbox_earcut": "mapbox-earcut", "PIL": "pillow"}


def _copied(dockerfile) -> list[str]:
    """The sources of every COPY instruction (without --from stages)."""
    sources = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY ") and "--from" not in line:
            sources += line.split()[1:-1]
    return sources


def _installed(dockerfile) -> set[str]:
    """The distributions the image installs with pip (names without version pins)."""
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    names = set()
    for line in text.splitlines():
        if "pip install" in line:
            for word in line.split("pip install", 1)[1].split():
                if not word.startswith("-"):
                    names.add(re.split(r"[<>=!~\[]", word, maxsplit=1)[0].lower())
    return names


def _third_party_imports(module: str, tmp_path) -> set[str]:
    """Distributions that importing `module` loads, beyond the standard library and pykci."""
    code = (f"import sys; before = set(sys.modules); import {module}; "
            "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
            "print(' '.join(sorted(n for n in new - set(sys.stdlib_module_names) "
            "if not n.startswith('_') and n not in ('pykci', 'cython_runtime'))))")
    result = run([sys.executable, "-c", code], cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-600:]
    return {DISTRIBUTION.get(name, name).lower() for name in result.stdout.split()}


@pytest.mark.parametrize("name", ["Dockerfile.ingest", "Dockerfile.query"])
def test_everything_an_image_copies_exists(repo, name):
    missing = [src for src in _copied(repo / "docker" / name) if not (repo / src).exists()]
    assert not missing, f"docker/{name} copies files that are not in the repository: {missing}"


def test_ingest_image_installs_what_the_ingest_imports(repo, tmp_path):
    """A dependency added to pykci.ingest_citygml (or to a module it imports) must be added
    to docker/Dockerfile.ingest too, or the benchmark image stops at the first import."""
    dockerfile = repo / "docker" / "Dockerfile.ingest"
    assert "src/pykci" in _copied(dockerfile)
    assert 'ENTRYPOINT ["python", "-m", "pykci.ingest_citygml"]' in dockerfile.read_text(encoding="utf-8")
    needed = _third_party_imports("pykci.ingest_citygml", tmp_path)
    assert needed <= _installed(dockerfile), f"not installed in the image: {sorted(needed - _installed(dockerfile))}"


def test_query_image_has_the_script_and_what_it_reads(repo):
    """db-llm/query_ollama_full.py imports its sibling ollama_session.py, takes the pykci
    package from ../src and reads GRAPH.md and CYPHER.md one level up."""
    dockerfile = repo / "docker" / "Dockerfile.query"
    copied = _copied(dockerfile)
    assert {"src/pykci", "db-llm", "GRAPH.md", "CYPHER.md"} <= set(copied)
    assert 'ENTRYPOINT ["python", "-u", "db-llm/query_ollama_full.py"]' in dockerfile.read_text(encoding="utf-8")
    script = (repo / "db-llm" / "query_ollama_full.py").read_text(encoding="utf-8")
    assert "from ollama_session import" in script and '"GRAPH.md"' in script and '"CYPHER.md"' in script


def test_what_the_query_scripts_take_from_pykci_needs_only_the_driver(repo, tmp_path):
    """The query image installs langchain and neo4j, not the pipeline's dependencies: the
    modules the scripts import from pykci must not need more."""
    session = (repo / "db-llm" / "ollama_session.py").read_text(encoding="utf-8")
    scripts = session + (repo / "db-llm" / "query_ollama_full.py").read_text(encoding="utf-8")
    modules = sorted(set(re.findall(r"^from (pykci\.[a-z_]+) import", scripts, re.M)))
    assert modules, "the query scripts no longer import pykci: update this test and Dockerfile.query"
    for module in modules:
        assert _third_party_imports(module, tmp_path) <= {"neo4j"}, module
