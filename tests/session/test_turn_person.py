"""The person behind each turn (Phase 2, Task 4; spec section 13 Q1).

A turn runs as the person whose input started it, so the session row carries
``turn_person`` (written by the wake that arms the turn, and by a resume of a
session that never ran one) and ``session_run_principal`` /
``resolve_run_principal`` rank a run by it instead of ``initiated_by``. A
resume of a parked turn keeps the parked turn's person; channel input runs as
the session's creator; a queued steer runs as its sender when realized.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.enqueue import wake_session
from primer.session.pending_messages import realize_next_pending, store_pending_steer
from primer.session.steer_delivery import DELIVERED_WOKEN, SteerDelivery, deliver_steer
from primer.session.yields import durably_wake_session
from primer.trigger.subscribers import DispatchDeps
from primer.worker.executor_builders import RunRefused, resolve_run_principal, session_run_principal
from primer.workspace.session_factory import resume_session
from tests.conftest import _FakeStorageProvider
from tests.session._peer_fakes import make_world, seed_session

NOW = datetime.now(UTC)
CREATOR = PrincipalRef(type="user", id="u-creator", display="creator", role="admin", source="local")
PEER = PrincipalRef(type="user", id="u-peer", display="peer", role="user", source="local")


async def test_steered_turn_runs_as_the_peer():
    """Q1: a steer arms its turn as the person who sent it, never the session's creator."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="s1", status=SessionStatus.WAITING, initiated_by=CREATOR)
    row = await wake_session(
        workspace_id="ws-1", session_id="s1", instruction="steer me",
        human_intent=True, deps=world.wake_deps(), by=PEER,
    )
    assert row.turn_person == PEER
    assert session_run_principal(row) == PEER, "the turn would run as the creator"
    assert (await world.sessions.get("s1")).turn_person == PEER, "the wake's arm write must carry it"


async def test_a_queued_steer_realized_later_runs_as_its_sender():
    """The queue keeps who sent a steer (attribution['by']) and the drain that realizes it wakes with them."""
    world = make_world(with_bus=True)
    row = await seed_session(
        world, sid="s1", status=SessionStatus.RUNNING, turn_status="running", initiated_by=CREATOR,
    )
    stored = await store_pending_steer(
        storage_provider=world.sp, session=row, text="later",
        workspace_registry=world.registry, event_bus=world.bus, by=PEER,
    )
    assert stored.attribution["by"] == PEER.model_dump(mode="json"), "the queue must keep who sent it"
    # The turn ends; the drain checkpoint realizes the queued steer.
    await world.sessions.update(row.model_copy(update={"status": SessionStatus.WAITING, "turn_status": "idle"}))
    realized = await realize_next_pending(
        storage_provider=world.sp, workspace_id="ws-1", session_id="s1", wake_deps=world.wake_deps(),
    )
    assert realized
    assert (await world.sessions.get("s1")).turn_person == PEER, "the realized turn must run as the queued sender"


async def test_a_resume_after_an_approval_keeps_the_parked_person():
    """Answering an approval resumes the parked turn; the turn keeps the person it parked with."""
    world = make_world(with_bus=True)
    row = await seed_session(
        world, sid="s1", status=SessionStatus.RUNNING,
        parked_status="parked", parked_at=NOW, initiated_by=CREATOR, turn_person=CREATOR,
    )
    await durably_wake_session(
        row, event_key="ask_user:s1:tc-1", payload={"response": "yes"},
        session_storage=world.sessions, engine=_ResumableEngine(),
    )
    current = await world.sessions.get("s1")
    assert current.parked_status == "resumable", "the resume must have landed"
    assert current.turn_person == CREATOR, "a resume must keep the parked turn's person"


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


class _ResumableEngine:
    """A claim engine that only records the lease re-arm: the durable flip's sole engine call."""

    def __init__(self) -> None:
        self.resumables: list[tuple] = []

    async def mark_resumable(self, kind: Any, entity_id: str, *, priority: int = 50) -> None:
        self.resumables.append((kind, entity_id))


def _channel() -> Channel:
    return Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="777",
        config=TelegramChannelConfig(),
    )


def _channel_event(text: str = "hello") -> ChannelEvent:
    return ChannelEvent(
        provider=ChannelProviderType.TELEGRAM, provider_id="cp-1", event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED, occurred_at=NOW, channel_id="ch-1",
        surface="thread", thread_anchor="thr-1", sender=EventSender(external_id="u1"), text=text,
    )


async def test_channel_input_runs_as_the_creator(monkeypatch):
    """Channel-thread senders are not primer users: the turn runs as the session's creator (spec 13 Q1)."""
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, created_at=NOW, initiated_by=CREATOR,
    ))
    store = CorrelationStore(sp)
    await store.upsert_session(
        channel_id="ch-1", anchor="thr-1", workspace_id="w1", session_id="s1", tool_call_id="tc-1",
    )
    await store.clear_gate("ch-1", "thr-1")  # thread mapped, no open gate: the reply is a steer
    captured: dict[str, Any] = {}

    async def _fake_deliver(**kw):
        captured.update(kw)
        return SteerDelivery(outcome=DELIVERED_WOKEN, session_id="s1")

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    router = ChannelEventRouter(
        storage_provider=sp, correlation_store=store,
        fire_deps=DispatchDeps(storage_provider=sp, claim_engine=None),
        event_bus=_RecordingBus(),
    )
    outcome = await router.route_event(event=_channel_event(), channel=_channel())
    assert outcome.kind == "steer"
    assert captured["by"] == CREATOR, "channel input must run as the session's creator"


async def test_resolve_run_principal_refuses_a_deleted_turn_person():
    """A turn person whose account is gone refuses the run, as a deleted creator does today."""
    sp = _FakeStorageProvider()
    await sp.get_storage(User).create(User(id="u-creator", username="u-creator", created_at=NOW, role="admin"))
    # u-peer no longer exists: only the creator's account is resolvable.
    session = WorkspaceSession(
        id="s1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING, created_at=NOW, initiated_by=CREATOR, turn_person=PEER,
    )
    with pytest.raises(RunRefused):
        await resolve_run_principal(sp, session)


async def test_a_resume_of_a_never_started_session_runs_as_the_resumer():
    """Resuming a session that never ran one starts its first turn; the resumer is its person.
    A resume of a parked (PAUSED) session writes nothing: the parked turn keeps its person."""
    world = make_world(with_bus=True)
    await seed_session(world, sid="s-new", status=SessionStatus.CREATED, initiated_by=CREATOR)
    out = await resume_session(
        workspace_id="ws-1", session_id="s-new", sessions=world.sessions,
        scheduler=world.scheduler, claim_engine=world.engine, by=PEER,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.turn_person == PEER, "the resumer starts the first turn"
    parked = await seed_session(
        world, sid="s-parked", status=SessionStatus.PAUSED,
        initiated_by=CREATOR, turn_person=CREATOR,
    )
    await resume_session(
        workspace_id="ws-1", session_id="s-parked", sessions=world.sessions,
        scheduler=world.scheduler, claim_engine=world.engine, by=PEER,
    )
    assert (await world.sessions.get("s-parked")).turn_person == CREATOR, "a parked turn keeps its person"
