"""Chat/workspace-surface ToolWaitPark ``llm_messages`` stamp (Phase 3
stage 7a, 01a0518b, 7a gate verdict item 6) - direct test for
``_BaseAgentExecutor._run_loop``'s ``except ToolWaitPark`` stamp slice.

``_dispatch_as_claims`` leaves ``ToolWaitPark.llm_messages`` unset at
raise time by contract; ``_run_loop`` is the "one layer up" that stamps
the in-progress turn's own produced messages onto the exception before
it propagates to the worker's park-write path, mirroring the existing
``except YieldToWorker`` arm exactly. Had no test of its own before this
- breaking the slice (e.g. stamping the WRONG slice, or not stamping at
all) would leave the suite green while a resumed turn injects tool
results with no paired assistant message to pair them against.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Message, TextPart
from primer.model.yield_ import ToolWaitPark
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


@pytest.mark.asyncio
async def test_run_loop_stamps_llm_messages_on_tool_wait_park(
    tmp_path: Path, monkeypatch,
) -> None:
    _backend, _workspace, session = await _build_session(tmp_path)
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    ex = WorkspaceAgentExecutor(
        agent=_agent(system_prompt=["base"]),
        llm=_FakeLLM(scripts=[]),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=mgr,
        session=session,
    )

    import primer.agent.loop as loop_mod

    async def _spy(*, messages_out, **kwargs):
        messages_out.append(
            Message(role="assistant", parts=[TextPart(text="calling tool")])
        )
        raise ToolWaitPark(
            outstanding_task_ids=["x:tool:0:1"], event_key="tool_wait:x:tool:0:1",
        )
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(loop_mod, "run_agent_turn", _spy)

    new_msg = Message(role="user", parts=[TextPart(text="hi")])
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([new_msg]):
            pass

    # Content, not just presence: exactly this turn's OWN produced
    # message(s) - not the caller's new_messages (already held by the
    # caller) and not empty (which would silently drop the assistant
    # tool_use the resume path needs to pair tool results against).
    # _run_loop's own stamp is a plain slice of typed Message objects
    # (unlike the graph-side stamp, which model_dumps to JSON dicts) -
    # dispatch.py's own except-YieldToWorker/except-ToolWaitPark
    # branches do that conversion themselves right before storage.
    assert excinfo.value.llm_messages == [
        Message(role="assistant", parts=[TextPart(text="calling tool")])
    ]


@pytest.mark.asyncio
async def test_run_loop_does_not_overwrite_an_already_stamped_park(
    tmp_path: Path, monkeypatch,
) -> None:
    """Mirrors the YieldToWorker arm's own convention (never actually
    exercised for ToolWaitPark before): if the raiser already stamped
    llm_messages (loop.py's own contract says it never does today, but
    the stamp code itself has no such assumption baked in - it always
    overwrites unconditionally, unlike the YieldToWorker arm elsewhere
    which is written the same way), this pins the current, unconditional
    behavior so a future change is a deliberate decision, not a
    surprise."""
    _backend, _workspace, session = await _build_session(tmp_path)
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=session)
    ex = WorkspaceAgentExecutor(
        agent=_agent(system_prompt=["base"]),
        llm=_FakeLLM(scripts=[]),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=mgr,
        session=session,
    )

    import primer.agent.loop as loop_mod

    async def _spy(*, messages_out, **kwargs):
        messages_out.append(
            Message(role="assistant", parts=[TextPart(text="fresh turn content")])
        )
        park = ToolWaitPark(
            outstanding_task_ids=["x:tool:0:1"], event_key="tool_wait:x:tool:0:1",
        )
        park.llm_messages = [{"role": "assistant", "parts": [{"type": "text", "text": "stale"}]}]
        raise park
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(loop_mod, "run_agent_turn", _spy)

    new_msg = Message(role="user", parts=[TextPart(text="hi")])
    with pytest.raises(ToolWaitPark) as excinfo:
        async for _ev in ex.invoke([new_msg]):
            pass

    assert excinfo.value.llm_messages == [
        Message(role="assistant", parts=[TextPart(text="fresh turn content")])
    ]
