"""Names that are used but defined nowhere: the mistake that moving code leaves behind.

When a function moves to a shared module, every caller needs the import. A caller that was
missed still imports, still shows its `--help`, and fails with NameError only when the line
runs, perhaps in a path no test reaches. undefined_names() finds them by reading the source
(ruff, rules F821 to F823), so the suite fails at once.
"""

import sys
from pathlib import Path

from pykci.testing.plugin import run

RULES = "F821,F822,F823"       # undefined name, undefined name in __all__, used before assignment


def undefined_names(*paths: Path) -> list[str]:
    """`path:line:column: rule message` for every undefined name in the Python files under
    the given paths."""
    result = run([sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--select", RULES,
                  "--output-format", "concise", *paths])
    if "No module named ruff" in result.stderr:
        raise RuntimeError("ruff is needed for this check: pip install -e '.[test]'")
    return [line for line in result.stdout.splitlines() if any(f" {rule} " in line for rule in RULES.split(","))]
