"""Review 2026-11-23 finding 1.4: cancelling a PARKED session must end it.

A session parked on an approval or an ask_user answer is RUNNING but
holds no worker scope: the park dropped its claim lease, so the cancel
path's RUNNING branch (set cancel_requested + NOTIFY) left the row
parked until a person answered or ``parked_until`` passed (3600 s
default). No worker held a scope for it, so nobody ever acted on the
flag.

The fix (``primer.workspace.session_factory.cancel_session``): a parked
RUNNING row gets its park flipped to ``resumable`` (the same guarded
patch a wake of it lands, fenced on the park it was read from) and its
lease re-armed as an IDEMPOTENT UPSERT (``engine.mark_resumable``, the
same re-arm the wake uses) -- a row a wake already flipped is re-armed
again (a retried cancel restores a lease a failed or crashed re-arm
lost), a row that ended between the read and the flip gets no lease,
and a failed re-arm is logged, not raised (the cancel still answers,
publishes and signals).

The next claim of the flipped row ends it through the WORKER POOL's
resumable branch (``primer/worker/pool.py``), which production takes for
every resumable row -- NOT ``run_one_session_turn``. That branch routes
a ``cancel_requested`` row to the same full cancelled terminal a normal
turn's claim-time exit uses (``_end_cancelled_at_claim`` in
``primer/session/dispatch.py``): the CANCELLED transcript record, the
tick, the ``session.ended`` event, the ``session:{id}:terminal`` publish
a webhook hold waits on, and the drain checkpoint. Before the fix the
branch took the pool's bare ``_end_session`` status write, which ended
the row silently (review 2026-11-23, K1 round 1).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_factory import (
    SessionCancelDeps,
    cancel_session,
)
from primer.worker.pool import WorkerPool

# Reuse the established session-factory harness fakes.
from tests.workspace.test_session_factory import (
    _FakeEventBus,
    _FakeScheduler,
    _cancel_deps,
)
from primer.worker import pool as pool_module


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _RecordingClaimEngine:
    """Records the lease mutations cancel_session performs."""

    def __init__(self, *, fail_rearm: bool = False) -> None:
        self._fail_rearm = fail_rearm
        self.marked: list[tuple[ClaimKind, str]] = []
        self.deleted: list[tuple[ClaimKind, str]] = []
        self.upserts: list[tuple[ClaimKind, str]] = []

    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 100
    ) -> None:
        if self._fail_rearm:
            raise RuntimeError("claim re-arm failed (simulated)")
        self.marked.append((kind, entity_id))

    async def delete_lease(self, kind: ClaimKind, entity_id: str) -> None:
        self.deleted.append((kind, entity_id))

    async def upsert(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 100,
        next_attempt_at: datetime | None = None,
    ) -> None:
        self.upserts.append((kind, entity_id))


class _RecordingShim:
    """Stands in for the pool's _WorkspaceIOShim: records the message
    lines the claim path writes instead of dropping them (the real shim
    resolves the workspace through the registry; the pool is built
    registry-less here, so this is also what the production shim would
    hand the session's workspace)."""

    instances: list[_RecordingShim] = []

    def __init__(self, workspace_registry: Any) -> None:
        self.lines: dict[str, bytearray] = defaultdict(bytearray)
        _RecordingShim.instances.append(self)

    def register_session(self, session_id: str, workspace_id: str) -> None:
        pass

    def workspace_id_for(self, session_id: str) -> str | None:
        return None

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines[session_id] += line

    async def append_state_line(self, workspace_id: str, rel: str, line: bytes) -> None:
        pass

    async def read_state_file(self, workspace_id: str, rel: str) -> bytes:
        return b""

    async def read_state_tail(self, workspace_id: str, rel: str, n: int):
        return b""


class _EventStore:
    """Records the platform event log appends (the recorder's store)."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def append(self, **kwargs: Any) -> int:
        self.emitted.append((kwargs["event_type"], kwargs.get("payload") or {}))
        return len(self.emitted)


async def _seed_session(
    sp,
    *,
    status: SessionStatus,
    sid: str = "sess-park-1",
    workspace_id: str = "ws-1",
    parked_status: str | None = None,
) -> WorkspaceSession:
    now = _now()
    session = WorkspaceSession(
        id=sid,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=status,
        created_at=now,
        parked_status=parked_status,
        parked_at=now if parked_status else None,
        parked_event_key="approval:call-1" if parked_status else None,
        parked_until=now + timedelta(seconds=3600) if parked_status else None,
    )
    await sp.get_storage(WorkspaceSession).create(session)
    return session


@pytest.mark.asyncio
async def test_cancel_parked_running_session_flips_the_park_and_rearms(
    fake_storage_provider,
):
    """A parked RUNNING row gets cancel_requested AND its park flipped to
    'resumable' AND the lease re-armed: the next claim can end it."""
    await _seed_session(
        fake_storage_provider,
        status=SessionStatus.RUNNING,
        parked_status="parked",
    )
    engine = _RecordingClaimEngine()
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    deps = _cancel_deps(
        fake_storage_provider, scheduler, engine, event_bus=bus
    )

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-park-1", deps=deps,
    )

    # Still RUNNING out of this call (the end happens at the next claim,
    # like any running cancel) -- but the row no longer sits parked.
    assert out.cancel_requested is True
    assert out.cancel_requested_at is not None
    assert out.status == SessionStatus.RUNNING
    assert out.parked_status == "resumable"
    assert out.resumable_at is not None
    # The bus publish and the scheduler signal are preserved.
    assert ("session:sess-park-1:cancel", {}) in bus.published
    assert "sess-park-1" in scheduler.signalled
    # The lease is re-armed the way a wake re-arms it.
    assert (ClaimKind.SESSION, "sess-park-1") in engine.marked
    # The row persisted as flagged + flipped, the park's identity kept.
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get("sess-park-1")
    assert row is not None
    assert row.cancel_requested is True
    assert row.parked_status == "resumable"
    assert row.resumable_at is not None
    assert row.parked_at is not None  # the park columns survive the flip


@pytest.mark.asyncio
async def test_cancel_running_non_parked_session_behaves_exactly_as_before(
    fake_storage_provider,
):
    """A RUNNING row that never parked: exactly the pre-fix behaviour
    (flag + publish + signal), no park flip, no re-arm."""
    await _seed_session(fake_storage_provider, status=SessionStatus.RUNNING)
    engine = _RecordingClaimEngine()
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    deps = _cancel_deps(
        fake_storage_provider, scheduler, engine, event_bus=bus
    )

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-park-1", deps=deps,
    )

    assert out.cancel_requested is True
    assert out.status == SessionStatus.RUNNING
    assert out.parked_status is None
    assert out.resumable_at is None
    assert ("session:sess-park-1:cancel", {}) in bus.published
    assert "sess-park-1" in scheduler.signalled
    assert engine.marked == []  # nothing to re-arm
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get("sess-park-1")
    assert row is not None
    assert row.parked_status is None


@pytest.mark.asyncio
async def test_cancel_parked_session_reaches_ended_via_the_pool_claim(
    fake_storage_provider, monkeypatch,
):
    """The full chain through the WORKER POOL's claim path -- the path
    production takes for a resumable row (it never calls
    run_one_session_turn for one): a parked row is not claimable even
    with an armed lease (the original bug shape -- nothing can act on
    it); the cancel flips it and re-arms the lease; the pool's claim
    takes the resumable-cancel branch and ends it with the same full
    cancelled terminal a normal turn's claim-time exit uses: the
    CANCELLED transcript record, the session.ended event, the
    session:{id}:terminal publish a webhook hold waits on, ENDED/
    cancelled on the row, the lease dropped, no executor ever built."""
    sid = "sess-park-pool"
    await _seed_session(
        fake_storage_provider,
        status=SessionStatus.RUNNING,
        parked_status="parked",
        sid=sid,
    )
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    engine = InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=sessions),
        },
    )
    scheduler = _FakeScheduler()
    bus = _FakeEventBus()

    # The bug shape: even with a lease armed (boot recovery re-arms
    # every RUNNING session), a 'parked' row is not eligible, so nothing
    # claims it and nobody answers the park.
    await engine.upsert(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-k1-cancel", max_count=10)
    assert all(l.entity_id != sid for l in leases)

    deps = SessionCancelDeps(
        storage_provider=fake_storage_provider,
        scheduler=scheduler,
        claim_engine=engine,
        event_bus=bus,
    )
    await cancel_session(
        workspace_id="ws-1", session_id=sid, deps=deps,
    )

    # The cancel re-armed a lease the claim query now admits (the row is
    # 'resumable' now).
    lease = next(
        (
            l
            for l in await engine.claim_due("wrk-k1-cancel", max_count=10)
            if l.entity_id == sid
        ),
        None,
    )
    assert lease is not None, (
        "after the cancel, the flipped row must be claimable so the "
        "pool's resumable-cancel exit can end it"
    )

    events = _EventStore()
    fake_storage_provider.get_event_store = lambda: events
    executor_built = False

    async def _build_executor_raises(session: WorkspaceSession):
        nonlocal executor_built
        executor_built = True
        raise AssertionError("executor must not be built for a cancelled claim")

    monkeypatch.setattr(
        WorkerPool, "_build_session_executor", _build_executor_raises
    )
    monkeypatch.setattr(pool_module, "_WorkspaceIOShim", _RecordingShim)

    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=scheduler,
        storage=fake_storage_provider,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        event_bus=bus,
        engine=engine,
    )
    pool._worker_id = "wrk-k1-cancel"

    # The production path for a claimed resumable lease.
    await pool._run_engine_session(lease)

    # The row ended cancelled promptly: no answer, no parked_until.
    row = await sessions.get(sid)
    assert row is not None
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "cancelled"
    assert row.ended_at is not None
    # 1. The CANCELLED transcript record landed (the bare status write
    #    of the old branch wrote no record at all).
    shim = _RecordingShim.instances[-1]
    records = [
        json.loads(line)
        for line in shim.lines.get(sid, b"").decode().splitlines()
        if line.strip()
    ]
    assert any(
        rec.get("kind") == "cancelled" for rec in records
    ), f"no CANCELLED record in the transcript: {records!r}"
    # 2. The session.ended event was recorded for subscribers.
    assert ("session.ended", {"ended_reason": "cancelled"}) in events.emitted
    # 3. The terminal publish a webhook hold waits on.
    assert any(
        key == f"session:{sid}:terminal" for key, _ in bus.published
    ), f"no terminal publish: {bus.published!r}"
    # The lease is dropped: nothing claimable remains for the row.
    assert all(
        l.entity_id != sid for l in await engine.claim_due("wrk-k1-cancel", max_count=10)
    )
    assert not executor_built
    # The delete route's 409 gate (primer/api/routers/sessions.py) reads
    # status == RUNNING and not force under the lifecycle lock: an ENDED
    # row passes it, so a delete without force no longer answers 409.
    assert row.status != SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_a_retried_cancel_on_a_resumable_row_with_no_lease_re_arms_it(
    fake_storage_provider,
):
    """A row a wake already flipped to 'resumable' (whose re-arm was
    lost, so no lease exists): the retried cancel still re-arms it. The
    upsert is idempotent, so a wake that re-armed fine is re-armed
    harmlessly too."""
    await _seed_session(
        fake_storage_provider,
        status=SessionStatus.RUNNING,
        parked_status="resumable",
    )
    engine = _RecordingClaimEngine()
    scheduler = _FakeScheduler()
    deps = _cancel_deps(fake_storage_provider, scheduler, engine)

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-park-1", deps=deps,
    )

    assert out.cancel_requested is True
    assert out.status == SessionStatus.RUNNING
    assert out.parked_status == "resumable"
    # The lease is (re-)armed by the retry.
    assert (ClaimKind.SESSION, "sess-park-1") in engine.marked


@pytest.mark.asyncio
async def test_a_failed_rearm_still_answers_publishes_and_signals(
    fake_storage_provider,
):
    """A mark_resumable that raises must not 500 the cancel: the flag
    and the flip stand, and the bus publish and scheduler signal still
    fire. A retried cancel re-arms (the upsert is idempotent)."""
    await _seed_session(
        fake_storage_provider,
        status=SessionStatus.RUNNING,
        parked_status="parked",
    )
    engine = _RecordingClaimEngine(fail_rearm=True)
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    deps = _cancel_deps(
        fake_storage_provider, scheduler, engine, event_bus=bus
    )

    out = await cancel_session(
        workspace_id="ws-1", session_id="sess-park-1", deps=deps,
    )

    assert out.cancel_requested is True
    assert out.parked_status == "resumable"  # the flip landed
    assert ("session:sess-park-1:cancel", {}) in bus.published
    assert "sess-park-1" in scheduler.signalled
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get("sess-park-1")
    assert row is not None
    assert row.cancel_requested is True
    assert row.parked_status == "resumable"


@pytest.mark.asyncio
async def test_a_row_that_ended_between_read_and_flip_gets_no_lease(
    fake_storage_provider,
):
    """A row that ends between the cancel's read and its flip (the flag
    write stands, the flip's fence is refused): no lease is armed for an
    ENDED row."""
    sid = "sess-park-ended"
    await _seed_session(
        fake_storage_provider,
        status=SessionStatus.RUNNING,
        parked_status="parked",
        sid=sid,
    )

    class _EndAfterFlagStorage:
        """The real fake storage, except: right after the cancel's flag
        write lands, the row is ended (a concurrent end between the read
        and the flip)."""

        def __init__(self, inner) -> None:
            self._inner = inner
            self._flag_written = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def patch_if(self, id, patch=None, *, where, **kwargs: Any):
            out = await self._inner.patch_if(id, patch, where=where, **kwargs)
            if (
                out is not None
                and "cancel_requested" in (patch or {})
                and not self._flag_written
            ):
                self._flag_written = True
                row = await self._inner.get(id)
                row.status = SessionStatus.ENDED
                row.ended_reason = "cancelled"
                row.ended_at = _now()
                await self._inner.update(row)
            return out

    class _Provider:
        """The shared fake provider with the flag-then-end storage for
        the session model only."""

        def __init__(self, inner) -> None:
            self._inner = inner
            self._session_storage = _EndAfterFlagStorage(
                inner.get_storage(WorkspaceSession)
            )

        def get_storage(self, model):
            if model is WorkspaceSession:
                return self._session_storage
            return self._inner.get_storage(model)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    engine = _RecordingClaimEngine()
    bus = _FakeEventBus()
    scheduler = _FakeScheduler()
    deps = _cancel_deps(
        _Provider(fake_storage_provider), scheduler, engine, event_bus=bus
    )

    out = await cancel_session(
        workspace_id="ws-1", session_id=sid, deps=deps,
    )

    # No lease for the ended row.
    assert engine.marked == []
    # The cancel still answers (with the row as it is now) and still
    # publishes and signals.
    assert out is not None
    assert out.status == SessionStatus.ENDED
    assert (f"session:{sid}:cancel", {}) in bus.published
    assert sid in scheduler.signalled
    # The flag stands on the ENDED row (harmless), the park is gone
    # under the end.
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row is not None
    assert row.status == SessionStatus.ENDED
    assert row.cancel_requested is True
