"""Review 2026-11-23 finding 1.3: a harness operation enqueued between the worker's row
release and its lease release used to be silently erased and the harness marked READY.

The fix closes the window on both sides:

* The adapter's ``on_release`` leaves the row alone on a successful release that still
  sees a pending_operation (an operation enqueued in the gap), and the engine handler
  re-reads the row before releasing: when it sees an operation that is not the one the
  claim ran, it keeps the lease (``drop_lease=False``) so the new operation stays
  claimable.
* The ``harness:{id}:done`` event is published by the engine handler AFTER the lease
  release, so a consumer that reacts to done by enqueuing the next operation does not
  land in the gap at all.

These tests drive the real claim path (a real ``InMemoryClaimEngine`` +
``HarnessClaimAdapter`` over real SQLite storage) and inject the gap deterministically:
the dispatched SYNC fails (fetch_required, no git), and a hook that runs right after the
row release enqueues a FETCH through the route's exact sequence (a fenced ``patch_if``
on ``pending_operation: None`` then the lease upsert). The gap operation differs from the
dispatched one, so the fresh re-read can tell them apart.

Round 1 adds the SAME-operation case: the enqueue routes guard only on
``pending_operation`` (not on status), so a second request for the same operation lands
in the gap the moment the row release clears it. A success always clears the dispatched
operation, so a pending operation after a success is a new request whatever it names:
the lease must be kept even when it names the operation just dispatched.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.harness import dispatch as harness_dispatch
from primer.harness.hashes import hash_overrides
from primer.int.claim import CLAIM_PRIORITY_OPERATOR, ClaimKind
from primer.model.harness import Harness, HarnessOperation, HarnessRendering, HarnessStatus
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.storage.factory import StorageProviderFactory
from primer.worker.engine_handlers import run_engine_harness
from tests._support.harness_ops import enqueued


@pytest_asyncio.fixture
async def sp(tmp_path):
    p = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "gap.sqlite")))
    await p.initialize()
    await p.get_content_store().ensure_schema()
    yield p
    await p.aclose()


class _Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, key, payload):
        self.events.append(key)


def _harness(hid: str = "gap-id") -> Harness:
    return Harness(
        id=hid, slug="acme", name="Acme", git_url="https://github.com/x/y",
        created_at=datetime.now(timezone.utc),
    )


def _engine(sp) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(adapters={
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=sp.get_storage(Harness)),
    })


class _Pool:
    """The minimal worker pool the engine handler reads: its ``_release_lease``
    releases through the real engine, so the release (and its on_release) is the
    real production path."""

    def __init__(self, sp, bus, engine, *, worker_id: str = "w1") -> None:
        self._storage = sp
        self._event_bus = bus
        self._provider_registry = None
        self._semantic_search_registry = None
        self._worker_id = worker_id
        self._engine = engine

    async def _release_lease(self, lease, outcome):
        await self._engine.release(lease, outcome=outcome)


async def _claim(sp, engine, hid, *, worker_id: str = "w1"):
    await engine.upsert(ClaimKind.HARNESS, hid)
    [lease] = await engine.claim_due(worker_id, max_count=1, kinds=[ClaimKind.HARNESS])
    return lease


@pytest.mark.asyncio
async def test_an_operation_enqueued_in_the_gap_survives_and_stays_claimable(
    sp, monkeypatch: pytest.MonkeyPatch,
):
    harness_storage = sp.get_storage(Harness)
    # A SYNC that will fail (available_commit is None => fetch_required): dispatch
    # releases the row (pending_operation=None, status=ERROR) and returns normally.
    h = enqueued(_harness(), HarnessOperation.SYNC)
    await harness_storage.create(h)
    engine = _engine(sp)
    lease = await _claim(sp, engine, h.id)
    bus = _Bus()

    real_run = harness_dispatch.run_one_harness_operation

    async def _gap_dispatch(deps, *, harness_id, worker_id):
        # The real operation: it fails (fetch_required) and releases the row.
        await real_run(deps, harness_id=harness_id, worker_id=worker_id)
        # The gap: a REST enqueue that lands after the row release and before the
        # lease release. The route's exact sequence - a fenced patch_if (the row's
        # pending_operation must still be None), then the lease upsert.
        row = await harness_storage.get(harness_id)
        assert row.pending_operation is None, "dispatch must have released the row"
        enq = await harness_storage.patch_if(
            harness_id,
            {"pending_operation": HarnessOperation.FETCH.value},
            where={"pending_operation": [None]},
        )
        assert enq is not None, "the gap enqueue's fence must hold"
        await engine.upsert(
            ClaimKind.HARNESS, harness_id, priority=CLAIM_PRIORITY_OPERATOR,
        )

    monkeypatch.setattr(harness_dispatch, "run_one_harness_operation", _gap_dispatch)
    await run_engine_harness(_Pool(sp, bus, engine), lease)

    # The gap operation is not erased: the row keeps it, and the failed SYNC's
    # status is not clobbered back to READY.
    row = await harness_storage.get(h.id)
    assert row.pending_operation == HarnessOperation.FETCH
    assert row.status == HarnessStatus.ERROR
    # The lease was kept (not dropped), so a second worker can claim the new op.
    claimed = await engine.claim_due("w2", max_count=1, kinds=[ClaimKind.HARNESS])
    assert [lease.entity_id for lease in claimed] == [h.id]
    # done is published once, by the handler after the release.
    assert bus.events == [f"harness:{h.id}:done"]


@pytest.mark.asyncio
async def test_without_a_gap_the_lease_drops_and_done_publishes_once(sp):
    harness_storage = sp.get_storage(Harness)
    # The same failing SYNC, but nothing enqueues in the gap: the operation is
    # cleared, the lease is dropped, and done is published exactly once.
    h = enqueued(_harness(hid="no-gap-id"), HarnessOperation.SYNC)
    await harness_storage.create(h)
    engine = _engine(sp)
    lease = await _claim(sp, engine, h.id)
    bus = _Bus()

    await run_engine_harness(_Pool(sp, bus, engine), lease)

    row = await harness_storage.get(h.id)
    assert row.pending_operation is None
    assert not await engine.has_lease(ClaimKind.HARNESS, h.id)
    assert bus.events == [f"harness:{h.id}:done"]


@pytest.mark.asyncio
async def test_the_same_operation_enqueued_in_the_gap_survives_and_keeps_its_lease(
    sp, monkeypatch: pytest.MonkeyPatch,
):
    """Round 1 (review 2026-11-23): a second request for the SAME operation
    lands in the gap and the lease must be kept.

    The enqueue routes guard only on ``pending_operation`` (not on status), so
    a second FETCH while the first is in flight is the dominant same-op case:
    the first row release clears the operation, the second request's fenced
    enqueue lands, and the lease release follows. A success always clears the
    dispatched operation, so the fresh re-read seeing the same operation is a
    new request by construction: the old formula (``fresh.pending_operation !=
    dispatched_operation``) missed it, dropped the lease, and the harness sat
    with a pending operation and no lease - every route, delete included,
    answered 409 until a restart.

    The dispatched SYNC SUCCEEDS through the real fast path (no git): the
    bundle hash matches the rendering's and the overrides are unchanged, so
    ``_do_sync`` just refreshes the row and returns INSTALLED.
    """
    harness_storage = sp.get_storage(Harness)
    h = enqueued(
        _harness(hid="same-op-id"), HarnessOperation.SYNC,
        bundle_hash="b1", available_bundle_hash="b1", available_commit="c1",
    )
    await harness_storage.create(h)
    await sp.get_storage(HarnessRendering).create(HarnessRendering(
        id=h.id, harness_id=h.id,
        bundle_hash="b1", overrides_hash=hash_overrides({}),
        schema_hash=None, entries=[],
        rendered_at=datetime.now(timezone.utc),
    ))
    engine = _engine(sp)
    lease = await _claim(sp, engine, h.id)
    bus = _Bus()

    real_run = harness_dispatch.run_one_harness_operation

    async def _gap_dispatch(deps, *, harness_id, worker_id):
        # The real operation: it succeeds (the fast path) and releases the
        # row (pending_operation=None, status=INSTALLED).
        await real_run(deps, harness_id=harness_id, worker_id=worker_id)
        row = await harness_storage.get(harness_id)
        assert row.pending_operation is None, "dispatch must have released the row"
        # The gap: a second request for the SAME operation, the route's exact
        # sequence (fenced patch_if then the lease upsert).
        enq = await harness_storage.patch_if(
            harness_id,
            {"pending_operation": HarnessOperation.SYNC.value},
            where={"pending_operation": [None]},
        )
        assert enq is not None, "the gap enqueue's fence must hold"
        await engine.upsert(
            ClaimKind.HARNESS, harness_id, priority=CLAIM_PRIORITY_OPERATOR,
        )

    monkeypatch.setattr(harness_dispatch, "run_one_harness_operation", _gap_dispatch)
    await run_engine_harness(_Pool(sp, bus, engine), lease)

    # The second request is not erased: the row keeps it.
    row = await harness_storage.get(h.id)
    assert row.pending_operation == HarnessOperation.SYNC
    assert row.status == HarnessStatus.INSTALLED
    # The lease was kept (the old code dropped it: the 409 wedge), so a
    # worker claims the second request.
    claimed = await engine.claim_due("w2", max_count=1, kinds=[ClaimKind.HARNESS])
    assert [lease.entity_id for lease in claimed] == [h.id]
    assert bus.events == [f"harness:{h.id}:done"]
