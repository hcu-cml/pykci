"""The test suite grows with the code: every module of the package is named by a test.

Adding a module (a new input format, a new export, a new analysis) without a test that at
least runs it fails here. See pykci.testing.coverage for what "named" means; the eval/
research scripts are outside the package and are covered by their own harness
(tests/integration/test_eval_harness.py)."""

from pykci.testing import coverage, snapshot


def test_every_module_is_named_by_a_test(repo):
    coverage.check(repo / "src" / "pykci", "pykci", repo / "tests")


def test_every_snapshot_is_compared_by_a_test(repo):
    """A recorded result whose test is gone would never be checked again."""
    assert snapshot.orphans(repo / "tests" / "data", repo / "tests") == []
