"""A Stop does not outlive the park it was pressed against.

A reviewer reproduced the harm: Stop while parked on an approval, approve hours later, the approved
tool RUNS, then the continuation is killed before its first token (the leftover flag is honoured by the
first poll of the turn that runs after the resume). A later explicit human action wins over an earlier
Stop, so resuming a park clears ``interrupt_requested``.

The clear sits at the single place the pool turns a resumable park into a resume, BEFORE a handler is
selected, so it covers every kind of park (an approval or an answer on an agent session, a tool_wait
batch, and the graph variants of both, which share the two handlers). The /interrupt route refuses a
Stop on a parked session, so the flag can only be on the row through a race with the park itself.
"""

from __future__ import annotations

import pytest

import primer.toolset.misc  # noqa: F401  (registers the ask_user resume hook)
from primer.int.claim import ReleaseOutcome
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.workspace_session import WorkspaceSession
from tests.conftest import _FakeStorageProvider
from tests.worker.test_engine_session_resume import (
    _async_return,
    _build_engine,
    _build_pool,
    _claim_session,
    _make_resumable_session,
    _RecordingExecutor,
)


async def _parked_with_a_stop(sid: str, **kwargs):
    storage_provider = _FakeStorageProvider()
    sessions = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(sessions)
    pool = _build_pool(storage_provider, engine)
    row = _make_resumable_session(sid, **kwargs)
    row.interrupt_requested = True
    await sessions.create(row)
    return pool, engine, sessions


@pytest.mark.asyncio
async def test_an_approved_or_answered_park_resumes_without_the_old_stop(monkeypatch):
    """The real ask_user resume: the human's answer is injected AND the flag is gone, so the
    continuation that the next claim runs is not killed by it."""
    tool_call_id = "tc-ask-stop"
    assistant = Message(role="assistant", parts=[
        ToolCallPart(id=tool_call_id, name="_misc__ask_user", arguments={"prompt": "Name?"}),
    ])
    pool, engine, sessions = await _parked_with_a_stop(
        "sess-resume-stop", tool_name="ask_user", tool_call_id=tool_call_id,
        resume_event_payload={"response": "Alice"}, llm_messages=[assistant.model_dump(mode="json")],
    )
    executor = _RecordingExecutor()
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _w: _async_return(object()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))

    await pool._run_engine_session(await _claim_session(engine, "sess-resume-stop"))

    assert len(executor.injected) == 1, "the resume itself still happened"
    tool_part = next(p for p in executor.injected[0][-1].parts if isinstance(p, ToolResultPart))
    assert tool_part.error is False
    assert (await sessions.get("sess-resume-stop")).interrupt_requested is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, blob",
    [
        pytest.param("_resume_engine_session", None, id="approval-or-answer-agent-and-graph"),
        pytest.param(
            "_resume_engine_tool_wait",
            {"kind": "tool_wait", "outstanding_task_ids": ["t1"], "event_key": "tool_wait:t1"},
            id="tool-wait-agent-and-graph",
        ),
    ],
)
async def test_the_flag_is_cleared_before_any_resume_handler_runs(monkeypatch, handler, blob):
    sid = f"sess-resume-{handler}"
    pool, engine, sessions = await _parked_with_a_stop(
        sid, tool_name="ask_user", tool_call_id="tc", resume_event_payload={"response": "x"},
        parked_state_blob=blob,
    )
    seen: dict = {}

    async def handler_stub(lease, session_row):
        seen["flag_when_the_handler_ran"] = (await sessions.get(sid)).interrupt_requested
        return ReleaseOutcome(success=True, drop_lease=False)

    monkeypatch.setattr(pool, handler, handler_stub)

    await pool._run_engine_session(await _claim_session(engine, sid))

    assert seen == {"flag_when_the_handler_ran": False}
