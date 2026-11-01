"""AG-L2: the cancelled exit on a row another path already ENDED still writes ``last_seq`` back.

The exit leaves such a row's status and reason alone, but the turn's writer has numbered records (the streamed output made
durable on the way out among them). Without the write-back a later reopen or release marker reuses those seqs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.chat import TextDelta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session import dispatch
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.persistence import WorkspaceMessageWriter
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease


@pytest.mark.asyncio
async def test_reconciler_ends_the_row_mid_turn_then_the_cancelled_exit_persists_last_seq(monkeypatch):
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="a"), status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc), turn_status="claimable", last_seq=10,
    ))
    bus = InMemoryEventBus()
    await bus.initialize()
    writers: list[WorkspaceMessageWriter] = []

    class _SpyWriter(WorkspaceMessageWriter):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            writers.append(self)

    monkeypatch.setattr(dispatch, "WorkspaceMessageWriter", _SpyWriter)

    class _Executor:
        was_interrupted = False

        def bind_interrupt_event(self, event):
            self._stop = event

        async def invoke(self, messages, **kwargs):
            yield TextDelta(text="partial answer", index=0)
            await asyncio.sleep(0.05)  # the turn's cancel watcher subscribes
            # A Cancel lands, and the reconciler ends the row before the turn's exit runs.
            row = await sessions.get("s1")
            row.cancel_requested = True
            row.status = SessionStatus.ENDED
            row.ended_reason = "workspace_lost"
            await sessions.update(row)
            await bus.publish("session:s1:cancel", {})
            await asyncio.wait_for(self._stop.wait(), 5)
            self.was_interrupted = True

    async def build(_s):
        return _Executor()

    outcome = await run_one_session_turn(_make_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=FakeWorkspaceIO(), event_bus=bus, build_executor=build,
    ))

    row = await sessions.get("s1")
    assert outcome.drop_lease is True
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "workspace_lost")
    (writer,) = writers
    assert writer.last_seq > 10, "the streamed output was numbered on the way out"
    assert row.last_seq == writer.last_seq
