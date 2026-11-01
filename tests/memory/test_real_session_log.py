"""The reflection reads a REAL session log: the one the product writes for a session with a tool call and a steer (agent memory).

Real ``wake_session`` + ``run_one_session_turn`` + ``WorkspaceAgentExecutor`` over a local workspace (the harness of ``test_message_log_order``),
loaded by the real :func:`session_message_loader`. Hand-built message lists had one user message per turn; the product's log is not like that.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from primer.memory.jobs import MemoryDeps, _trivial
from primer.memory.reflect import new_turns, session_log_shapes, session_message_loader
from primer.model.chat import TextPart, ToolCallPart
from primer.model.memory import MemoryJob
from tests.memory.test_jobs import NOW
from tests.session.test_dispatch import fake_event_bus, fake_storage_provider  # noqa: F401
from tests.session.test_message_log_order import POSIX, _ans, _exec_call, _Harness


@POSIX
async def test_a_two_turn_session_with_a_tool_call_is_reflected_whole(tmp_path, fake_storage_provider, fake_event_bus):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_exec_call("c1"), _ans("DONE ONE."), _ans("DONE TWO.")]) as h:
        await h.turn("first task: make an id")
        await h.turn("second task: say thanks")
        history = await h.history()
        loader = session_message_loader(fake_storage_provider, h.shim.read_state_file)
        job = MemoryJob(
            id="reflect:a1:" + h.sid, kind="reflect", agent_id="a1", session_id=h.sid, target_turn=2, created_at=NOW, expires_at=NOW + timedelta(days=7),
        )
        messages = await loader(job)
        sliced = new_turns(messages, job)
        texts = [p.text for m in sliced for p in m.parts if isinstance(p, TextPart)]
        assert any("first task" in t for t in texts) and any("second task" in t for t in texts), (history, texts)
        assert any(isinstance(p, ToolCallPart) for m in sliced for p in m.parts), "the tool call of turn one is in the reflected range"
        deps = MemoryDeps(sp=fake_storage_provider, provider_registry=None, semantic_search_registry=None, engine=None, recorder=None, store=None,
                          now=lambda: NOW, load_messages=loader)
        assert await _trivial(deps, job) is None, "two turns with a tool call are worth a model call"


@POSIX
async def test_a_compacted_session_is_reflected_from_its_original_lines(tmp_path, fake_storage_provider, fake_event_bus):
    """A compaction marker folds the head of the log into a summary for the MODEL; nothing is deleted, and the reflection reads what was said."""
    from primer.agent.compaction import CompactionStrategy

    big = "q" * 200_000
    scripts = [_exec_call("c1"), _ans("a" * 240_000), _ans("SUMMARY"), _ans("A2.")]
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, scripts, compaction=CompactionStrategy(tail_turns=1)) as h:
        await h.turn("first task: " + big)
        await h.turn("second task: say thanks")
        assert [r for r, _t in await h.history()][0] == "assistant", "the product's own history is folded into a summary"
        loader = session_message_loader(fake_storage_provider, h.shim.read_state_file)
        job = MemoryJob(
            id="reflect:a1:" + h.sid, kind="reflect", agent_id="a1", session_id=h.sid, target_turn=2, created_at=NOW, expires_at=NOW + timedelta(days=7),
        )
        sliced = new_turns(await loader(job), job)
        texts = [p.text for m in sliced for p in m.parts if isinstance(p, TextPart)]
        assert any("first task" in t for t in texts) and any("second task" in t for t in texts)
        assert any(isinstance(p, ToolCallPart) for m in sliced for p in m.parts)
        shapes = await session_log_shapes(fake_storage_provider, h.shim.read_state_file)(job)
        assert "compaction_marker" in shapes and "user_input" in shapes, shapes
