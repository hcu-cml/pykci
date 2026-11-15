"""No code is written twice in this repository: the package, the research scripts in eval/
and db-llm/, and the tests. pykci.testing.clones says what counts as a repeat.

When this fails, give the code one home and import it. The list below is for repeats that
are kept on purpose; each entry needs its reason, and the list should stay empty."""

from pykci.testing import clones

# (file, name) pairs of places that may repeat, with the reason
ALLOWED: dict[tuple[str, str], str] = {}

# generated files that are written by the builders and tracked as examples of their output
EXCLUDE = ("output/",)


def test_no_repeated_code(repo):
    found = [c for c in clones.find({"pykci": repo}, exclude={"pykci": EXCLUDE})
             if not all((p.file, p.name) in ALLOWED for p in c.places)]
    assert not found, clones.report(found)


def test_allowed_repeats_still_exist(repo):
    """An exception whose code is gone must be removed from the list."""
    present = {(p.file, p.name) for c in clones.find({"pykci": repo}, exclude={"pykci": EXCLUDE})
               for p in c.places}
    assert set(ALLOWED) <= present
