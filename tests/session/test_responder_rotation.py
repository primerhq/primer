"""Responder epoch rotation and reservation projection helpers."""

import asyncio
from datetime import UTC, datetime

import pytest

from primer.model.except_ import ConflictError
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    AgentSessionBinding,
    ResponderReservation,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.responder_attribution import (
    ReservationFences,
    RotationStarter,
    project_reservation,
    responder_source,
    rotate_responder_epoch,
)
from tests.conftest import _InMemoryStorage


NOW = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
EPOCH = "a" * 32
NEXT = "b" * 32
PERSON = PrincipalRef(
    type="user", id="u1", display="User One", role="user",
    source="local", user_id="u1",
)


def _source(n: int):
    return responder_source("ask_user", {"call_id": f"c{n}"})


def _reservation(n: int) -> tuple[str, ResponderReservation]:
    source = _source(n)
    return source.key, ResponderReservation(
        domain=source.domain,
        source_identity=source.identity,
        person=PERSON,
        seq=n,
        accepted_at=NOW,
    )


def _session(**updates) -> WorkspaceSession:
    values = {
        "id": "s1",
        "workspace_id": "w1",
        "binding": AgentSessionBinding(agent_id="a1"),
        "binding_epoch": 2,
        "status": SessionStatus.RUNNING,
        "turn_status": "running",
        "turn_no": 3,
        "last_seq": 7,
        "created_at": NOW,
        "responder_seq": 5,
        "responder_epoch": EPOCH,
        "responder_reservation_version": 9,
        "responder_reservations": {},
    }
    values.update(updates)
    return WorkspaceSession(**values)


async def _store(row=None):
    store = _InMemoryStorage(WorkspaceSession)
    await store.create(row or _session())
    return store


def _starter(*, eligible=True, person=PERSON) -> RotationStarter:
    return RotationStarter(
        eligible=eligible,
        person=person,
        domain="user_input",
        identity={"message_id": "m1"},
        accepted_at=NOW,
    )


async def _rotate(store, **kw):
    row = await store.get("s1")
    assert row is not None
    return await rotate_responder_epoch(
        store,
        "s1",
        current_epoch=EPOCH,
        new_epoch=NEXT,
        fences=ReservationFences.from_session(row),
        **kw,
    )


async def test_empty_rotation_preserves_high_water_and_bumps_version_once() -> None:
    old_key, old = _reservation(5)
    store = await _store(_session(
        responder_reservations={old_key: old},
        turn_person=PERSON,
        turn_person_seq=5,
        turn_person_at=NOW,
        turn_person_epoch=EPOCH,
        turn_person_source_key=old_key,
    ))
    result = await _rotate(store)

    assert result.rotated is True and result.starter is None
    assert result.row.responder_epoch == NEXT
    assert result.row.responder_seq == 5
    assert result.row.responder_reservation_version == 10
    assert result.row.responder_reservations == {}
    assert result.row.turn_person is None
    assert result.row.turn_person_source_key is None


async def test_starter_rotation_allocates_once_and_leaves_baseline_unprojected() -> None:
    store = await _store()
    result = await _rotate(store, starter=_starter())

    assert result.starter is not None and result.starter.seq == 6
    assert result.row.responder_seq == 6
    assert result.row.responder_reservation_version == 10
    assert len(result.row.responder_reservations or {}) == 1
    assert result.row.turn_person == PERSON
    assert result.row.turn_person_seq is None


async def test_same_target_retry_is_idempotent() -> None:
    store = await _store()
    first = await _rotate(store, starter=_starter())
    row = await store.get("s1")
    assert row is not None
    retry = await rotate_responder_epoch(
        store,
        "s1",
        current_epoch=EPOCH,
        new_epoch=NEXT,
        starter=_starter(),
        fences=ReservationFences.from_session(row),
    )
    assert first.rotated is True and retry.rotated is False
    assert retry.row.responder_reservation_version == 10
    assert retry.starter == first.starter


