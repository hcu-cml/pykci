"""Recorded results a test compares with: what the graph holds after a pipeline step, the
schema a command writes, the fields of an output file.

A snapshot is a JSON file under tests/data. check() fails when the result differs and says
what differs. A deliberate change (a new label, a new property, a new index) is recorded by
running the test once with PYKCI_UPDATE_SNAPSHOTS=1; the changed file then shows the
extension in the diff of the commit, next to the code and the documentation that describe it.
"""

import json
import os
from pathlib import Path

ENV_UPDATE = "PYKCI_UPDATE_SNAPSHOTS"


def updating() -> bool:
    return os.environ.get(ENV_UPDATE, "").lower() in ("1", "true", "yes")


def dump(data) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _differences(recorded, actual, path="") -> list[str]:
    if isinstance(recorded, dict) and isinstance(actual, dict):
        out = []
        for key in sorted(set(recorded) | set(actual), key=str):
            here = f"{path}/{key}"
            if key not in actual:
                out.append(f"- {here}  (recorded, now missing)")
            elif key not in recorded:
                out.append(f"+ {here}  (new: {json.dumps(actual[key], ensure_ascii=False)[:80]})")
            else:
                out += _differences(recorded[key], actual[key], here)
        return out
    if isinstance(recorded, list) and isinstance(actual, list) and all(
            isinstance(v, (str, int, float)) for v in recorded + actual):
        gone, new = [v for v in recorded if v not in actual], [v for v in actual if v not in recorded]
        return ([f"- {path}: {gone}"] if gone else []) + ([f"+ {path}: {new}"] if new else [])
    if recorded != actual:
        return [f"~ {path}: {json.dumps(recorded, ensure_ascii=False)[:80]} -> "
                f"{json.dumps(actual, ensure_ascii=False)[:80]}"]
    return []


def orphans(data_dir: Path, tests_dir: Path) -> list[str]:
    """Snapshot files under `data_dir` that no test file names (by the file name without
    .json): the test that compared with them is gone, so they would never be checked again."""
    text = " ".join(p.read_text(encoding="utf-8") for p in Path(tests_dir).rglob("*.py"))
    return sorted(p.relative_to(data_dir).as_posix() for p in Path(data_dir).rglob("*.json")
                  if "fixtures" not in p.relative_to(data_dir).parts and p.stem not in text)


def check(path: Path, actual) -> None:
    """Assert that `actual` (JSON-serialisable) equals the snapshot at `path`; with
    PYKCI_UPDATE_SNAPSHOTS=1 write it instead."""
    path = Path(path)
    actual = json.loads(dump(actual))           # what a reader of the file gets back
    if updating():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(actual), encoding="utf-8", newline="\n")
        return
    assert path.is_file(), (f"no snapshot {path.name}: record it with {ENV_UPDATE}=1 "
                            "and review the new file")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    differences = _differences(recorded, actual)
    assert not differences, (
        f"{path.name} no longer matches ({len(differences)} difference(s)):\n  "
        + "\n  ".join(differences[:40])
        + f"\nIf the change is intended, record it with {ENV_UPDATE}=1, review the diff of the "
          "snapshot and describe the change in the documentation.")
