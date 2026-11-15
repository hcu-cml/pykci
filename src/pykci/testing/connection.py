"""The rule that there is one way to reach the database, pykci.runtime, as checks over the
sources (tests/contract/test_connection.py).

A module that names a Bolt address or a password itself, or builds its own driver, ignores
NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD and the --neo4j-* options: run against a test or a
second database it would still read, or write, the one on localhost:7687.
"""

import ast
from pathlib import Path

from pykci import runtime

LITERALS = ("bolt://", "bolt+s://", "bolt+ssc://", "neo4j://", "neo4j+s://", "neo4j+ssc://",
            runtime.DEFAULT_NEO4J_PASSWORD)


def _docstrings(tree) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def literals(files) -> list[str]:
    """`file:line 'text'` of every string constant that is a connection: a bare address or
    the default password. Documentation is not code: docstrings, and sentences that show an
    address as an example (a help text, an error message), are left alone."""
    found = []
    for path in files:
        path = Path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in skip:
                continue
            text = node.value.strip()
            if " " not in text and any(text == lit or text.startswith(lit) for lit in LITERALS):
                found.append(f"{path.name}:{node.lineno} {text[:60]!r}")
    return found


def driver_calls(files) -> list[str]:
    """`file:line` of every GraphDatabase.driver() / AsyncGraphDatabase.driver() call."""
    found = []
    for path in files:
        path = Path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "driver"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("GraphDatabase", "AsyncGraphDatabase")):
                found.append(f"{path.name}:{node.lineno}")
    return found
