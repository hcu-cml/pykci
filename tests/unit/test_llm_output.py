"""pykci.llm_output: an answer of a language model without its reasoning blocks."""

from pykci.llm_output import THINK_BLOCK, without_reasoning


def test_reasoning_blocks_are_removed():
    answer = "<think>\nthe user wants roofs\n</think>MATCH (b:Building) RETURN b.id"
    assert without_reasoning(answer) == "MATCH (b:Building) RETURN b.id"
    assert without_reasoning("a <THINK>x</THINK> b <think>y</think> c") == "a  b  c"


def test_a_block_that_never_closes_takes_the_rest_with_it():
    assert without_reasoning("RETURN 1 <think>and then the model ran out of tok") == "RETURN 1 "
    assert without_reasoning("<think>only thinking") == ""


def test_text_without_reasoning_is_unchanged():
    assert without_reasoning("MATCH (n) RETURN count(n)") == "MATCH (n) RETURN count(n)"
    assert without_reasoning("") == ""


def test_the_pattern_spans_lines():
    assert THINK_BLOCK.sub("", "<think>line 1\nline 2</think>done") == "done"


def test_the_minimal_query_script_uses_it(repo):
    text = (repo / "db-llm" / "query_ollama_min.py").read_text(encoding="utf-8")
    assert "from pykci.llm_output import THINK_BLOCK, without_reasoning" in text
    assert "<think>.*?</think>" not in text
