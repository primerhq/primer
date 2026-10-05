from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, PostReleaseWake, ReleaseOutcome


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _RecordingStorage:
    def __init__(self, row):
        self._row = row
        self.get_conns = []
        self.update_conns = []
        self.patch_conns = []

    async def get(self, id, *, conn=None):
        self.get_conns.append(conn)
        return self._row

    async def update(self, entity, *, conn=None):
        self.update_conns.append(conn)
        self._row = entity
        return entity

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        self.patch_conns.append(conn)
        self._row = type(self._row).model_validate({**self._row.model_dump(mode="json"), **patch})
        return self._row




@pytest.mark.asyncio
async def test_sessions_adapter_forwards_conn():
    from primer.claim.adapters.sessions import SessionClaimAdapter
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    row = WorkspaceSession(
        id="s1",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
    )
    storage = _RecordingStorage(row)
    adapter = SessionClaimAdapter(session_storage=storage)
    sentinel = object()
    # Success / non-park release: avoids the WorkspaceIO failure branch.
    await adapter.on_release(
        sentinel, "s1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    # The release is one field-scoped patch_if on the caller's transaction (no whole-document update).
    assert storage.patch_conns == [sentinel]
    assert storage.update_conns == []


@pytest.mark.asyncio
async def test_harnesses_adapter_forwards_conn():
    from primer.claim.adapters.harnesses import HarnessClaimAdapter
    from primer.model.harness import Harness, HarnessOperation

    row = Harness(
        id="h1",
        slug="my-harness",
        name="My Harness",
        git_url="https://example.com/repo.git",
        created_at=_now(),
        pending_operation=HarnessOperation.FETCH,
    )
    storage = _RecordingStorage(row)
    adapter = HarnessClaimAdapter(harness_storage=storage)
    sentinel = object()
    await adapter.on_release(
        sentinel, "h1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    assert storage.update_conns == [sentinel]


@pytest.mark.asyncio
async def test_in_memory_release_is_fenced_on_ownership():
    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    await engine.upsert(ClaimKind.HARNESS, "c1")
    leases = await engine.claim_due("worker-A", max_count=1)
    lease_a = leases[0]
    assert lease_a.claimed_by == "worker-A"
    # Simulate re-claim by worker B.
    engine._leases[(ClaimKind.HARNESS, "c1")].claimed_by = "worker-B"
    await engine.release(lease_a, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert calls == []  # on_release NOT called (stale worker fenced out)
    assert engine._leases[(ClaimKind.HARNESS, "c1")].claimed_by == "worker-B"  # lease untouched


@pytest.mark.asyncio
async def test_in_memory_release_runs_when_still_owned():
    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    await engine.upsert(ClaimKind.HARNESS, "c2")
    leases = await engine.claim_due("worker-A", max_count=1)
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert calls == ["c2"]  # owned -> on_release ran


@pytest.mark.asyncio
async def test_in_memory_release_with_a_stale_claimed_at_still_runs_on_release():
    """The fence is ``claimed_by`` only; ``claimed_at`` is NOT part of it.

    ``worker_id`` is per pool start and the pool never runs a second executor for a key it
    already has in flight (a same-worker duplicate claim is skipped), so a same-worker release
    that carries an older ``claimed_at`` is the sole in-flight execution finishing, not a
    zombie. A ``claimed_at`` term in the fence would drop its ``on_release`` (the park write,
    the tool result) and lose the work.
    """
    from datetime import timedelta

    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    await engine.upsert(ClaimKind.HARNESS, "c3")
    first = (await engine.claim_due("worker-A", max_count=1))[0]
    # Its heartbeat stalls: the lease expires and the SAME worker claims the row again.
    engine._leases[(ClaimKind.HARNESS, "c3")].expires_at = _now() - timedelta(seconds=1)
    second = (await engine.claim_due("worker-A", max_count=1))[0]
    assert second.claimed_by == first.claimed_by == "worker-A"
    assert second.claimed_at != first.claimed_at

    await engine.release(first, outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert calls == ["c3"], "a release with an older claimed_at was fenced out"
    assert (ClaimKind.HARNESS, "c3") not in engine._leases


@pytest.mark.asyncio
async def test_in_memory_release_fires_post_release_hook_after_mutation():
    """01a0518b review, required test: the bound hook must fire strictly
    AFTER release()'s own lease mutation is applied - "post-commit"
    degrades to "post-mutation" for the in-memory engine (no real
    transaction), per PostReleaseWake's own docstring. Proven by having
    the hook itself inspect engine state: if it ever ran BEFORE the
    drop, the lease would still be present when it fires."""
    from primer.claim.in_memory import InMemoryClaimEngine

    class _Adapter:
        kind = ClaimKind.TOOL_CALL
        entity_table = "toolcalltask"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return PostReleaseWake(
                session_id="s1", event_key="tool_wait:s1:0",
                payload={"tool_wait_ready": True},
            )

    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: _Adapter()})
    await engine.upsert(ClaimKind.TOOL_CALL, "t1")
    leases = await engine.claim_due("worker-A", max_count=1)

    hook_calls: list[PostReleaseWake] = []
    lease_already_dropped: list[bool] = []

    async def _hook(signal: PostReleaseWake) -> None:
        hook_calls.append(signal)
        lease_already_dropped.append(
            (ClaimKind.TOOL_CALL, "t1") not in engine._leases
        )

    engine.bind_post_release_hook(_hook)
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert hook_calls == [PostReleaseWake(
        session_id="s1", event_key="tool_wait:s1:0",
        payload={"tool_wait_ready": True},
    )]
    assert lease_already_dropped == [True]


@pytest.mark.asyncio
async def test_in_memory_release_skips_hook_when_signal_is_none():
    """Every OTHER adapter (and this one, absent a batch) returns None -
    the hook must never fire for those."""
    from primer.claim.in_memory import InMemoryClaimEngine

    class _Adapter:
        kind = ClaimKind.TOOL_CALL
        entity_table = "toolcalltask"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return None

    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: _Adapter()})
    await engine.upsert(ClaimKind.TOOL_CALL, "t1")
    leases = await engine.claim_due("worker-A", max_count=1)

    hook_calls = []
    engine.bind_post_release_hook(lambda signal: hook_calls.append(signal))
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert hook_calls == []


