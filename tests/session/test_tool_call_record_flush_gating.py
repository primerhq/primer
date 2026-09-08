"""TOOL_CALL record stash + eager flush is gated on
``tool_calls_as_claims_enabled`` (Phase 3 stage 7a, 01a0518b, 7a gate
verdict unverified item A - confirmed real and fixed).

The stash (``_CoalesceState.tool_call_record_seq``/``tool_call_record_
name``) exists ONLY so the tool-dispatch seam can look up a scoped
call's durable record synchronously, and the eager flush (bypassing the
writer's normal 16KB/100ms buffered policy) exists ONLY because a
claim-based worker reads ``messages.jsonl`` from a DIFFERENT PROCESS
once tool_calls_as_claims is armed. Neither ``ToolWaitPark`` nor a mixed
``graph_checkpoint`` can ever be produced on a flag-off turn
(``run_agent_turn``'s own routing gate never enters
``_dispatch_as_claims``), so before this fix BOTH ran unconditionally on
EVERY tool call of EVERY turn - a per-tool-call forced flush cost paid
on the default (flag-off) path for zero behavioural benefit.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done, ToolCallEnd, ToolCallStart
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.persistence import WorkspaceMessageWriter

from tests.conftest import _FakeStorageProvider


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        key = (session_id, "messages.jsonl")
        self._data[key] = self._data.get(key, b"") + line


class _FakeEventBus:
    async def publish(self, key: str, payload: dict) -> None:
        return None


class _TwoToolCallsExecutor:
    """Two tool calls dispatched in-process, then a clean Done - no park
    at all, isolating the stash/flush gate from any park-specific logic.
    """

    def __init__(self, *, tool_calls_as_claims_enabled: bool) -> None:
        self._tool_calls_as_claims_enabled = tool_calls_as_claims_enabled

    async def invoke(self, messages, **kwargs):
        yield ToolCallStart(id="call_a", name="tool_a", index=0)
        yield ToolCallEnd(id="call_a", arguments={"x": 1}, index=0)
        yield ToolCallStart(id="call_b", name="tool_b", index=1)
        yield ToolCallEnd(id="call_b", arguments={"y": 2}, index=1)
        yield Done(stop_reason="stop", raw_reason="stop")


async def _run_turn_and_count_flushes(*, tool_calls_as_claims_enabled: bool, monkeypatch) -> int:
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    session = WorkspaceSession(
        id=f"s-flush-gate-{tool_calls_as_claims_enabled}",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await session_storage.create(session)

    async def _build_executor(_session: WorkspaceSession):
        return _TwoToolCallsExecutor(
            tool_calls_as_claims_enabled=tool_calls_as_claims_enabled,
        )

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=_FakeEventBus(),
        build_executor=_build_executor,
    )

    flush_calls = 0
    real_flush = WorkspaceMessageWriter.flush

    async def _counting_flush(self):
        nonlocal flush_calls
        flush_calls += 1
        return await real_flush(self)

    monkeypatch.setattr(WorkspaceMessageWriter, "flush", _counting_flush)

    lease = Lease(
        kind=ClaimKind.SESSION, entity_id=session.id, claimed_by="worker-1",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    outcome = await run_one_session_turn(lease, deps)
    assert outcome.success is True
    return flush_calls


@pytest.mark.asyncio
async def test_flag_off_skips_the_per_tool_call_eager_flush(monkeypatch) -> None:
    """Only the ONE unconditional end-of-turn flush runs - not one per
    tool call. Before the fix this was 3 (2 eager + 1 final)."""
    flush_calls = await _run_turn_and_count_flushes(
        tool_calls_as_claims_enabled=False, monkeypatch=monkeypatch,
    )
    assert flush_calls == 1


@pytest.mark.asyncio
async def test_flag_on_preserves_the_per_tool_call_eager_flush(monkeypatch) -> None:
    """The claim-based-worker scenario the eager flush exists for keeps
    working: one flush per tool call, plus the final one."""
    flush_calls = await _run_turn_and_count_flushes(
        tool_calls_as_claims_enabled=True, monkeypatch=monkeypatch,
    )
    assert flush_calls == 3
