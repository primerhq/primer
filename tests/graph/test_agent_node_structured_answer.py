"""The structured answer of a round that also carried a real tool call survives a later plain-text answer (X3.5).

The Anthropic adapter streams a ``response_format`` answer as the TEXT of a
synthetic ``structured_output`` block (review 2026-11-23, 2.2, lane F). When
that round ALSO carries a real tool call, the turn continues; the round's
text (the structured answer) is an assistant message in the produced history,
and a later round that answers in plain text used to win the node output on
its own: ``_agent_node_output`` took only the LAST assistant message, its
plain text failed the schema parse, and the structured answer was lost
(``parsed`` None). It now scans the produced assistant messages from the end
for the most recent one whose text parses as JSON.
"""

from __future__ import annotations

import pytest

from primer.graph._agent_node import _AgentNodeMixin
from primer.model.chat import Message, TextPart, ToolCallPart


def _assistant(*parts) -> Message:
    return Message(role="assistant", parts=list(parts))


def _tool_call() -> ToolCallPart:
    return ToolCallPart(id="tc1", name="ws__read", arguments={"path": "a.txt"})


@pytest.mark.asyncio
async def test_the_structured_answer_survives_a_later_plain_text_round() -> None:
    """Round 1: the real tool call AND the structured answer (streamed as text).
    Round 2: the model answers in plain text. The node output keeps the
    structured answer (the most recent assistant message that parses as the
    schema's JSON) and the plain text stays the visible answer."""
    structured = {"verdict": "approved", "reason": "within scope"}
    produced = [
        _assistant(TextPart(text="```json\n" + _dump(structured) + "\n```"), _tool_call()),
        _assistant(TextPart(text="I have reviewed the change.")),
    ]

    out = _AgentNodeMixin()._agent_node_output(
        produced, {"type": "json_schema"}, [], 0
    )

    assert out.parsed == structured, "the structured answer is kept, not lost"
    assert out.text == "I have reviewed the change."


@pytest.mark.asyncio
async def test_the_most_recent_parsable_answer_wins() -> None:
    """A structured answer in a later round supersedes an earlier one (the
    scan is from the end): the model re-answered in the schema and that is
    what the node gets."""
    first = {"verdict": "pending"}
    second = {"verdict": "approved"}
    produced = [
        _assistant(TextPart(text=_dump(first)), _tool_call()),
        _assistant(TextPart(text="checking")),
        _assistant(TextPart(text=_dump(second))),
    ]

    out = _AgentNodeMixin()._agent_node_output(
        produced, {"type": "json_schema"}, [], 0
    )

    assert out.parsed == second


@pytest.mark.asyncio
async def test_no_parsable_answer_stays_unparsed() -> None:
    """Plain text only (no structured round): ``parsed`` stays None, as
    before the fix."""
    produced = [_assistant(TextPart(text="just words"))]

    out = _AgentNodeMixin()._agent_node_output(
        produced, {"type": "json_schema"}, [], 0
    )

    assert out.parsed is None
    assert out.text == "just words"


@pytest.mark.asyncio
async def test_without_a_response_format_nothing_is_parsed() -> None:
    produced = [_assistant(TextPart(text='{"looks": "like json"}'), _tool_call())]

    out = _AgentNodeMixin()._agent_node_output(produced, None, [], 0)

    assert out.parsed is None


def _dump(obj: object) -> str:
    import json

    return json.dumps(obj)
