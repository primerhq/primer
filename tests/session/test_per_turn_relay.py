"""S6 P3 / crosscheck M4: a thread-mapped interactive session relays every turn.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 4. The hook is the
drain checkpoint: a turn that finishes cleanly but leaves the session
WAITING must still post its answer into the thread.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done, TextDelta
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider

# Every session here is channel-bound: without a resolvable reply binding
# resolve_reply_binding returns None and the relay no-ops for reasons that
# have nothing to do with the gate under test.
_BINDING = {"channel_id": "ch-1", "anchor": "thr-1", "quiet": False}


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

    ``run_one_session_turn`` decides the post-turn status from
    ``getattr(executor, "last_done_reason", None)``, NOT from the Done event
    it just streamed; an executor without the attribute always lands on
    ENDED/completed and the WAITING cases under test never occur.
    """

    def __init__(self, events: list[Any], last_done_reason: str | None) -> None:
        self._events = events
        self.last_done_reason = last_done_reason

    async def invoke(self, messages: list[Any], **kwargs: Any):
        for ev in self._events:
            yield ev


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def dispatch_prompt(self, *, envelope, session=None):
        self.texts.append(envelope.prompt)
        return [{"ok": True}]


def _lease(session_id: str) -> Lease:
    now = datetime.now(UTC)
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


async def _run(metadata: dict, stop_reason: str) -> _RecordingDispatcher:
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        turn_status="running",
        metadata={SESSION_REPLY_BINDING_KEY: _BINDING, **metadata},
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    dispatcher = _RecordingDispatcher()

    async def _build(session):
        return _ScriptedExecutor(
            [
                TextDelta(text="here is the answer", index=0),
                Done(stop_reason=stop_reason, raw_reason=stop_reason),
            ],
            last_done_reason=stop_reason,
        )

    await run_one_session_turn(_lease("s1"), SessionDispatchDeps(
        storage_provider=sp,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=bus,
        build_executor=_build,
        channel_dispatcher=dispatcher,
    ))
    await bus.aclose()
    return dispatcher


async def test_mapped_session_relays_a_non_ending_turn():
    """max_tokens leaves the session WAITING; the thread still gets the text."""
    dispatcher = await _run({RELAY_EVERY_TURN_KEY: True}, "max_tokens")
    assert dispatcher.texts == ["here is the answer"]


async def test_unmapped_session_does_not_relay_a_non_ending_turn():
    """Regression: today's behaviour for every other session is unchanged."""
    dispatcher = await _run({}, "max_tokens")
    assert dispatcher.texts == []


async def test_unmapped_session_still_relays_on_clean_completion():
    dispatcher = await _run({}, "stop")
    assert dispatcher.texts == ["here is the answer"]


async def test_quiet_binding_still_suppresses_a_mapped_session():
    """S6 section 4: a non-interactive channel trigger ingests silently."""
    dispatcher = await _run(
        {SESSION_REPLY_BINDING_KEY: {**_BINDING, "quiet": True}}, "stop",
    )
    assert dispatcher.texts == []
