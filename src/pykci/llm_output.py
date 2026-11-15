"""
What a language model returns, without its reasoning.

Reasoning models (deepseek-r1 and others) wrap their thinking in <think>…</think> before
the answer. Whoever shows or runs the answer removes that first, as the query scripts in
db-llm/ do, with this one pattern.
"""

import re

# one reasoning block, across lines, whatever the case of the tag
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def without_reasoning(text: str) -> str:
    """`text` without its reasoning blocks. A block that was opened and never closed (the
    model hit its token limit while thinking) takes the rest of the text with it."""
    text = THINK_BLOCK.sub("", text)
    if "<think>" in text and "</think>" not in text:
        text = text.split("<think>", 1)[0]
    return text
