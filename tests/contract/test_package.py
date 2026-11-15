"""pykci as a library: every module imports cleanly, importing does nothing but define
names, and the small modules other programs read (provenance rules, CRS names, runtime
settings) do not drag in the pipeline's heavy dependencies."""

import sys

import pytest

from pykci.testing import coverage
from pykci.testing.plugin import run

MODULES = coverage.modules(__import__("pathlib").Path(__file__).resolve().parents[2] / "src" / "pykci", "pykci")


@pytest.mark.parametrize("module", MODULES)
def test_import_has_no_side_effects(module, tmp_path):
    """Importing a module must not parse arguments, print, open a connection or write files:
    the commands do their work under `if __name__ == "__main__"` or in main()."""
    code = (f"import sys; sys.argv = ['x', '--definitely-not-an-option']; import {module}; "
            "import threading; assert threading.active_count() == 1, 'import started a thread'")
    result = run([sys.executable, "-c", code], cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout == "", f"import of {module} prints: {result.stdout[:200]!r}"
    assert not list(tmp_path.iterdir()), f"import of {module} writes files"


def _imported_by(module: str, tmp_path) -> set[str]:
    code = f"import sys; import {module}; print('\\n'.join(sorted(m.split('.')[0] for m in sys.modules)))"
    result = run([sys.executable, "-c", code], cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    return set(result.stdout.split())


HEAVY = {"lxml", "neo4j", "numpy", "shapely", "pyproj", "mapbox_earcut"}


@pytest.mark.parametrize("module", ["pykci", "pykci.provenance", "pykci.runtime", "pykci.crs",
                                    "pykci.geometry", "pykci.cli"])
def test_light_modules_stay_light(module, tmp_path):
    """A program that only needs the provenance rules or a CRS name must not pay for the
    CityGML codec. (pykci.crs and pykci.runtime import pyproj / neo4j inside the functions
    that need them.)"""
    assert not (_imported_by(module, tmp_path) & HEAVY)


def test_package_import_imports_no_submodule(tmp_path):
    loaded = _imported_by("pykci", tmp_path)
    result = run([sys.executable, "-c", "import sys, pykci; print([m for m in sys.modules if m.startswith('pykci.')])"],
                 cwd=tmp_path)
    assert result.stdout.strip() == "[]", loaded


def test_no_undefined_names(repo):
    """A name used without its import fails only when its line runs. Reading the source finds
    it everywhere at once: the package, the research scripts, the tests."""
    from pykci.testing import lint

    assert lint.undefined_names(repo / "src", repo / "eval", repo / "db-llm", repo / "tests") == []
