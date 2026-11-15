"""What the documents say, to compare with the code.

The schema tests ask one question of the graph documents (GRAPH.md and the documents of the
features): is this label, relationship type or command named anywhere? words() answers it
for a set of files; undocumented() applies it to the names a code scan found.
"""

import re
from pathlib import Path

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def words(paths) -> set[str]:
    """Every identifier-like word of the given text files."""
    text = "\n".join(Path(p).read_text(encoding="utf-8") for p in paths)
    return set(_WORD.findall(text))


def undocumented(names: dict[str, list[str]], documented: set[str], show: int = 3) -> dict[str, list[str]]:
    """The names (name → places in the code) that the documents do not mention, each with
    its first `show` places."""
    return {name: places[:show] for name, places in names.items() if name not in documented}