class _SpyTxnConn:
    """asyncpg-conn stand-in that records the ORDER of transaction
    enter/exit against on_release, without a live database - proves the
    exact ordering PostReleaseWake's own docstring promises for the
    Postgres arm (the InMemory engine has no real transaction to prove
    this against at all)."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.in_transaction = False

    def transaction(self):
        conn = self

        class _TxnCtx:
            async def __aenter__(self_inner):
                conn.in_transaction = True
                conn._events.append("txn_enter")
                return conn

            async def __aexit__(self_inner, *exc):
                conn._events.append("txn_exit")
                conn.in_transaction = False
                return False

        return _TxnCtx()

    async def fetchval(self, query, *args):
        # The lease-ownership fence check inside release() (DELETE ...
        # RETURNING 1 / UPDATE ... RETURNING 1) - always "owned" here,
        # unrelated to what this test proves.
        return 1


class _SpyPool:
    def __init__(self, conn: _SpyTxnConn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _SpyStorageProvider:
    def __init__(self, conn: _SpyTxnConn) -> None:
        self.pool = _SpyPool(conn)
        self.leases_table = '"primer"."leases"'
        self.schema = "primer"


@pytest.mark.asyncio
async def test_postgres_release_fires_post_release_hook_after_transaction_exits():
    """01a0518b review, required test (the Postgres arm - PostReleaseWake's
    own docstring is written specifically about this engine's hazard, and
    it had no test at all): the bound hook must fire strictly AFTER
    `async with conn.transaction()` has exited, never from inside it -
    that is the entire reason PostReleaseWake exists instead of an
    adapter calling the wake code directly from inside on_release. Proven
    by having both on_release and the hook record conn.in_transaction at
    the moment they run, plus the raw event order."""
    from primer.claim.postgres import PostgresClaimEngine

    events: list[str] = []
    in_transaction_at: dict[str, bool] = {}
    conn = _SpyTxnConn(events)

    class _Adapter:
        kind = ClaimKind.TOOL_CALL
        entity_table = "toolcalltask"

        def eligibility_sql(self) -> str:
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            events.append("on_release")
            in_transaction_at["on_release"] = conn.in_transaction
            return PostReleaseWake(
                session_id="s1", event_key="tool_wait:s1:0",
                payload={"tool_wait_ready": True},
            )

    engine = PostgresClaimEngine(
        storage_provider=_SpyStorageProvider(conn),
        adapters={ClaimKind.TOOL_CALL: _Adapter()},
    )

    async def _hook(signal: PostReleaseWake) -> None:
        events.append("hook")
        in_transaction_at["hook"] = conn.in_transaction

    engine.bind_post_release_hook(_hook)

    from primer.int.claim import Lease

    lease = Lease(
        kind=ClaimKind.TOOL_CALL, entity_id="t1", claimed_by="worker-A",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert events == ["txn_enter", "on_release", "txn_exit", "hook"]
    assert in_transaction_at == {"on_release": True, "hook": False}


@pytest.mark.asyncio
async def test_postgres_release_skips_hook_when_signal_is_none():
    """Mirrors the InMemory sibling: every OTHER adapter (and this one,
    absent a batch) returns None - the hook must never fire, and no
    transaction-exit-then-fire ordering claim even arises."""
    from primer.claim.postgres import PostgresClaimEngine
    from primer.int.claim import Lease

    events: list[str] = []
    conn = _SpyTxnConn(events)

    class _Adapter:
        kind = ClaimKind.TOOL_CALL
        entity_table = "toolcalltask"

        def eligibility_sql(self) -> str:
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return None

    engine = PostgresClaimEngine(
        storage_provider=_SpyStorageProvider(conn),
        adapters={ClaimKind.TOOL_CALL: _Adapter()},
    )
    hook_calls: list[PostReleaseWake] = []
    engine.bind_post_release_hook(lambda signal: hook_calls.append(signal))

    lease = Lease(
        kind=ClaimKind.TOOL_CALL, entity_id="t1", claimed_by="worker-A",
        claimed_at=_now(), expires_at=_now(), attempt_count=0, last_error=None,
    )
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert hook_calls == []
    assert events == ["txn_enter", "txn_exit"]


@pytest.mark.asyncio
async def test_triggers_adapter_forwards_conn():
    from primer.claim.adapters.triggers import TriggerClaimAdapter
    from primer.model.trigger import DelayedTriggerConfig, Trigger

    row = Trigger(
        id="t1",
        slug="my-trigger",
        name="My Trigger",
        config=DelayedTriggerConfig(fire_at=_now()),
        created_at=_now(),
        next_fire_at=_now(),
    )
    storage = _RecordingStorage(row)
    adapter = TriggerClaimAdapter(storage=storage)
    sentinel = object()
    await adapter.on_release(
        sentinel, "t1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    assert storage.update_conns == [sentinel]


@pytest.mark.asyncio
async def test_in_memory_entity_noop_release_skips_on_release_and_only_moves_the_lease():
    """A lease-only release: the kind's ``on_release`` is not called (the entity is neither read nor
    written), and the lease is requeued or dropped as the outcome says."""
    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    for key in ("requeue", "drop"):
        await engine.upsert(ClaimKind.HARNESS, key)
    leases = {lease.entity_id: lease for lease in await engine.claim_due("worker-A", max_count=8)}

    await engine.release(leases["requeue"], outcome=ReleaseOutcome(success=True, entity_noop=True))
    await engine.release(
        leases["drop"], outcome=ReleaseOutcome(success=True, entity_noop=True, drop_lease=True),
    )

    assert calls == [], "entity_noop must not call on_release"
    assert engine._leases[(ClaimKind.HARNESS, "requeue")].claimed_by is None      # claimable again
    assert (ClaimKind.HARNESS, "drop") not in engine._leases
    reclaimed = await engine.claim_due("worker-B", max_count=8)
    assert [x.entity_id for x in reclaimed] == ["requeue"]


@pytest.mark.asyncio
async def test_in_memory_entity_noop_requeue_leaves_attempt_count_and_last_error_alone():
    """A hand-back is not a run: it must not reset a failing lease's history (a task at attempt 4 of 5
    that is handed back would otherwise start again at 0) and must not push it behind its peers."""
    from datetime import timedelta

    from primer.claim.in_memory import InMemoryClaimEngine

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return None

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    await engine.upsert(ClaimKind.HARNESS, "c")
    for _ in range(3):                                   # three real failures
        [lease] = await engine.claim_due("worker-A", max_count=1)
        await engine.release(lease, outcome=ReleaseOutcome(success=False, last_error="boom"))
    row = engine._leases[(ClaimKind.HARNESS, "c")]
    assert (row.attempt_count, row.last_error) == (3, "boom")
    before = row.next_attempt_at
    [lease] = await engine.claim_due("worker-A", max_count=1)

    await engine.release(lease, outcome=ReleaseOutcome(success=True, entity_noop=True))

    assert (row.attempt_count, row.last_error) == (3, "boom")
    assert row.claimed_by is None and row.next_attempt_at == before
    [again] = await engine.claim_due("worker-B", max_count=1)
    await engine.release(
        again, outcome=ReleaseOutcome(success=True, entity_noop=True, requeue_after=timedelta(seconds=60)),
    )
    assert row.next_attempt_at > before and row.attempt_count == 3


def test_entity_noop_cannot_be_combined_with_a_park():
    from datetime import timedelta

    from primer.int.claim import ParkRequest

    park = ParkRequest(
        parked_state={}, parked_event_key="k", parked_until=None, parked_at=_now(),
    )
    with pytest.raises(ValueError):
        ReleaseOutcome(success=True, entity_noop=True, park=park)
    with pytest.raises(ValueError):
        ReleaseOutcome(success=True, entity_noop=True, preserve_park=True)
    assert ReleaseOutcome(success=True, entity_noop=True, requeue_after=timedelta(seconds=1)).entity_noop
