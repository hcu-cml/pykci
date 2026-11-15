"""The end-to-end harness of the CityGML codec (eval/citygml3_tests.py) as part of the suite.

The harness ingests the CityGML 3.0 fixtures and the FZK-Haus models, exports them as 3.0 and
2.0, validates every export against the OGC schemas and counts what a round trip loses. It
is the project's own acceptance test of the codec; this test runs it on the test database
and requires every one of its checks to pass, instead of repeating them."""

import json
import sys

import pytest

from pykci import validate_citygml
from pykci.testing import database
from pykci.testing.plugin import run

# the checks that need no data beyond the repository (T1-T4, T7-T10 take real city files)
EXPECTED = {"T5_kitchen_sink", "T6_tile_merge", "T11_dynamizer", "T12_id_scope", "T13_fzk_haus",
            "T14_export_by_source_file"}


@pytest.mark.slow
def test_codec_harness_passes(neo4j_settings, neo4j_driver, repo, tmp_path):
    if not (validate_citygml.DEFAULT_SCHEMA_DIR / validate_citygml.BUNDLE).is_dir():
        pytest.skip("no XSD bundle: run `pykci validate-citygml --setup` once")
    s = neo4j_settings
    try:
        result = run([sys.executable, repo / "eval" / "citygml3_tests.py", "--uri", s.uri, "--user", s.user,
                      "--password", s.password, "--wipe", "--out", tmp_path], cwd=repo, timeout=3600)
    finally:
        database.reclaim(neo4j_driver)          # the harness empties the database with its own query
    report = tmp_path / "citygml3_tests.json"
    assert report.is_file(), (result.stdout + result.stderr)[-2000:]
    results = json.loads(report.read_text(encoding="utf-8"))
    failed = {name: {k: v for k, v in r.items() if k not in ("counts", "expected", "loss")}
              for name, r in results.items() if not r["ok"]}
    assert not failed, json.dumps(failed, indent=1)[:3000]
    assert EXPECTED <= set(results), f"checks that did not run: {sorted(EXPECTED - set(results))}"
    assert result.returncode == 0
