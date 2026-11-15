"""Fixtures of the pykci test suite. Markers and the database fixtures come from the plugin
pykci.testing.plugin (enabled in pyproject.toml).

    tests/unit         no database, no network: functions with inputs and expected outputs
    tests/contract     rules the repository keeps: every command, every module, the docs,
                       the old entry points, no repeated code
    tests/integration  the pipeline on a throwaway Neo4j (marker `neo4j`)
    tests/data         recorded results the tests compare with (README there)
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo() -> Path:
    """The repository root."""
    return ROOT


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return ROOT / "tests" / "data"


@pytest.fixture(scope="session")
def fzk_lod2() -> Path:
    """The smallest real model in the repository: one building, CityGML 2.0, LoD2."""
    return ROOT / "input" / "citygml" / "FZK-Haus" / "FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml"


@pytest.fixture(scope="session")
def citygml3_fixtures() -> Path:
    return ROOT / "eval" / "fixtures" / "citygml3"
