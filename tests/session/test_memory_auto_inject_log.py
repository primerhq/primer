"""Agent memory's per-turn notes are never written to ``messages.jsonl`` (T11b design: the log holds no memory marker).

Real ``wake_session`` + ``run_one_session_turn`` + ``WorkspaceAgentExecutor`` over a real local workspace (the harness of
``test_message_log_order``), with a per-turn lookup bolted on: the model is shown the notes, the file gets no line for them, and a compaction over
such a session runs and writes what it writes without them.
"""
from __future__ import annotations

import shutil

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Message, TextPart
from tests.session.test_message_log_order import (  # noqa: F401
    COMPACTION_RECORDS,
    PLAIN_RECORDS,
    _Harness,
    _ans,
    _text,
    fake_event_bus,
    fake_storage_provider,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
NOTES = "Notes this agent learned from earlier runs. NOTES-BODY"


@pytest.fixture
def lookups(monkeypatch) -> list[str]:
    """Every executor built gets a lookup that answers the notes; returns the user texts it was asked about."""
    asked: list[str] = []

    async def lookup(text: str):
        asked.append(text)
        return Message(role="system", parts=[TextPart(text=NOTES)])

    real = WorkspaceAgentExecutor.__init__
    monkeypatch.setattr(WorkspaceAgentExecutor, "__init__", lambda self, *a, **kw: real(self, *a, memory_lookup=lookup, **kw))
    return asked


async def test_a_turn_with_auto_inject_persists_no_extra_line(tmp_path, fake_storage_provider, fake_event_bus, lookups):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_ans("ANSWER-ONE."), _ans("ANSWER-TWO.")]) as h:
        await h.turn("QUESTION-ONE")
        await h.turn("QUESTION-TWO")
        assert await h.records() == PLAIN_RECORDS, "the records are those of a plain turn"
        assert await h.history() == [
            ("user", "hello"), ("user", "QUESTION-ONE"), ("assistant", "ANSWER-ONE."),
            ("user", "QUESTION-TWO"), ("assistant", "ANSWER-TWO."),
        ]
        assert "NOTES-BODY" not in "\n".join(await h.lines()), "nothing of the notes is in the log"
        assert lookups == ["hello\nQUESTION-ONE", "QUESTION-TWO"], "asked with the user words of each turn"
        first, second = h.llm.calls[0]["messages"], h.llm.calls[1]["messages"]
        assert any(NOTES in _text(m) for m in first), "this turn's prompt carries the notes"
        assert any(NOTES in _text(m) for m in second)
        assert sum(NOTES in _text(m) for m in second) == 1, "and only this turn's: the first turn's are not in the history"


async def test_compaction_over_a_session_with_auto_inject_succeeds(tmp_path, fake_storage_provider, fake_event_bus, lookups):
    async with _Harness(
        tmp_path, fake_storage_provider, fake_event_bus,
        [_ans("a" * 240_000), _ans("SUMMARY"), _ans("A2.")], compaction=CompactionStrategy(tail_turns=1),
    ) as h:
        await h.turn("q" * 200_000)
        await h.turn("Q2")
        assert await h.records() == COMPACTION_RECORDS
        history = await h.history()
        assert [r for r, _t in history] == ["assistant", "user", "assistant"]
        assert history[0][1].endswith("\n\nSUMMARY") and history[1:] == [("user", "Q2"), ("assistant", "A2.")]
        assert "NOTES-BODY" not in "\n".join(await h.lines())