async def test_same_target_starter_retry_tolerates_progressed_epoch() -> None:
    store = await _store()
    original = await store.get("s1")
    assert original is not None
    fences = ReservationFences.from_session(original)
    first = await rotate_responder_epoch(
        store, "s1", current_epoch=EPOCH, new_epoch=NEXT,
        starter=_starter(), fences=fences,
    )
    assert first.starter is not None
    later_key, later = _reservation(7)
    reservations = {
        key: value.model_dump(mode="json")
        for key, value in (first.row.responder_reservations or {}).items()
    }
    reservations[later_key] = later.model_dump(mode="json")
    progressed = await store.patch_if(
        "s1",
        {
            "status": SessionStatus.WAITING.value,
            "last_seq": 8,
            "responder_seq": 7,
            "responder_reservation_version": 11,
            "responder_reservations": reservations,
            "turn_person": later.person.model_dump(mode="json"),
            "turn_person_seq": later.seq,
            "turn_person_at": later.accepted_at.isoformat(),
            "turn_person_epoch": NEXT,
            "turn_person_source_key": later_key,
        },
        where={"responder_epoch": [NEXT]},
    )
    assert progressed is not None

    retry = await rotate_responder_epoch(
        store, "s1", current_epoch=EPOCH, new_epoch=NEXT,
        starter=_starter(), fences=fences,
    )

    assert retry.rotated is False and retry.starter == first.starter
    assert retry.row == progressed
    assert retry.row.status is SessionStatus.WAITING
    assert retry.row.last_seq == 8
    assert retry.row.turn_person_source_key == later_key
    assert len(retry.row.responder_reservations or {}) == 2


@pytest.mark.parametrize("starter", [None, _starter(eligible=False)])
async def test_same_target_empty_retry_tolerates_lifecycle_progress(starter) -> None:
    store = await _store()
    original = await store.get("s1")
    assert original is not None
    fences = ReservationFences.from_session(original)
    first = await rotate_responder_epoch(
        store, "s1", current_epoch=EPOCH, new_epoch=NEXT,
        starter=starter, fences=fences,
    )
    progressed = await store.patch_if(
        "s1",
        {"status": SessionStatus.WAITING.value, "last_seq": 8},
        where={"responder_epoch": [NEXT]},
    )
    assert progressed is not None

    retry = await rotate_responder_epoch(
        store, "s1", current_epoch=EPOCH, new_epoch=NEXT,
        starter=starter, fences=fences,
    )

    assert retry.rotated is False and retry.starter is None
    assert retry.row == progressed
    assert retry.row.status is SessionStatus.WAITING
    assert retry.row.last_seq == 8


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


async def test_concurrent_same_rotation_mutates_once() -> None:
    inner = await _store()
    row = await inner.get("s1")
    assert row is not None
    fences = ReservationFences.from_session(row)
    store = _RacingStorage(inner)

    async def rotate():
        return await rotate_responder_epoch(
            store, "s1", current_epoch=EPOCH, new_epoch=NEXT,
            starter=_starter(), fences=fences,
        )

    results = await asyncio.gather(rotate(), rotate())
    assert sorted(result.rotated for result in results) == [False, True]
    final = await inner.get("s1")
    assert final is not None and final.responder_reservation_version == 10


async def test_concurrent_different_rotation_refuses_loser() -> None:
    inner = await _store()
    row = await inner.get("s1")
    assert row is not None
    fences = ReservationFences.from_session(row)
    store = _RacingStorage(inner)

    async def rotate(epoch):
        return await rotate_responder_epoch(
            store, "s1", current_epoch=EPOCH, new_epoch=epoch, fences=fences,
        )

    results = await asyncio.gather(
        rotate(NEXT), rotate("c" * 32), return_exceptions=True,
    )
    assert sum(isinstance(result, ConflictError) for result in results) == 1


async def test_ineligible_starter_clears_prior_person() -> None:
    store = await _store(_session(turn_person=PERSON))
    result = await _rotate(
        store,
        starter=_starter(eligible=False, person=PrincipalRef.system()),
    )
    assert result.row.turn_person is None
    assert result.row.responder_reservations == {}
    assert result.row.responder_seq == 5


