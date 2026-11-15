"""Test doubles of the unit tests: a graph that answers from canned rows, so code that talks
to Neo4j can be tested for what it asks and what it does with the answers."""

import pytest


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def data(self):
        return self.rows

    def single(self):
        return self.rows[0] if self.rows else None

    def consume(self):
        return None


class FakeGraph:
    """Stands in for a Neo4j driver and for its session (session() returns the same
    object). run(query, **params) is recorded in `calls` and answered with the rows of the
    first key of `answers` that occurs in the query; an exception as answer is raised; a
    query that matches no key returns no rows."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def session(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def run(self, query, **params):
        self.calls.append((query, params))
        for needle, answer in self.answers.items():
            if needle in query:
                if isinstance(answer, Exception):
                    raise answer
                return FakeResult(answer)
        return FakeResult([])

    @property
    def queries(self):
        """The queries asked so far, each on one line."""
        return [" ".join(q.split()) for q, _ in self.calls]


@pytest.fixture
def fake_graph():
    """fake_graph({text in a query: rows or an exception}) → a FakeGraph."""
    return FakeGraph
