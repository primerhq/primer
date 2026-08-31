"""S7: a stable, bounded worker label for metric series.

The lease-ownership id stays a per-start uuid (pool.py); this label is the
second identity, stable across restarts so a restarted worker keeps its
Prometheus series instead of orphaning one per process start.
"""

from __future__ import annotations

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.model.scheduler import WorkerConfig
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.identity import stable_worker_label
from primer.worker.pool import WorkerPool


def test_label_is_sanitised_hostname_plus_index(monkeypatch):
    monkeypatch.setattr(
        "primer.worker.identity.socket.gethostname", lambda: "Primer.Worker-1",
    )
    assert stable_worker_label(WorkerConfig(worker_index=3)) == "primer-worker-1-3"


def test_explicit_label_wins():
    assert stable_worker_label(WorkerConfig(worker_label="Pool A")) == "pool-a"


def test_label_is_stable_across_calls():
    cfg = WorkerConfig()
    assert stable_worker_label(cfg) == stable_worker_label(cfg)


def test_empty_hostname_falls_back(monkeypatch):
    monkeypatch.setattr("primer.worker.identity.socket.gethostname", lambda: "!!!")
    assert stable_worker_label(WorkerConfig()) == "unknown-0"


async def test_pool_exposes_the_label_without_start():
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    try:
        pool = WorkerPool(
            config=WorkerConfig(concurrency=1, worker_label="lane-a"),
            scheduler=scheduler,
            storage=None,                  # type: ignore[arg-type]
            workspace_registry=None,       # type: ignore[arg-type]
            provider_registry=None,        # type: ignore[arg-type]
            engine=InMemoryClaimEngine(adapters={}),
        )
        assert pool.worker_label == "lane-a"
        assert pool.worker_id == ""
    finally:
        await scheduler.aclose()


async def test_two_pools_on_one_host_keep_distinct_lease_ids():
    """The label may collide; the lease-ownership id must not."""
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    engine = InMemoryClaimEngine(adapters={})
    pools = [
        WorkerPool(
            config=WorkerConfig(concurrency=1),
            scheduler=scheduler,
            storage=None,                  # type: ignore[arg-type]
            workspace_registry=None,       # type: ignore[arg-type]
            provider_registry=None,        # type: ignore[arg-type]
            engine=engine,
        )
        for _ in range(2)
    ]
    try:
        for pool in pools:
            await pool.start()
        assert pools[0].worker_label == pools[1].worker_label
        assert pools[0].worker_id != pools[1].worker_id
    finally:
        for pool in pools:
            await pool.drain_and_stop()
        await scheduler.aclose()


def test_worker_index_is_bounded():
    with pytest.raises(ValueError):
        WorkerConfig(worker_index=-1)