async def test_legacy_row_can_rotate_to_authenticated_starter_baseline() -> None:
    store = await _store(_session(
        responder_seq=None,
        responder_epoch=None,
        responder_reservation_version=None,
        responder_reservations=None,
        turn_person=PERSON,
    ))
    row = await store.get("s1")
    assert row is not None
    result = await rotate_responder_epoch(
        store, "s1", current_epoch=None, new_epoch=NEXT,
        starter=_starter(), fences=ReservationFences.from_session(row),
    )
    assert result.row.responder_seq == 1
    assert result.row.responder_reservation_version == 1
    assert result.row.turn_person == PERSON
    assert result.row.turn_person_seq is None


def _two_reservations():
    key5, r5 = _reservation(5)
    key6, r6 = _reservation(6)
    return key5, r5, key6, r6


async def test_projection_latest_wins_and_older_cannot_regress() -> None:
    key5, r5, key6, r6 = _two_reservations()
    store = await _store(_session(
        responder_seq=6,
        responder_reservation_version=10,
        responder_reservations={key5: r5, key6: r6},
    ))
    row = await store.get("s1")
    assert row is not None
    fences = ReservationFences.from_session(row)

    first = await project_reservation(
        store, "s1", responder_epoch=EPOCH, source_key=key5, fences=fences,
    )
    latest = await project_reservation(
        store, "s1", responder_epoch=EPOCH, source_key=key6, fences=fences,
    )
    older = await project_reservation(
        store, "s1", responder_epoch=EPOCH, source_key=key5, fences=fences,
    )
    assert first.applied and latest.applied and not older.applied
    assert older.row.turn_person_seq == 6
    assert older.row.responder_reservation_version == 10


async def test_projection_retry_is_idempotent_and_same_seq_conflict_fails() -> None:
    key5, r5, key6, r6 = _two_reservations()
    store = await _store(_session(
        responder_seq=6,
        responder_reservation_version=10,
        responder_reservations={key5: r5, key6: r6},
    ))
    row = await store.get("s1")
    assert row is not None
    fences = ReservationFences.from_session(row)
    applied = await project_reservation(
        store, "s1", responder_epoch=EPOCH, source_key=key5, fences=fences,
    )
    retry = await project_reservation(
        store, "s1", responder_epoch=EPOCH, source_key=key5, fences=fences,
    )
    assert applied.applied and not retry.applied

    current = await store.get("s1")
    assert current is not None
    await store.update(current.model_copy(update={"turn_person_source_key": key6}))
    with pytest.raises(ConflictError, match="same sequence"):
        await project_reservation(
            store, "s1", responder_epoch=EPOCH, source_key=key5, fences=fences,
        )


@pytest.mark.parametrize(
    "update",
    [
        {"status": SessionStatus.PAUSED},
        {"binding_epoch": 3},
        {"parked_event_key": "ask_user:s1:new"},
        {"last_seq": 8},
    ],
)
async def test_rotation_and_projection_refuse_moved_occurrence(update) -> None:
    key5, r5 = _reservation(5)
    base = _session(responder_reservations={key5: r5})

    rotate_store = await _store(base)
    fences = ReservationFences.from_session(base)
    await rotate_store.update(base.model_copy(update=update))
    with pytest.raises(ConflictError):
        await rotate_responder_epoch(
            rotate_store, "s1", current_epoch=EPOCH, new_epoch=NEXT,
            fences=fences,
        )

    project_store = await _store(base)
    await project_store.update(base.model_copy(update=update))
    with pytest.raises(ConflictError):
        await project_reservation(
            project_store, "s1", responder_epoch=EPOCH,
            source_key=key5, fences=fences,
        )


async def test_rotation_and_projection_fields_remain_storage_only() -> None:
    store = await _store()
    rotated = await _rotate(store, starter=_starter())
    assert rotated.starter is not None
    projected = await project_reservation(
        store, "s1", responder_epoch=NEXT,
        source_key=next(iter(rotated.row.responder_reservations or {})),
        fences=ReservationFences.from_session(rotated.row),
    )
    assert WorkspaceSession._storage_only_fields.isdisjoint(projected.row.model_dump())
