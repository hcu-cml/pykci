"""The contract of a command table (pykci.cli.COMMANDS): what every command must satisfy,
whoever adds it.

    target exists     a "module" entry is importable, a "path" entry is a file
    runs              `<cli> <command> --help` exits 0 and prints a usage text
    documented        the command name appears in the documents that list the commands
    tested            a test module names the module (see pykci.testing.coverage)

tests/contract/test_commands.py parametrises over the table and calls these, so a new
command is checked the moment it is registered.
"""

import importlib.metadata
import importlib.util
import sys
from pathlib import Path

from pykci.testing.plugin import run


def target_problem(kind: str, target: str, root: Path | None = None) -> str | None:
    """Why the entry cannot run, or None."""
    if kind == "module":
        try:
            found = importlib.util.find_spec(target)
        except (ImportError, ValueError) as exc:
            return f"module {target!r} cannot be located: {exc}"
        return None if found else f"module {target!r} is not importable"
    if kind == "path":
        return None if root and (root / target).is_file() else f"script {target!r} is not in {root}"
    return f"unknown kind {kind!r} (use 'module' or 'path')"


def help_of(cli_module: str, command: str, timeout: int = 180):
    """`python -m <cli_module> <command> --help` → CompletedProcess."""
    return run([sys.executable, "-m", cli_module, command, "--help"], timeout=timeout)


def help_problem(cli_module: str, command: str) -> str | None:
    """Why `--help` of the command is not usable, or None."""
    result = help_of(cli_module, command)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        return f"`{command} --help` exits {result.returncode}: {' | '.join(tail)}"
    if "usage" not in result.stdout.lower():
        return f"`{command} --help` prints no usage text"
    return None


def legacy_console_problem(module: str, timeout: int = 180) -> str | None:
    """Why `python -m <module> --help` fails on a console that cannot show every character
    (Windows cp1252, a pipe without an encoding), or None. Help texts hold arrows and
    umlauts; a command that prints them without pykci.runtime.console() ends with
    UnicodeEncodeError there."""
    result = run([sys.executable, "-m", module, "--help"], timeout=timeout,
                 env={"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        return f"`python -m {module} --help` exits {result.returncode} on a cp1252 console: {' '.join(tail)}"
    if "usage" not in result.stdout.lower():
        return f"`python -m {module} --help` prints no usage text"
    return None


def console_script(name: str) -> str | None:
    """The `module:function` an installed command name runs, or None when no installed
    package provides it."""
    for entry in importlib.metadata.entry_points(group="console_scripts"):
        if entry.name == name:
            return entry.value
    return None


def undocumented(commands, *documents: Path) -> dict[str, list[str]]:
    """{document name: [commands it does not mention]} for the documents that must list
    every command."""
    missing = {}
    for doc in documents:
        text = Path(doc).read_text(encoding="utf-8")
        absent = [c for c in commands if c not in text]
        if absent:
            missing[Path(doc).name] = absent
    return missing
