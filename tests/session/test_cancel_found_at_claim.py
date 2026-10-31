"""AG-L1: a Cancel already on the row when the turn is claimed takes the normal cancelled exit.

The turn still never runs, but the exit used to write the whole row read before it (no lock, no fence) and announce
nothing: no CANCELLED record, no ``:terminal`` key (a webhook hold waited out its cap), no ``session.ended`` and no drain,
so a follow-up queued behind the armed turn was stranded.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, PendingSessionMessage, SessionStatus, WorkspaceSession
from primer.session import dispatch
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.pending_messages import store_pending_steer
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO, _make_lease


class _RecBus(InMemoryEventBus):
    def __init__(self):
        super().__init__()
        self.published: list[tuple[str, Any]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload))
        return await super().publish(event_key, payload)


def _no_build(_s):
    raise AssertionError("the turn must not run")


async def _seed(sp, **fields):
    row = WorkspaceSession(**{
        "id": "s1", "workspace_id": "w1", "binding": AgentSessionBinding(agent_id="a"), "status": SessionStatus.RUNNING,
        "created_at": datetime.now(timezone.utc), "turn_status": "claimable", "cancel_requested": True, "last_seq": 4,
        **fields,
    })
    return await sp.get_storage(WorkspaceSession).create(row)


@pytest.mark.asyncio
async def test_cancel_found_at_claim_records_announces_and_drains(monkeypatch):
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    row = await _seed(sp)
    await store_pending_steer(storage_provider=sp, session=row, text="actually, do this instead", workspace_registry=None)
    bus = _RecBus()
    await bus.initialize()
    io = FakeWorkspaceIO()
    emitted: list[tuple[str, Any]] = []

    class _Recorder:
        async def emit(self, name: str, **kwargs: Any) -> None:
            emitted.append((name, kwargs.get("payload")))

    monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: _Recorder())
    drained: list[str] = []

    async def _realize(deps, session):
        drained.append(session.id)

    monkeypatch.setattr(dispatch, "_realize_pending_at_checkpoint", _realize)

    outcome = await run_one_session_turn(_make_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=io, event_bus=bus, build_executor=_no_build,
    ))

    after = await sessions.get("s1")
    assert (outcome.success, outcome.drop_lease) == (True, True)
    assert (after.status, after.ended_reason, after.turn_status) == (SessionStatus.ENDED, "cancelled", "idle")
    records = [json.loads(ln) for ln in io.read_lines("s1")]
    assert [(r["seq"], r["kind"], r["payload"]) for r in records] == [(5, "cancelled", {"reason": "operator_cancel"})]
    assert after.last_seq == 5
    assert ("session:s1:tick", {"seq": 5}) in bus.published
    assert ("session:s1:terminal", {"status": "ended", "ended_reason": "cancelled"}) in bus.published
    assert ("session.ended", {"ended_reason": "cancelled"}) in emitted
    assert drained == ["s1"]
    queued = (await sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=10))).items
    assert len(queued) == 1, "the drain checkpoint, not this exit, decides what happens to the queued follow-up"


@pytest.mark.asyncio
async def test_cancel_found_at_claim_does_not_overwrite_a_row_another_path_ended(monkeypatch):
    """The exit decided from the top read; the row was ended (the reconciler: ``workspace_lost``) before it wrote."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    stale = await _seed(sp)
    ended = stale.model_copy(update={"status": SessionStatus.ENDED, "ended_reason": "workspace_lost",
                                     "ended_at": datetime.now(timezone.utc)})
    await sessions.update(ended)
    real_get = sessions.get
    calls = {"n": 0}

    async def _get(sid, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return stale.model_copy()      # the dispatch's top read, taken before the reconciler wrote
        return await real_get(sid, *a, **k)

    monkeypatch.setattr(sessions, "get", _get)
    bus = _RecBus()
    await bus.initialize()
    io = FakeWorkspaceIO()

    outcome = await run_one_session_turn(_make_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=io, event_bus=bus, build_executor=_no_build,
    ))

    after = await real_get("s1")
    assert outcome.drop_lease is True
    assert (after.status, after.ended_reason) == (SessionStatus.ENDED, "workspace_lost")
    assert io.read_lines("s1") == []
    assert ("session:s1:terminal", {"status": "ended", "ended_reason": "workspace_lost"}) in bus.published



@pytest.mark.asyncio
async def test_a_persons_follow_up_queued_behind_the_cancelled_turn_is_realized():
    """The probe inverted, with a real drain: the follow-up becomes the next turn (a person's message reopens the session)."""
    from tests.session._peer_fakes import FakeEngine, FakeRegistry, FakeScheduler

    sp = _FakeStorageProvider()
    row = await _seed(sp)
    await store_pending_steer(storage_provider=sp, session=row, text="actually, do this instead", workspace_registry=None)
    bus = _RecBus()
    await bus.initialize()
    scheduler = FakeScheduler()

    await run_one_session_turn(_make_lease("s1"), SessionDispatchDeps(
        storage_provider=sp, workspace_io=FakeWorkspaceIO(), event_bus=bus, build_executor=_no_build,
        scheduler=scheduler, claim_engine=FakeEngine(), workspace_registry=FakeRegistry(),
    ))

    queued = (await sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=10))).items
    assert queued == []
    assert scheduler.enqueued == ["s1"]
