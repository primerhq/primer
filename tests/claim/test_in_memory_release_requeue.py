"""InMemoryClaimEngine (the engine of every embedded single-process install) against the contract PostgresClaimEngine implements (CV-7).

The Postgres release sets ``next_attempt_at = now() + requeue`` for everything but a lease-only hand-back with no explicit delay. That statement is
checked below against the source, so a change there that these tests do not follow fails here; the engine itself runs against a live database only in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome

START = datetime(2026, 1, 1, tzinfo=UTC)


async def _released(outcome: ReleaseOutcome, *, armed: datetime) -> InMemoryClaimEngine:
    now = [START]
    eng = InMemoryClaimEngine(adapters={}, clock=lambda: now[0])
    await eng.upsert(ClaimKind.HARNESS, "h1", next_attempt_at=armed)
    (lease,) = await eng.claim_due("w1", max_count=1)
    now[0] = START + timedelta(hours=1)
    await eng.release(lease, outcome=outcome)
    return eng


def _next(eng: InMemoryClaimEngine) -> datetime:
    return eng._leases[(ClaimKind.HARNESS, "h1")].next_attempt_at


async def test_a_release_without_requeue_sets_next_attempt_at_to_now():
    """CV-7: Postgres sets ``now() + 0``; the in-memory row kept its old tick and so its place ahead of fresh leases."""
    eng = await _released(ReleaseOutcome(success=True), armed=START - timedelta(days=30))
    assert _next(eng) == START + timedelta(hours=1)


async def test_a_failed_release_also_sets_next_attempt_at_to_now():
    eng = await _released(ReleaseOutcome(success=False, last_error="boom"), armed=START - timedelta(days=30))
    assert _next(eng) == START + timedelta(hours=1)


async def test_a_release_with_requeue_sets_now_plus_the_delay():
    eng = await _released(ReleaseOutcome(success=True, requeue_after=timedelta(seconds=90)), armed=START - timedelta(days=30))
    assert _next(eng) == START + timedelta(hours=1, seconds=90)


async def test_a_lease_only_hand_back_keeps_its_place_in_line():
    """``entity_noop`` with no explicit delay leaves ``next_attempt_at`` alone (Postgres: the CASE's first arm)."""
    armed = START - timedelta(days=30)
    eng = await _released(ReleaseOutcome(success=True, entity_noop=True), armed=armed)
    assert _next(eng) == armed


async def test_a_lease_only_hand_back_with_a_delay_still_requeues():
    eng = await _released(ReleaseOutcome(success=True, entity_noop=True, requeue_after=timedelta(seconds=30)), armed=START - timedelta(days=30))
    assert _next(eng) == START + timedelta(hours=1, seconds=30)


def test_the_postgres_statements_are_the_ones_these_tests_describe():
    source = (Path(__file__).resolve().parents[2] / "primer" / "claim" / "postgres.py").read_text(encoding="utf-8")
    assert "next_attempt_at    = CASE WHEN $8 THEN next_attempt_at" in source
    assert "outcome.entity_noop and outcome.requeue_after is None" in source
