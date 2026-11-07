"""Canonical responder source identities and active-epoch reservation CAS."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

import primer.session.responder_attribution as attribution
from primer.model.except_ import ConflictError
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    RESPONDER_RESERVATION_LIMIT,
    AgentSessionBinding,
    ResponderReservation,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.responder_attribution import (
    ReservationFences,
    reserve_source,
    responder_source,
)
from tests.conftest import _InMemoryStorage


NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
EPOCH = "a" * 32
PERSON = PrincipalRef(
    type="user", id="u1", display="User One", role="user",
    source="local", user_id="u1",
)


def _session(**updates) -> WorkspaceSession:
    values = {
        "id": "s1",
        "workspace_id": "w1",
        "binding": AgentSessionBinding(agent_id="a1"),
        "binding_epoch": 3,
        "status": SessionStatus.RUNNING,
        "turn_status": "running",
        "turn_no": 4,
        "last_seq": 8,
        "created_at": NOW,
        "responder_seq": 0,
        "responder_epoch": EPOCH,
        "responder_reservation_version": 0,
        "responder_reservations": {},
    }
    values.update(updates)
    return WorkspaceSession(**values)


async def _store(row: WorkspaceSession | None = None) -> _InMemoryStorage:
    store = _InMemoryStorage(WorkspaceSession)
    await store.create(row or _session())
    return store


async def _reserve(store, domain="ask_user", identity=None, **kw):
    row = await store.get("s1")
    assert row is not None
    return await reserve_source(
        store,
        "s1",
        responder_epoch=EPOCH,
        person=PERSON,
        domain=domain,
        identity={"call_id": "c1"} if identity is None else identity,
        accepted_at=NOW,
        fences=ReservationFences.from_session(row),
        **kw,
    )


def test_source_codec_has_a_pinned_vector_and_order_neutral_objects() -> None:
    first = responder_source("ask_user", {"payload": "é", "call_id": "c1"})
    reordered = responder_source("ask_user", {"call_id": "c1", "payload": "é"})

    assert first == reordered
    assert first.identity == 'json:{"call_id":"c1","payload":"é"}'
    assert first.key == "rs1_e68457db5610d72e1083431dde79f88e13e12c8184b8015b3c4196cce52af745"
    assert len(first.key) == 68


def test_source_codec_separates_types_and_lengths() -> None:
    integer = responder_source("d", 1)
    assert integer == responder_source("d", 1.0)
    assert responder_source("d", 0) == responder_source("d", -0.0)
    assert len({
        integer.key,
        responder_source("d", "1").key,
        responder_source("d", True).key,
        responder_source("d", b"1").key,
        responder_source("d1", "").key,
    }) == 5


@pytest.mark.parametrize(
    "identity",
    [float("nan"), float("inf"), {"x": {1, 2}}, {1: "not-string"}, ("tuple",)],
)
def test_source_codec_rejects_non_json_and_nonfinite(identity) -> None:
    with pytest.raises(ValueError):
        responder_source("ask_user", identity)


def test_source_codec_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError, match="byte limit"):
        responder_source("ask_user", "x" * 3000)
    with pytest.raises(ValueError, match="too many"):
        responder_source("ask_user", list(range(300)))
    with pytest.raises(ValueError):
        responder_source("Ask User", "c1")


async def test_first_reservation_and_idempotent_retry_bump_once() -> None:
    store = await _store()
    first = await _reserve(store)
    retry = await _reserve(store)
    row = await store.get("s1")

    assert first.created is True and first.reservation.seq == 1
    assert retry.created is False and retry.reservation == first.reservation
    assert row is not None
    assert row.responder_seq == 1
    assert row.responder_reservation_version == 1


async def test_same_source_after_later_winner_keeps_original_allocation() -> None:
    store = await _store()
    first = await _reserve(store, identity={"call_id": "c1"})
    second = await _reserve(store, identity={"call_id": "c2"})
    retry = await _reserve(store, identity={"call_id": "c1"})
    row = await store.get("s1")

    assert (first.reservation.seq, second.reservation.seq, retry.reservation.seq) == (1, 2, 1)
    assert retry.created is False
    assert row is not None and row.responder_reservation_version == 2


class _RacingStorage:
    def __init__(self, inner):
        self.inner = inner
        self.barrier = asyncio.Barrier(2)
        self.calls = 0

    async def get(self, *args, **kwargs):
        return await self.inner.get(*args, **kwargs)

    async def patch_if(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= 2:
            await self.barrier.wait()
        return await self.inner.patch_if(*args, **kwargs)


async def test_two_concurrent_sources_retry_without_losing_an_addition() -> None:
    inner = await _store()
    store = _RacingStorage(inner)
    row = await inner.get("s1")
    assert row is not None
    fences = ReservationFences.from_session(row)

    async def one(call_id: str):
        return await reserve_source(
            store,
            "s1",
            responder_epoch=EPOCH,
            person=PERSON,
            domain="ask_user",
            identity={"call_id": call_id},
            accepted_at=NOW,
            fences=fences,
        )

    results = await asyncio.gather(one("c1"), one("c2"))
    final = await inner.get("s1")
    assert sorted(result.reservation.seq for result in results) == [1, 2]
    assert final is not None
    assert final.responder_seq == 2
    assert final.responder_reservation_version == 2
    assert len(final.responder_reservations or {}) == 2


async def test_forced_digest_collision_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(attribution, "_source_digest", lambda _value: "0" * 64)
    store = await _store()
    await _reserve(store, identity={"call_id": "c1"})

    with pytest.raises(ConflictError, match="collision"):
        await _reserve(store, identity={"call_id": "c2"})


async def test_map_cap_is_a_domain_conflict() -> None:
    reservations = {}
    for seq in range(1, RESPONDER_RESERVATION_LIMIT + 1):
        source = responder_source("ask_user", {"call_id": f"c{seq}"})
        reservations[source.key] = ResponderReservation(
            domain=source.domain,
            source_identity=source.identity,
            person=PERSON,
            seq=seq,
            accepted_at=NOW,
        )
    store = await _store(_session(
        responder_seq=RESPONDER_RESERVATION_LIMIT,
        responder_reservation_version=RESPONDER_RESERVATION_LIMIT,
        responder_reservations=reservations,
    ))
    with pytest.raises(ConflictError, match="map is full"):
        await _reserve(store, identity={"call_id": "overflow"})


@pytest.mark.parametrize(
    "update",
    [
        {"responder_epoch": "b" * 32},
        {"status": SessionStatus.PAUSED},
        {"binding_epoch": 4},
        {"parked_event_key": "ask_user:s1:new"},
        {"pause_requested": True, "pause_requested_at": NOW + timedelta(seconds=1)},
        {"cancel_requested": True, "cancel_requested_at": NOW + timedelta(seconds=1)},
        {"interrupt_requested": True, "cancel_requested_at": NOW + timedelta(seconds=1)},
        {"last_seq": 9},
    ],
)
async def test_moved_epoch_lifecycle_binding_park_request_or_log_refuses_without_mutation(update) -> None:
    store = await _store()
    original = await store.get("s1")
    assert original is not None
    fences = ReservationFences.from_session(original)
    await store.update(original.model_copy(update=update))

    with pytest.raises(ConflictError):
        await reserve_source(
            store,
            "s1",
            responder_epoch=EPOCH,
            person=PERSON,
            domain="ask_user",
            identity={"call_id": "c1"},
            accepted_at=NOW,
            fences=fences,
        )
    final = await store.get("s1")
    assert final is not None
    assert final.responder_seq == 0
    assert final.responder_reservation_version == 0


async def test_where_contract_contains_only_json_scalars() -> None:
    inner = await _store()

    class InspectingStorage:
        async def get(self, *args, **kwargs):
            return await inner.get(*args, **kwargs)

        async def patch_if(self, *args, **kwargs):
            where = kwargs["where"]
            assert all(
                isinstance(value, (str, int, float, bool, type(None)))
                for allowed in where.values()
                for value in allowed
            )
            return await inner.patch_if(*args, **kwargs)

    result = await _reserve(InspectingStorage())
    assert result.created is True


@pytest.mark.parametrize(
    ("person", "accepted_at"),
    [
        (PrincipalRef.system(), NOW),
        (PERSON, NOW.replace(tzinfo=None)),
        (PERSON, NOW.astimezone(timezone(timedelta(hours=4)))),
    ],
)
async def test_reservation_requires_a_person_and_aware_utc(person, accepted_at) -> None:
    store = await _store()
    row = await store.get("s1")
    assert row is not None
    with pytest.raises(ValueError):
        await reserve_source(
            store,
            "s1",
            responder_epoch=EPOCH,
            person=person,
            domain="ask_user",
            identity={"call_id": "c1"},
            accepted_at=accepted_at,
            fences=ReservationFences.from_session(row),
        )
