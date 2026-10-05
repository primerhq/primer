"""A release that times out has an UNKNOWN outcome, and one bounded probe decides it.

``WorkerPool._release_lease`` bounds ``engine.release``, and the bound also covers what follows the commit (the
post-release hook, the COMMIT's own reply). A timeout used to be treated as a failed release, so the session
handler skipped ``_maybe_rearm_session``; when the release had in fact committed, its lease was already gone and a
steer that landed during the turn (``turn_status == "claimable"``) was stranded with no lease. Now one
``has_lease`` probe decides: row gone, the release committed and the handler carries on as after any release (the
steer is re-armed); row still there, or the probe cannot tell, the lease is left to expire for a peer, as before.

Every test runs two sessions: ``hung`` (whose release times out) and ``steady`` (an unrelated release that returns).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider

WORKER = "wrk-outcome"


def _session(sid: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )


async def _until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


class _World:
    """Two sessions on a real pool, each turn ending with a steer queued (``turn_status == "claimable"``).

    ``hang`` decides where the ``hung`` session's release stops answering: ``"after_commit"`` (the lease is gone,
    then the post-commit part never returns) or ``"before_commit"`` (nothing was released)."""

    def __init__(self, monkeypatch, hang: str) -> None:
        self.storage = _FakeStorageProvider()
        self.sessions = self.storage.get_storage(WorkspaceSession)
        self.engine = InMemoryClaimEngine(
            adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=self.sessions)},
        )
        self.scheduler = InMemoryScheduler()
        self.pool = WorkerPool(
            config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
            scheduler=self.scheduler, storage=self.storage,
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=self.engine,
        )
        self.pool._worker_id = WORKER
        self.pool._dispatch = {ClaimKind.SESSION: self.pool._run_engine_session}
        self.pool._release_timeout_seconds = 0.3
        self.upserts: list[str] = []
        monkeypatch.setattr("primer.worker.pool.run_one_session_turn", self._turn)

        real_release = self.engine.release
        real_upsert = self.engine.upsert

        async def release(lease, *, outcome):
            if lease.entity_id != "hung":
                return await real_release(lease, outcome=outcome)
            if hang == "after_commit":
                await real_release(lease, outcome=outcome)   # committed: the lease row is gone ...
            await asyncio.Event().wait()                     # ... and then nothing answers

        async def upsert(kind, entity_id, **kw):
            self.upserts.append(entity_id)
            await real_upsert(kind, entity_id, **kw)

        self.engine.release = release  # type: ignore[method-assign]
        self._upsert = upsert

    async def _turn(self, engine_lease, _deps) -> ReleaseOutcome:
        row = await self.sessions.get(engine_lease.entity_id)
        await self.sessions.update(row.model_copy(update={"turn_status": "claimable"}))
        return ReleaseOutcome(success=True, drop_lease=True)

    async def run(self) -> None:
        await self.scheduler.initialize()
        for sid in ("hung", "steady"):
            await self.sessions.create(_session(sid))
            await self.engine.upsert(ClaimKind.SESSION, sid)
        leases = await self.engine.claim_due(WORKER, max_count=10)
        assert {lease.entity_id for lease in leases} == {"hung", "steady"}
        self.engine.upsert = self._upsert  # type: ignore[method-assign]   # count only the re-arms from here
        self.pool._reserve_and_dispatch(leases)
        await _until(lambda: not self.pool._in_flight, "a session never finished: the release was not bounded")

    async def close(self) -> None:
        for task in list(self.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*self.pool._turn_tasks, return_exceptions=True)
        await self.scheduler.aclose()


@pytest.mark.asyncio
async def test_a_release_that_committed_and_then_hung_still_rearms_the_queued_steer(monkeypatch):
    w = _World(monkeypatch, hang="after_commit")
    try:
        await w.run()
        assert w.pool._release_timeouts_total == 1
        assert w.pool._release_timeouts_committed_total == 1
        assert w.pool.metrics_snapshot()["primer_worker_release_timeouts_committed_total"] == 1
        # The steer has a lease again: a FRESH, unclaimed one (the old claim was released by the commit).
        assert sorted(w.upserts) == ["hung", "steady"]
        row = w.engine._leases[(ClaimKind.SESSION, "hung")]
        assert row.claimed_by is None
    finally:
        await w.close()


@pytest.mark.asyncio
async def test_a_release_that_hung_before_committing_is_left_to_expire_and_not_rearmed(monkeypatch):
    w = _World(monkeypatch, hang="before_commit")
    try:
        await w.run()
        assert w.pool._release_timeouts_total == 1
        assert w.pool._release_timeouts_committed_total == 0
        assert w.upserts == ["steady"], "the hung session was re-armed although its release never committed"
        # Its lease is the old claim, still this worker's: it expires and a peer re-claims it.
        row = w.engine._leases[(ClaimKind.SESSION, "hung")]
        assert row.claimed_by == WORKER
    finally:
        await w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["raises", "hangs"])
async def test_a_probe_that_cannot_answer_counts_as_unknown_and_nothing_is_rearmed(monkeypatch, probe):
    """Even though the release DID commit here, a probe that fails or does not answer within its own bound
    leaves the outcome unknown: the handler finishes (the probe is bounded) and treats it as a failed release."""
    w = _World(monkeypatch, hang="after_commit")
    w.pool._release_probe_timeout_seconds = 0.3

    async def has_lease(kind, entity_id):
        if probe == "raises":
            raise ConnectionError("the probe's connection is gone too")
        await asyncio.Event().wait()

    w.engine.has_lease = has_lease  # type: ignore[method-assign]
    try:
        await w.run()
        assert w.pool._release_timeouts_total == 1
        assert w.pool._release_timeouts_committed_total == 0
        assert w.upserts == ["steady"]
    finally:
        await w.close()
