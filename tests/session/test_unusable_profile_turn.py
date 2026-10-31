"""A turn whose model profile the person it runs as may not use fails the TURN, never the session (access-control P4 Task 4, plan Decision 7).

A peer without the profile grant who steers the owner's session must not end it: the session is PAUSED with its park kept, an ERROR record
says why, ``last_turn_error.code`` is ``model_profile_unusable``, and nothing counts as a turn. After an admin grants use, a resume runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.access.person import Person
from primer.authz import PROFILE_UNUSABLE_CODE, ProfileUnusable
from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.worker.executor_builders import resolve_llm_and_model
from tests.conftest import _FakeStorageProvider
from tests.worker.test_engine_session_resume import _claim_session
from tests.worker.test_resume_of_a_refused_workspace import SID, _setup

OWNER, PEER = Person("owner", "user"), Person("peer", "user")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease(session_id: str) -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1", claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


class _Io:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)


async def _run(storage_provider, build_executor):
    bus = InMemoryEventBus()
    await bus.initialize()
    io = _Io()
    deps = SessionDispatchDeps(storage_provider=storage_provider, workspace_io=io, event_bus=bus, build_executor=build_executor)
    try:
        return await run_one_session_turn(_lease("s1"), deps), io
    finally:
        await bus.aclose()


async def _seed_parked_session(sp) -> WorkspaceSession:
    row = WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=_now(), turn_status="running", turn_no=4, parked_status="parked", parked_event_key="evt-1", interrupt_requested=True,
    )
    await sp.get_storage(WorkspaceSession).create(row)
    return row


async def test_peer_without_grant_pauses_not_ends():
    sp = _FakeStorageProvider()
    await _seed_parked_session(sp)

    async def build_executor(session):
        raise ProfileUnusable("pro-private")

    outcome, io = await _run(sp, build_executor)

    row = await sp.get_storage(WorkspaceSession).get("s1")
    assert row.status == SessionStatus.PAUSED and row.ended_reason is None and row.ended_at is None
    assert row.last_turn_error is not None and row.last_turn_error.code == PROFILE_UNUSABLE_CODE
    assert row.parked_status == "parked" and row.parked_event_key == "evt-1", "the park is kept"
    assert row.turn_status == "idle" and row.turn_started_at is None and row.interrupt_requested is False
    assert row.turn_no == 4, "a refused turn is not a turn"
    assert outcome.drop_lease is True and outcome.entity_noop is True
    assert any(b"model_profile_unusable" in line and b"pro-private" in line for line in io.lines), "the transcript says why"


async def test_a_resume_refused_for_the_parked_person_pauses_and_keeps_the_park(monkeypatch):
    """The resume handler builds its own executor as the parked turn's person: the same refusal pauses, never ends."""
    pool, engine, sessions = await _setup(refusing=False)
    before = await sessions.get(SID)

    async def handler(lease, row):
        raise ProfileUnusable("pro-private")

    monkeypatch.setattr(pool, "_resume_engine_session", handler)

    await pool._run_engine_session(await _claim_session(engine, SID))

    row = await sessions.get(SID)
    assert row.status == SessionStatus.PAUSED and row.ended_reason is None
    assert row.parked_status == "resumable" and row.parked_state == before.parked_state
    assert row.last_turn_error is not None and row.last_turn_error.code == PROFILE_UNUSABLE_CODE
    assert row.turn_no == before.turn_no


class _Pool:
    """Just what ``resolve_llm_and_model`` reads: the storage, and the adapter resolution (a stand-in for the real resolver)."""

    def __init__(self, storage) -> None:
        self._storage = storage

    async def _resolve_llm(self, agent, override=None):
        return "llm", "model"


async def _world():
    sp = _FakeStorageProvider()
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="pro", description="d", provider_id="prov", model_name="m", context_length=1000,
            access=Access(grants={"owner": "read"}, rev=1),
        )
    )
    agent = Agent.model_validate({"id": "ag1", "description": "d", "model": {"profile_id": "pro"}})
    return sp, agent


async def test_owner_next_turn_runs():
    sp, agent = await _world()
    pool = _Pool(sp)

    assert await resolve_llm_and_model(pool, agent, None, person=OWNER) == ("llm", "model")
    with pytest.raises(ProfileUnusable):
        await resolve_llm_and_model(pool, agent, None, person=PEER)


async def test_resume_after_grant_runs():
    sp, agent = await _world()
    pool = _Pool(sp)
    with pytest.raises(ProfileUnusable):
        await resolve_llm_and_model(pool, agent, None, person=PEER)

    store = sp.get_storage(ModelProfile)
    row = await store.get("pro")
    await store.update(row.model_copy(update={"access": Access(grants={"owner": "read", "peer": "read"}, rev=2)}))

    assert await resolve_llm_and_model(pool, agent, None, person=PEER) == ("llm", "model")


async def test_resume_rechecks_the_parked_person():
    """The person is the one the turn runs as NOW: the owner's turn is not refused because a peer was, and the grant is read live."""
    sp, agent = await _world()
    pool = _Pool(sp)
    store = sp.get_storage(ModelProfile)

    assert await resolve_llm_and_model(pool, agent, None, person=OWNER) == ("llm", "model")
    row = await store.get("pro")
    await store.update(row.model_copy(update={"access": Access(rev=3)}))  # the owner's grant is revoked while the turn is parked

    with pytest.raises(ProfileUnusable):
        await resolve_llm_and_model(pool, agent, None, person=OWNER)
    assert await resolve_llm_and_model(pool, agent, None, person=Person("root", "admin")) == ("llm", "model")
