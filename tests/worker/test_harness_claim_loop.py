"""Integration: WorkerPool's _claim_harness_loop picks up a pending harness and executes it."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from matrix.bus.in_memory import InMemoryEventBus
from matrix.claim.factory import ClaimEngineFactory
from matrix.int.claim import ClaimKind
from matrix.model.harness import Harness, HarnessOperation, HarnessStatus
from matrix.model.scheduler import WorkerConfig
from matrix.scheduler.in_memory import InMemoryScheduler
from matrix.worker.pool import WorkerPool


@pytest_asyncio.fixture
async def local_bare_repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "harness.yaml").write_text(
        "apiVersion: matrix/v1\nkind: Harness\nmetadata:\n  name: t\n"
    )
    (work / "overrides.schema.json").write_text('{"type":"object","properties":{}}')
    (work / "templates").mkdir()
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    yield f"file://{bare}"


@pytest.mark.asyncio
async def test_claim_loop_picks_up_pending_harness(
    fake_storage_provider,
    fake_provider_registry,
    local_bare_repo,
):
    bus = InMemoryEventBus()
    await bus.initialize()
    scheduler = InMemoryScheduler(storage_provider=fake_storage_provider)
    now = datetime.now(timezone.utc)
    harness_storage = fake_storage_provider.get_storage(Harness)
    await harness_storage.create(Harness(
        id="h1", slug="sx", name="x",
        git_url=local_bare_repo, ref="main",
        status=HarnessStatus.DRAFT,
        pending_operation=HarnessOperation.FETCH,
        created_at=now,
    ))

    engine = ClaimEngineFactory.create(
        storage_provider=fake_storage_provider,
        event_bus=bus,
    )
    pool = WorkerPool(
        config=WorkerConfig(
            concurrency=4,
            claim_batch_size=2,
            heartbeat_interval_seconds=5,
            lease_ttl_seconds=15,
            poll_interval_seconds=0.1,
            drain_timeout_seconds=5,
        ),
        scheduler=scheduler,
        storage=fake_storage_provider,
        workspace_registry=None,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=None,
        engine=engine,
    )
    try:
        await pool.start()
        # Seed the engine so the claim loop picks up the harness.
        await engine.upsert(ClaimKind.HARNESS, "h1", priority=100)
        # Poll storage for the harness reaching READY (fetch done)
        row = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            row = await harness_storage.get("h1")
            if row is not None and row.status == HarnessStatus.READY:
                break
        assert row is not None
        assert row.status == HarnessStatus.READY, (
            f"worker did not complete fetch: status={row.status!r}, "
            f"error={row.last_operation_error!r}"
        )
        assert row.overrides_schema is not None
        assert row.pending_operation is None
    finally:
        await pool.drain_and_stop(timeout=5)
        await bus.aclose()
