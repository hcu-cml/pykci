"""Repeated code in Python sources: the same function body, the same table, or a function
copied and slightly changed, in more than one place.

The rule is one home per piece of code: what two modules need lives in one of them, or in a
shared module, and the other imports it. find() is what tests/contract/test_no_repeated_code.py
runs over the sources, so a copy fails the suite.

What counts
    identical   two functions (also nested ones and methods) whose bodies are the same syntax
                tree, docstrings aside, of at least `min_nodes` nodes (25 is a body of about
                three statements: shorter ones repeat by nature)
    table       two module-level literals (dict, list, tuple, set, call) with the same content,
                of at least `min_table_nodes` nodes
    similar     two functions of the same name (leading underscores aside) in different files
                whose source text is at least `similar` alike (0.9: copied, then edited)

What does not: the same name with different behaviour, and anything below the sizes above.
"""

import ast
import difflib
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = frozenset({".venv", "venv", "node_modules", "__pycache__", ".git", "build", "dist",
                       ".ruff_cache", ".pytest_cache"})
# names every script has, with nothing shared behind them
COMMON_NAMES = frozenset({"main", "__init__", "parse_args", "run", "close", "wrapper", "inner"})


@dataclass(frozen=True)
class Place:
    source: str         # the name the caller gave the source tree
    file: str           # path relative to that tree
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.source}:{self.file}:{self.line} {self.name}"


@dataclass(frozen=True)
class Clone:
    kind: str           # identical | table | similar
    size: int           # syntax-tree nodes (of the smaller one for `similar`)
    places: tuple[Place, ...]
    ratio: float = 1.0

    def __str__(self) -> str:
        head = f"{self.kind} ({self.size} nodes" + (f", {self.ratio:.2f} alike)" if self.kind == "similar" else ")")
        return head + "".join(f"\n    {p}" for p in self.places)


def python_files(root: Path, skip: frozenset[str] = SKIP_DIRS, exclude: tuple[str, ...] = ()):
    """The .py files under `root`, without the skipped directories and without the relative
    paths (posix) that start with one of `exclude`."""
    root = Path(root)
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if set(rel.parts) & skip or rel.as_posix().startswith(exclude):
            continue
        yield p


def _body(node) -> list:
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _digest(nodes) -> str:
    text = "\n".join(ast.dump(n, annotate_fields=False, include_attributes=False) for n in nodes)
    return hashlib.sha1(text.encode()).hexdigest()


def _size(nodes) -> int:
    return sum(1 for n in nodes for _ in ast.walk(n))


def find(sources: dict[str, Path], *, min_nodes: int = 25, min_table_nodes: int = 12,
         similar: float = 0.9, exclude: dict[str, tuple[str, ...]] | None = None) -> list[Clone]:
    """Every clone among the Python files of `sources` (name → directory); `exclude` gives,
    per source, relative path prefixes to leave out (generated files, vendored code)."""
    exclude = exclude or {}
    funcs = []      # (Place, digest, size, text)
    tables = []     # (Place, digest, size)
    for source, root in sources.items():
        for path in python_files(root, exclude=exclude.get(source, ())):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
            rel = path.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = _body(node)
                    funcs.append((Place(source, rel, node.lineno, node.name), _digest(body), _size(body),
                                  ast.get_source_segment(text, node) or ""))
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(
                        node.value, (ast.Dict, ast.Tuple, ast.List, ast.Set, ast.Call)):
                    size = _size([node.value])
                    if size >= min_table_nodes:
                        name = ", ".join(ast.unparse(t) for t in node.targets)
                        tables.append((Place(source, rel, node.lineno, name), _digest([node.value]), size))

    clones = []
    groups = defaultdict(list)
    for place, digest, size, _ in funcs:
        if size >= min_nodes:
            groups[digest].append((place, size))
    identical = set()
    for group in groups.values():
        if len(group) > 1:
            clones.append(Clone("identical", group[0][1], tuple(p for p, _ in group)))
            identical.update(p for p, _ in group)

    groups = defaultdict(list)
    for place, digest, size in tables:
        groups[digest].append((place, size))
    for group in groups.values():
        if len(group) > 1:
            clones.append(Clone("table", group[0][1], tuple(p for p, _ in group)))

    by_name = defaultdict(list)
    for f in funcs:
        if f[0].name not in COMMON_NAMES and f[2] >= min_nodes:
            by_name[f[0].name.lstrip("_")].append(f)
    for group in by_name.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if (a[0].source, a[0].file) == (b[0].source, b[0].file) or a[1] == b[1]:
                    continue        # one file, or already reported as identical
                ratio = difflib.SequenceMatcher(None, a[3], b[3]).ratio()
                if ratio >= similar:
                    clones.append(Clone("similar", min(a[2], b[2]), (a[0], b[0]), ratio))
    return sorted(clones, key=lambda c: (-c.size, str(c.places[0])))


def report(clones: list[Clone]) -> str:
    """The failure message of the no-repeated-code test."""
    if not clones:
        return "no repeated code"
    return (f"{len(clones)} piece(s) of repeated code. Give each one home (a module both can "
            "import) and import it everywhere else:\n\n"
            + "\n\n".join(str(c) for c in clones))
