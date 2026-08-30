"""S6 P2: every terminal turn exit announces itself on the bus.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 9 - the interactive
webhook hold is an async wait on the completion event, never a poll.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(self, session_id: str, filename: str = "messages.jsonl"):
        raw = self._data.get((session_id, filename), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


class _ScriptedExecutor:
    """Scripted stream plus the ``last_done_reason`` the dispatch reads.

    The post-turn status comes from
    ``getattr(executor, "last_done_reason", None)``
    (`primer/session/dispatch.py:659`), not from the streamed Done event, so
    the attribute has to be set for the mapping to be exercised at all.
    """

    def __init__(
        self, events: list[Any], last_done_reason: str | None = None,
    ) -> None:
        self._events = events
        self.last_done_reason = last_done_reason

    async def invoke(self, messages: list[Any], **kwargs: Any):
        for ev in self._events:
            if isinstance(ev, Exception):
                raise ev
            yield ev


def _lease(session_id: str) -> Lease:
    now = datetime.now(UTC)
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


async def _collect_terminals(bus, session_id, sink):
    sub = bus.subscribe()
    try:
        async for event in sub:
            if event.event_key == f"session:{session_id}:terminal":
                sink.append(event.payload)
    except asyncio.CancelledError:
        pass
    finally:
        await sub.aclose()


async def _run(events, sink, last_done_reason=None):
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        turn_status="running",
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    collector = asyncio.create_task(_collect_terminals(bus, "s1", sink))
    await asyncio.sleep(0)

    async def _build(session):
        return _ScriptedExecutor(events, last_done_reason)

    deps = SessionDispatchDeps(
        storage_provider=sp,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=bus,
        build_executor=_build,
    )
    await run_one_session_turn(_lease("s1"), deps)
    await asyncio.sleep(0.05)
    collector.cancel()
    try:
        await collector
    except asyncio.CancelledError:
        pass
    await bus.aclose()


async def test_clean_completion_publishes_terminal():
    """01a0518a: _CLEAN_TURN_RESTS_PARKED defaults True, so a clean stop
    now rests the row WAITING (served as session_state="parked") instead
    of ending it - the hold must still be released either way, which is
    this test's actual point (see test_flag_off_clean_completion_still_
    ends below for the pre-flip shape, preserved via the test seam)."""
    sink: list[dict] = []
    await _run(
        [Done(stop_reason="stop", raw_reason="stop")], sink,
        last_done_reason="stop",
    )
    assert len(sink) == 1
    assert sink[0]["status"] == SessionStatus.WAITING.value
    assert sink[0]["ended_reason"] is None


async def test_flag_off_clean_completion_still_ends(monkeypatch):
    """The pre-01a0518a shape, preserved via the flag's test seam: with
    _CLEAN_TURN_RESTS_PARKED off, a clean stop still ends the session and
    still announces on the bus."""
    from primer.session import dispatch

    monkeypatch.setattr(dispatch, "_CLEAN_TURN_RESTS_PARKED", False)
    sink: list[dict] = []
    await _run(
        [Done(stop_reason="stop", raw_reason="stop")], sink,
        last_done_reason="stop",
    )
    assert len(sink) == 1
    assert sink[0]["status"] == SessionStatus.ENDED.value
    assert sink[0]["ended_reason"] == "completed"


async def test_executor_failure_publishes_terminal():
    sink: list[dict] = []
    await _run([RuntimeError("boom")], sink)
    assert len(sink) == 1
    assert sink[0]["status"] == SessionStatus.ENDED.value
    assert sink[0]["ended_reason"] == "failed"


async def test_a_non_ending_turn_also_announces_its_exit():
    """max_tokens leaves the row WAITING; the hold must still be released."""
    sink: list[dict] = []
    await _run(
        [Done(stop_reason="max_tokens", raw_reason="max_tokens")], sink,
        last_done_reason="max_tokens",
    )
    assert len(sink) == 1
    assert sink[0]["status"] == SessionStatus.WAITING.value
