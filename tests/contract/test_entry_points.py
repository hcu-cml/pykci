"""pykci runs on its own, in every way it is documented to:

  - installed: the `pykci` command and `python -m pykci.<module>`
  - from a plain checkout, without installing: `python ingest_citygml.py …`,
    `python pykci_cli.py …`, `python tools/validate_citygml.py …` (the shims of
    _pykci_shim.py), and `import ingest_citygml` for code written before the package

The checkout tests run Python without the site directory (-S), so the editable install of
this environment is invisible and only the shim can make `pykci` importable; the
third-party packages are put back with PYTHONPATH.
"""

import importlib
import importlib.metadata
import sys
import sysconfig
from pathlib import Path

import pytest

from pykci.testing import commands
from pykci.testing.plugin import run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import _pykci_shim  # noqa: E402  (the shim helper lives at the repository root, outside the package)

sys.path.remove(str(ROOT))

ROOT_SHIMS = sorted(p for p in ROOT.glob("*.py") if p.name != "_pykci_shim.py")
TOOL_SHIMS = sorted((ROOT / "tools").glob("*.py"))
SHIMS = ROOT_SHIMS + TOOL_SHIMS
# modules that are libraries only (no `python file.py` use): imported, never run
LIBRARY_ONLY = {"pykci_runtime", "pykci_provenance", "citygml_profiles", "citygml_writer"}


def _checkout_env() -> dict:
    """An environment in which pykci is not installed but its dependencies are."""
    return {"PYTHONPATH": sysconfig.get_paths()["purelib"], "PYTHONNOUSERSITE": "1"}


def _ids(paths):
    return [p.relative_to(ROOT).as_posix() for p in paths]


def test_every_root_python_file_is_a_shim():
    """Code lives in src/pykci; a .py file at the root or in tools/ is a forwarder."""
    for path in SHIMS:
        text = path.read_text(encoding="utf-8")
        assert "forward(__name__, __file__)" in text and len(text.splitlines()) <= 10, path.name


@pytest.mark.parametrize("shim", SHIMS, ids=_ids(SHIMS))
def test_shim_points_at_a_module_of_the_package(shim):
    assert importlib.util.find_spec(_pykci_shim.target(str(shim))) is not None


@pytest.mark.slow
@pytest.mark.parametrize("shim", [s for s in SHIMS if s.stem not in LIBRARY_ONLY],
                         ids=_ids([s for s in SHIMS if s.stem not in LIBRARY_ONLY]))
def test_script_runs_from_a_checkout_without_installing(shim, tmp_path):
    result = run([sys.executable, "-S", shim, "--help"], env=_checkout_env(), cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    assert "usage" in result.stdout.lower()


@pytest.mark.parametrize("shim", ROOT_SHIMS, ids=_ids(ROOT_SHIMS))
def test_old_module_name_imports_the_package_module(shim, tmp_path):
    """`import ingest_citygml` (code from before the package) gives pykci.ingest_citygml itself,
    so every name, private ones included, is the same object."""
    target = _pykci_shim.target(str(shim))
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import {shim.stem} as old; "
            f"import importlib; new = importlib.import_module({target!r}); "
            "assert old is new, (old, new); print(old.__name__)")
    result = run([sys.executable, "-S", "-c", code], env=_checkout_env(), cwd=tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip() == target


def _script_modules():
    """The modules of the package that are also commands (they have a __main__ block)."""
    package = ROOT / "src" / "pykci"
    return sorted("pykci." + p.stem for p in package.glob("*.py")
                  if 'if __name__ == "__main__":' in p.read_text(encoding="utf-8"))


@pytest.mark.slow
@pytest.mark.parametrize("module", _script_modules())
def test_help_survives_a_console_that_cannot_show_every_character(module):
    """Help texts hold arrows and umlauts. On a Windows cp1252 console, or a pipe without an
    encoding, printing them must not end the command with UnicodeEncodeError: every command
    calls pykci.runtime.console() before it prints."""
    assert commands.legacy_console_problem(module) is None


def test_console_script_is_installed():
    assert commands.console_script("pykci") == "pykci.cli:main"


@pytest.mark.slow
def test_python_dash_m(tmp_path):
    result = run([sys.executable, "-m", "pykci.cli", "--help"], cwd=tmp_path)
    assert result.returncode == 0 and "ingest-citygml" in result.stdout


def test_version_is_the_package_version():
    import pykci

    assert importlib.metadata.version("pykci") == pykci.__version__
