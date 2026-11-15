"""Which modules of a package the tests exercise: the rule that the test suite grows with
the code.

A module counts as tested when a test file names it, as `package.module` (an import, a
`python -m` call, a module run through a fixture). That is a low bar on purpose: it does
not measure how well a module is tested, it makes it impossible to add one without adding
a test that at least runs it. untested() is what `tests/contract/test_every_module_is_tested.py`
asserts to be empty.
"""

import re
from pathlib import Path

from pykci.testing.clones import python_files


def modules(package_dir: Path, package: str, exclude: tuple[str, ...] = ()) -> list[str]:
    """Dotted names of the modules under `package_dir` (the directory of `package`), without
    `__init__` files and the relative paths that start with one of `exclude`."""
    names = []
    for path in python_files(package_dir, exclude=exclude):
        rel = path.relative_to(package_dir).with_suffix("")
        if rel.name == "__init__":
            continue
        names.append(".".join((package, *rel.parts)))
    return names


def untested(package_dir: Path, package: str, tests_dir: Path, exclude: tuple[str, ...] = ()) -> list[str]:
    """The modules of the package that no file under `tests_dir` names."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in python_files(tests_dir))
    return [m for m in modules(package_dir, package, exclude)
            if not re.search(rf"\b{re.escape(m)}\b", text)]


def check(package_dir: Path, package: str, tests_dir: Path, exclude: tuple[str, ...] = ()) -> None:
    """Assert that every module of the package is named by a test."""
    missing = untested(package_dir, package, tests_dir, exclude)
    assert not missing, ("modules without a test: " + ", ".join(missing)
                         + ". Add a test under tests/unit (pure functions) or tests/integration "
                           "(what needs the graph) that imports or runs each of them.")
