"""Keeps the entry points of the time before pykci was a package.

The pipeline scripts used to be files at the repository root (`python ingest_citygml.py …`,
`import ingest_citygml`); the code is now the package in src/pykci/. Every old file name is
still there as a shim that calls forward():

  - run as a script, it runs the package module as __main__ with the same arguments;
  - imported, the old module name becomes an alias of the package module (same object, so
    private names resolve too).

This works from a plain checkout, without `pip install`: forward() puts src/ on the path.
Nothing here is installed; an installation has the `pykci` package and the `pykci` command.
New code imports `pykci.<module>`.
"""

import importlib
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# old file name (without .py) → module of the package, where the name changed
RENAMED = {
    "pykci_cli": "cli",
    "pykci_runtime": "runtime",
    "pykci_provenance": "provenance",
}


def target(file: str) -> str:
    """The package module a shim file stands for."""
    stem = Path(file).stem
    return "pykci." + RENAMED.get(stem, stem)


def forward(name: str, file: str) -> None:
    """Call as `forward(__name__, __file__)` from a shim."""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    module = target(file)
    if name == "__main__":
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    else:
        sys.modules[name] = importlib.import_module(module)
