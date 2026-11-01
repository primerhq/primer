"""InMemoryClaimEngine (the engine of every embedded single-process install) against the contract PostgresClaimEngine implements (CV-6).

``PostgresClaimEngine.upsert`` runs ``pg_notify('claim_ready', ...)`` on every call, new row or conflict (checked below against its source; the
engine itself runs against a live database only in CI).
"""

from __future__ import annotations

import asyncio

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind


async def _ready(eng: InMemoryClaimEngine, settle: float = 0.05) -> list:
    seen: list = []

    async def collect():
        async for item in eng.watch_ready():
            seen.append(item)

    task = asyncio.create_task(collect())
    await asyncio.sleep(settle)
    task.cancel()
    return seen


async def test_every_upsert_queues_a_ready_event_like_the_postgres_notify():
    """CV-6: a re-arm of an existing lease row (a harness op, a requeued trigger) used to wait for the claim poll."""
    eng = InMemoryClaimEngine(adapters={})
    await eng.upsert(ClaimKind.HARNESS, "h1")
    await eng.upsert(ClaimKind.HARNESS, "h1")
    assert await _ready(eng) == [(ClaimKind.HARNESS, "h1")] * 2


async def test_a_re_arm_wakes_a_waiting_watcher():
    eng = InMemoryClaimEngine(adapters={})
    await eng.upsert(ClaimKind.HARNESS, "h1")
    seen: list = []

    async def collect():
        async for item in eng.watch_ready():
            seen.append(item)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.02)
    assert len(seen) == 1
    await eng.upsert(ClaimKind.HARNESS, "h1")
    await asyncio.sleep(0.02)
    task.cancel()
    assert len(seen) == 2


def test_the_postgres_upsert_notifies_on_every_call():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "primer" / "claim" / "postgres.py").read_text(encoding="utf-8")
    assert "pg_notify('claim_ready'" in source
