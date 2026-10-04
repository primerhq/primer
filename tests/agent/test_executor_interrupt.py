"""The executor side of Stop: bind the event, pass it to the loop, report that the turn was stopped.

``bind_interrupt_event`` is a post-construction setter for the same reason as
``bind_scoped_call_resolver``: dispatch builds the executor first and creates the cancel event after.
``was_interrupted`` is how dispatch learns the turn ended because of a Stop (the loop returns cleanly,
so there is no exception to read), shaped like ``last_done_reason``.

Persistence rule under test: an interrupted turn persists its COMPLETED rounds (they happened, they are
paired, and their tool calls had real side effects) and never the interrupted round's partial text.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Message, TextPart
from tests.agent.test_workspace_executor import (
    _FakeLLM,
    _agent,
    _build_session,
    _model,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


async def _executor(tmp_path: Path) -> WorkspaceAgentExecutor:
    _backend, _workspace, session = await _build_session(tmp_path)
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    return WorkspaceAgentExecutor(
        agent=_agent(system_prompt=["base"]),
        llm=_FakeLLM(scripts=[]),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=mgr,
        session=session,
    )


def _spy_loop(monkeypatch, *, produce: list[Message], interrupted: bool) -> dict:
    """Replace run_agent_turn with a fake that records its kwargs and plays one scripted turn."""
    import primer.agent.loop as loop_mod

    seen: dict = {}

    async def _fake(*, messages_out, interrupt=None, interrupted_out=None, **kwargs):
        seen["interrupt"] = interrupt
        seen["interrupted_out"] = interrupted_out
        messages_out.extend(produce)
        if interrupted and interrupted_out is not None:
            interrupted_out.append(True)
        return
        yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(loop_mod, "run_agent_turn", _fake)
    return seen


def _persisted(ex: WorkspaceAgentExecutor, monkeypatch) -> list[list[Message]]:
    calls: list[list[Message]] = []

    async def _capture(turn_messages: list[Message]) -> None:
        calls.append(list(turn_messages))

    monkeypatch.setattr(ex, "_persist_turn", _capture)
    return calls


async def _run(ex: WorkspaceAgentExecutor) -> None:
    async for _ in ex.invoke([Message(role="user", parts=[TextPart(text="hi")])]):
        pass


ROUND = [
    Message(role="assistant", parts=[TextPart(text="calling")]),
    Message(role="tool", parts=[TextPart(text="result")]),
]


async def test_the_bound_event_is_what_the_loop_is_given(tmp_path: Path, monkeypatch) -> None:
    ex = await _executor(tmp_path)
    event = asyncio.Event()
    ex.bind_interrupt_event(event)
    seen = _spy_loop(monkeypatch, produce=ROUND, interrupted=False)
    _persisted(ex, monkeypatch)

    await _run(ex)

    assert seen["interrupt"] is event


async def test_with_nothing_bound_the_loop_gets_no_event_and_nothing_changes(tmp_path: Path, monkeypatch) -> None:
    ex = await _executor(tmp_path)
    seen = _spy_loop(monkeypatch, produce=ROUND, interrupted=False)
    persisted = _persisted(ex, monkeypatch)

    await _run(ex)

    assert seen["interrupt"] is None
    assert ex.was_interrupted is False
    assert len(persisted) == 1


async def test_a_stopped_turn_is_reported_and_persists_its_completed_rounds(tmp_path: Path, monkeypatch) -> None:
    ex = await _executor(tmp_path)
    ex.bind_interrupt_event(asyncio.Event())
    _spy_loop(monkeypatch, produce=ROUND, interrupted=True)
    persisted = _persisted(ex, monkeypatch)

    await _run(ex)

    assert ex.was_interrupted is True
    assert len(persisted) == 1, "the completed round of a stopped turn must reach the model's history"
    assert [m.role for m in persisted[0] if m.role != "user"] == ["assistant", "tool"]


async def test_a_turn_stopped_before_any_round_completed_persists_nothing(tmp_path: Path, monkeypatch) -> None:
    ex = await _executor(tmp_path)
    ex.bind_interrupt_event(asyncio.Event())
    _spy_loop(monkeypatch, produce=[], interrupted=True)
    persisted = _persisted(ex, monkeypatch)

    await _run(ex)

    assert ex.was_interrupted is True
    assert persisted == []


async def test_was_interrupted_describes_only_the_latest_invoke(tmp_path: Path, monkeypatch) -> None:
    ex = await _executor(tmp_path)
    ex.bind_interrupt_event(asyncio.Event())
    _persisted(ex, monkeypatch)

    _spy_loop(monkeypatch, produce=ROUND, interrupted=True)
    await _run(ex)
    assert ex.was_interrupted is True

    _spy_loop(monkeypatch, produce=ROUND, interrupted=False)
    await _run(ex)
    assert ex.was_interrupted is False
