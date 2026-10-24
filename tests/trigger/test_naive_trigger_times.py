"""A trigger time without a zone is read as UTC, and one naive time can never stop the claim (ticket 01a16e79, review of #928, probe p8).

Before: a delayed trigger whose ``fire_at`` came without a zone was stored as given, its naive ``next_fire_at`` was armed on the claim
lease, and the in-memory engine's ``claim_due`` compared it with its aware clock and raised ``TypeError``, for EVERY kind on EVERY poll:
one user-made trigger stopped all work on a SQLite deployment. The lead's ruling, both halves pinned here:

* the trigger model reads a naive ``fire_at`` / ``next_fire_at`` as UTC, on input and on the read of a stored row (what Postgres's
  ``::timestamptz`` does under a UTC session);
* the in-memory engine reads a lease's naive ``next_attempt_at`` as UTC, so a legacy row armed before the model change cannot stop
  the claim either.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.triggers import TriggerClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.trigger import DelayedTriggerConfig, ScheduledTriggerConfig, Trigger
from primer.storage.sqlite import SqliteStorageProvider

UTC = timezone.utc
NAIVE = datetime(2026, 5, 1, 9, 30)                      # noqa: DTZ001 - a time given without a zone is the point


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "naive.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _trigger(tid: str, *, next_fire_at: datetime, config=None) -> Trigger:
    return Trigger(
        id=tid, slug=f"t-{tid}", name="t", config=config or ScheduledTriggerConfig(cron="0 9 * * *", timezone="UTC"),
        enabled=True, next_fire_at=next_fire_at, created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _store_naive(sp, tid: str, naive: datetime) -> None:
    """Rewrite the stored ``next_fire_at`` (and a delayed ``fire_at``) without a zone: a row an older build saved."""
    text = naive.isoformat()
    await sp.connection.execute(
        "UPDATE \"trigger\" SET data = json_set(data, '$.next_fire_at', ?) WHERE id = ?", (text, tid),
    )
    await sp.connection.execute(
        "UPDATE \"trigger\" SET data = json_set(data, '$.config.fire_at', ?) "
        "WHERE id = ? AND json_extract(data, '$.config.kind') = 'delayed'", (text, tid),
    )
    await sp.connection.commit()
    cur = await sp.connection.execute("SELECT json_extract(data, '$.next_fire_at') FROM \"trigger\" WHERE id = ?", (tid,))
    assert (await cur.fetchone())[0] == text, "the premise: the stored value has no zone"


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def test_a_naive_fire_at_and_next_fire_at_are_read_as_utc_on_input():
    config = DelayedTriggerConfig(fire_at=NAIVE)
    trigger = _trigger("t1", next_fire_at=NAIVE, config=config)

    assert config.fire_at == NAIVE.replace(tzinfo=UTC)
    assert trigger.next_fire_at == NAIVE.replace(tzinfo=UTC)


def test_an_aware_time_is_kept_as_given():
    dubai = timezone(timedelta(hours=4))
    aware = datetime(2026, 5, 1, 9, 30, tzinfo=dubai)

    assert DelayedTriggerConfig(fire_at=aware).fire_at == aware
    assert _trigger("t1", next_fire_at=aware).next_fire_at.utcoffset() == timedelta(hours=4)


async def test_a_stored_legacy_naive_row_is_read_as_utc(sp):
    await sp.get_storage(Trigger).create(
        _trigger("t1", next_fire_at=NAIVE.replace(tzinfo=UTC), config=DelayedTriggerConfig(fire_at=NAIVE.replace(tzinfo=UTC))),
    )
    await _store_naive(sp, "t1", NAIVE)

    read = await sp.get_storage(Trigger).get("t1")

    assert read.next_fire_at == NAIVE.replace(tzinfo=UTC)
    assert read.config.fire_at == NAIVE.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


async def test_a_delayed_trigger_made_with_a_naive_fire_at_does_not_stop_the_claim(sp):
    """Probe p8 of the review, the other way round: red on main (``claim_due`` raised ``TypeError``)."""
    from primer.trigger import service

    engine = InMemoryClaimEngine(adapters={ClaimKind.TRIGGER: TriggerClaimAdapter(storage=sp.get_storage(Trigger))})
    await engine.upsert(ClaimKind.SESSION, "some-other-work")       # any other due lease, of a kind with no adapter here
    naive = datetime.now() + timedelta(hours=1)                      # noqa: DTZ005 - a body without an offset
    made = await service.create_trigger(
        slug="once-naive", name="o", description=None, config=DelayedTriggerConfig(fire_at=naive),
        deps=service.ServiceDeps(storage_provider=sp, claim_engine=engine),
    )

    assert [lease.entity_id for lease in await engine.claim_due("w", max_count=5)] == ["some-other-work"]
    assert made.next_fire_at == naive.replace(tzinfo=UTC), "stored as UTC"
    assert engine._leases[(ClaimKind.TRIGGER, made.id)].next_attempt_at.tzinfo is not None, "armed with a zone"


@pytest.mark.parametrize("offset, claimed", [(timedelta(minutes=-5), True), (timedelta(minutes=5), False)])
async def test_a_lease_armed_with_a_naive_next_attempt_at_is_read_as_utc(offset, claimed):
    """A naive ``next_attempt_at`` (a legacy arm) neither raises in ``_due`` nor in the claim order beside aware leases."""
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: now)
    await engine.upsert(ClaimKind.HARNESS, "aware", next_attempt_at=now - timedelta(minutes=1))
    await engine.upsert(ClaimKind.TRIGGER, "naive", next_attempt_at=(now + offset).replace(tzinfo=None))

    got = {lease.entity_id for lease in await engine.claim_due("w", max_count=5)}

    assert got == ({"aware", "naive"} if claimed else {"aware"})


async def test_a_stored_legacy_naive_trigger_is_claimed_at_its_utc_tick_on_the_in_memory_engine(sp):
    """A row an older build saved without a zone, and its lease re-armed with that naive time: not claimed before the tick read as
    UTC, claimed at it, and nothing raises on the way."""
    clock = {"now": NAIVE.replace(tzinfo=UTC) - timedelta(minutes=1)}
    adapter = TriggerClaimAdapter(storage=sp.get_storage(Trigger))
    engine = InMemoryClaimEngine(adapters={ClaimKind.TRIGGER: adapter}, clock=lambda: clock["now"])
    await sp.get_storage(Trigger).create(_trigger("t1", next_fire_at=NAIVE.replace(tzinfo=UTC)))
    await _store_naive(sp, "t1", NAIVE)
    await engine.upsert(ClaimKind.TRIGGER, "t1", next_attempt_at=NAIVE)

    assert await engine.claim_due("w", max_count=5) == [], "claimed before its tick"
    clock["now"] = NAIVE.replace(tzinfo=UTC)
    assert await adapter.is_eligible("t1", clock["now"]) is True
    assert [lease.entity_id for lease in await engine.claim_due("w", max_count=5)] == ["t1"]
