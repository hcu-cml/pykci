"""Labels and relationship types named in the Cypher of a code base, found without running it.

The graph schema is what the code writes and reads. scan() collects every node label and
relationship type that appears in a Cypher string of the given Python files, so a test can
require that each one is documented (GRAPH.md and the documents of the features):
extending the schema then means writing it down.

Reading Cypher with regular expressions is deliberately modest: it finds the names in
ordinary patterns, `(n:Label)`, `(:A:B {…})`, `[r:TYPE]`, `[:A|B*1..3]`, `SET n:Label`,
`n:Label` in a WHERE clause. Labels built at run time (`f"(n:{label})"`) are not names in the
source and are skipped.
"""

import ast
import re
from collections import defaultdict
from pathlib import Path

# a string is taken for Cypher when it holds one of these clauses
_CLAUSE = re.compile(r"\b(MATCH|MERGE|CREATE|UNWIND|OPTIONAL MATCH|DETACH DELETE|CALL)\b")
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
# (var:Label:Other …)  (:Label …)   the label chain right after an optional variable
_NODE = re.compile(rf"\(\s*(?:{_NAME})?\s*((?::\s*{_NAME})+)")
# SET n:Label / REMOVE n:Label / WHERE n:Label / n:A|B
_PREDICATE = re.compile(rf"\b(?:{_NAME})((?::{_NAME})+)(?![A-Za-z0-9_(])")
# [r:TYPE] [:A|B*1..3] [r:A|:B]
_REL = re.compile(rf"\[\s*(?:{_NAME})?\s*:\s*({_NAME}(?:\s*\|\s*:?\s*{_NAME})*)")
PLACEHOLDER = "\u0000"      # stands for a value formatted into the string at run time


def _strings(tree):
    """(line, text) of every string in the module; a formatted value becomes PLACEHOLDER."""
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else PLACEHOLDER
                for v in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def names_in(cypher: str) -> tuple[set[str], set[str]]:
    """(labels, relationship types) named in one Cypher text."""
    labels, rels = set(), set()
    # map literals ({key: value}) and string literals would look like labels: blank them
    text = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", cypher)
    text = re.sub(r"\{[^{}]*\}", "{}", text)
    for m in _REL.finditer(text):
        rels.update(t.strip(" :") for t in m.group(1).split("|"))
    without_rels = _REL.sub("[", text)
    for pattern in (_NODE, _PREDICATE):
        for m in pattern.finditer(without_rels):
            labels.update(t.strip() for t in m.group(1).split(":") if t.strip())
    labels = {name for name in labels if PLACEHOLDER not in name}
    rels = {name for name in rels if name and PLACEHOLDER not in name}
    return labels, rels


def scan(files) -> dict[str, dict[str, list[str]]]:
    """{"labels": {name: [file:line, …]}, "relationships": {name: [file:line, …]}} for the
    Cypher strings of the given Python files."""
    found = {"labels": defaultdict(list), "relationships": defaultdict(list)}
    for path in files:
        path = Path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, text in _strings(tree):
            if not _CLAUSE.search(text):
                continue
            labels, rels = names_in(text)
            for name in labels:
                found["labels"][name].append(f"{path.name}:{line}")
            for name in rels:
                found["relationships"][name].append(f"{path.name}:{line}")
    return {kind: dict(sorted(names.items())) for kind, names in found.items()}
