"""An overflow a provider YIELDS (Ollama, Gemini) through the whole path: loop, recovery, dispatch, one terminal record.

A fatal ``Error`` event is a terminal record: yielded, it would be streamed and written as the turn's ERROR, and a turn
that recovered would end with a second terminal after it. ``run_agent_turn(intercept_context_overflow=True)`` holds an
error-only overflow stream back, ``invoke`` recovers it like a raised one, and the dispatch layer writes what ``invoke``
yields. The unit tests (``tests/agent/test_yielded_overflow_recovery.py``) pin the pieces; this pins the record the
operator's log ends up with when they are joined: ONE terminal, the DONE, and no ERROR.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from primer.model.chat import Error
from primer.model.except_ import ContextOverflowUnrecoverable
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.agent.test_overflow_recovery import GEMINI_OVERFLOW, _Executor, _FailsThenAnswers, _SpyCompaction
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)

TERMINAL = {"done", "error"}


async def _dispatch(storage, io, bus, executor, sid: str):
    async def build(_session: WorkspaceSession):
        return executor

    deps = SessionDispatchDeps(storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build)
    return await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 10.0)


def _kinds(io, sid: str) -> list[str]:
    return [json.loads(line).get("kind") for line in io.read_lines(sid)]


class TestTheLogOfARecoveredYieldedOverflow:
    async def test_the_turn_ends_with_exactly_one_terminal_record_and_it_is_the_done(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        spy = _SpyCompaction()
        llm = _FailsThenAnswers(yields=Error(code="bad_request", message=GEMINI_OVERFLOW, fatal=True), failures=1)
        sid = seeded_session.id

        outcome = await _dispatch(fake_storage_provider, fake_workspace_io, fake_event_bus, _Executor(llm, spy), sid)

        kinds = _kinds(fake_workspace_io, sid)
        assert outcome.success is True and spy.forced == 1 and llm.calls == 2, "the overflow was recovered, once"
        assert [k for k in kinds if k in TERMINAL] == ["done"], f"one terminal record, the DONE: {kinds}"
        assert "error" not in kinds, "the intercepted overflow was never recorded as the turn's ERROR"
        assert "assistant_token" in kinds, "the replay's answer is in the log"
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert (row.ended_reason, row.ended_detail) == ("completed", None), "a recovered turn completed: it did not fail"

    async def test_a_second_yielded_overflow_ends_the_turn_with_the_typed_failure_and_one_error_record(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        spy = _SpyCompaction()
        llm = _FailsThenAnswers(yields=Error(code="bad_request", message=GEMINI_OVERFLOW, fatal=True), failures=99)
        sid = seeded_session.id

        outcome = await _dispatch(fake_storage_provider, fake_workspace_io, fake_event_bus, _Executor(llm, spy), sid)

        kinds = _kinds(fake_workspace_io, sid)
        assert outcome.success is False and llm.calls == 2, "the first overflow recovered, the replay's was not"
        assert [k for k in kinds if k in TERMINAL] == ["error"], f"one terminal record, the ERROR: {kinds}"
        error = next(json.loads(line) for line in fake_workspace_io.read_lines(sid) if json.loads(line).get("kind") == "error")
        assert error["payload"]["extensions"]["forced_compaction"] is True
        assert error["payload"]["extensions"]["replay_attempted"] is True
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert (row.status, row.ended_detail) == (SessionStatus.ENDED, ContextOverflowUnrecoverable.CODE)
