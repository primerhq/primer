"""Storage-only responder ledger contract on WorkspaceSession."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from primer.model.common import Identifiable, dump_for_storage
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    RESPONDER_RESERVATION_LIMIT,
    AgentSessionBinding,
    ResponderReservation,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage import raw_generation
from tests.conftest import _InMemoryStorage


NOW = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
EPOCH = "a" * 32
PERSON = PrincipalRef(
    type="user",
    id="person-1",
    display="Person One",
    role="user",
    source="local",
    user_id="person-1",
)


def _key(n: int) -> str:
    return f"rs1_{n:064x}"


def _reservation(seq: int, *, person: PrincipalRef = PERSON) -> ResponderReservation:
    return ResponderReservation(
        domain="ask_user",
        source_identity=f'{{"id":"call-{seq}","v":1}}',
        person=person,
        seq=seq,
        accepted_at=NOW + timedelta(seconds=seq),
    )


def _session(**updates) -> WorkspaceSession:
    values = {
        "id": "session-1",
        "workspace_id": "workspace-1",
        "binding": AgentSessionBinding(agent_id="agent-1"),
        "status": SessionStatus.WAITING,
        "created_at": NOW,
    }
    values.update(updates)
    return WorkspaceSession(**values)


def _active(**updates) -> WorkspaceSession:
    last = _reservation(9)
    values = {
        "turn_person": PERSON,
        "responder_seq": 9,
        "responder_epoch": EPOCH,
        "responder_reservation_version": 12,
        "responder_reservations": {_key(9): last},
        "turn_person_seq": 9,
        "turn_person_at": last.accepted_at,
        "turn_person_epoch": EPOCH,
        "turn_person_source_key": _key(9),
    }
    values.update(updates)
    return _session(**values)


def test_legacy_rows_default_every_ledger_field_even_with_turn_person() -> None:
    row = _session(turn_person=PERSON)

    assert row.responder_seq is None
    assert row.responder_epoch is None
    assert row.responder_reservation_version is None
    assert row.responder_reservations is None
    assert row.turn_person_seq is None
    assert row.turn_person_at is None
    assert row.turn_person_epoch is None
    assert row.turn_person_source_key is None


@pytest.mark.parametrize(
    "updates",
    [
        {"responder_seq": 1},
        {"responder_epoch": EPOCH},
        {"responder_reservation_version": 0},
        {"responder_reservations": {}},
        {"turn_person_seq": 1},
        {"turn_person_at": NOW},
        {"turn_person_epoch": EPOCH},
        {"turn_person_source_key": _key(1)},
    ],
)
def test_partial_ledger_state_is_refused(updates) -> None:
    with pytest.raises(ValidationError):
        _session(**updates)


def test_current_epoch_reservations_are_a_contiguous_lifetime_suffix() -> None:
    older = _reservation(8)
    latest = _reservation(9)
    row = _active(
        responder_reservations={_key(8): older, _key(9): latest},
    )

    assert row.responder_seq == 9
    with pytest.raises(ValidationError, match="contiguous suffix"):
        _active(responder_reservations={_key(7): _reservation(7), _key(9): latest})


def test_empty_current_epoch_map_preserves_lifetime_high_water() -> None:
    row = _session(
        responder_seq=9,
        responder_epoch="b" * 32,
        responder_reservation_version=13,
        responder_reservations={},
    )
    assert row.responder_seq == 9


@pytest.mark.parametrize(
    "accepted_at",
    [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=4)))],
)
def test_reservation_times_must_be_aware_utc(accepted_at: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        ResponderReservation(
            domain="ask_user",
            source_identity="bounded",
            person=PERSON,
            seq=1,
            accepted_at=accepted_at,
        )


def test_reservation_map_and_source_identity_are_bounded() -> None:
    reservations = {
        _key(i): _reservation(i + 1) for i in range(RESPONDER_RESERVATION_LIMIT + 1)
    }
    with pytest.raises(ValidationError):
        _session(
            responder_seq=RESPONDER_RESERVATION_LIMIT + 1,
            responder_epoch=EPOCH,
            responder_reservation_version=1,
            responder_reservations=reservations,
        )
    with pytest.raises(ValidationError, match="byte limit"):
        ResponderReservation(
            domain="ask_user",
            source_identity="x" * 2049,
            person=PERSON,
            seq=1,
            accepted_at=NOW,
        )


@pytest.mark.parametrize("epoch", ["epoch-2", "A" * 32, "a" * 31, "a" * 33])
def test_responder_epoch_is_exact_lowercase_hex(epoch: str) -> None:
    with pytest.raises(ValidationError):
        _session(
            responder_seq=0,
            responder_epoch=epoch,
            responder_reservation_version=0,
            responder_reservations={},
        )


@pytest.mark.parametrize("domain", ["AskUser", "ask user", "_ask_user", "a" * 65])
def test_reservation_domain_is_bounded_and_canonical(domain: str) -> None:
    with pytest.raises(ValidationError):
        ResponderReservation(
            domain=domain,
            source_identity="exact-id",
            person=PERSON,
            seq=1,
            accepted_at=NOW,
        )


def test_projection_must_match_person_and_current_reservation() -> None:
    with pytest.raises(ValidationError, match="all present"):
        _active(turn_person_epoch=None)
    with pytest.raises(ValidationError, match="must match"):
        _active(turn_person_seq=8)
    with pytest.raises(ValidationError, match="must name"):
        _active(turn_person_source_key=_key(8))
    with pytest.raises(ValidationError, match="must match responder_epoch"):
        _active(turn_person_epoch="b" * 32)


def test_authenticated_starter_baseline_needs_no_projection_yet() -> None:
    row = _active(
        turn_person_seq=None,
        turn_person_at=None,
        turn_person_epoch=None,
        turn_person_source_key=None,
    )
    assert row.turn_person == PERSON
    assert row.turn_person_seq is None


def test_storage_dump_round_trips_all_internal_fields_and_raw_generation() -> None:
    row = _active()
    stored = dump_for_storage(row)

    assert stored["responder_seq"] == 9
    assert stored["responder_epoch"] == EPOCH
    assert stored["responder_reservations"][_key(9)]["domain"] == "ask_user"
    assert stored["responder_reservations"][_key(9)]["person"]["id"] == "person-1"
    assert stored["turn_person_at"] == "2026-11-07T08:00:09Z"
    assert stored["turn_person_epoch"] == EPOCH
    assert raw_generation(row, "responder_epoch") == EPOCH
    assert raw_generation(row, "turn_person_seq") == 9
    assert WorkspaceSession.model_validate(stored) == row


async def test_generic_storage_patch_keeps_and_canonicalizes_internal_fields() -> None:
    store = _InMemoryStorage(WorkspaceSession)
    row = _active()
    await store.create(row)

    updated = await store.patch_if(
        row.id,
        {"responder_reservation_version": 13},
        where={"responder_epoch": [raw_generation(row, "responder_epoch")]},
    )

    assert updated is not None
    assert updated.responder_reservation_version == 13
    assert updated.responder_reservations == row.responder_reservations


def test_public_dumps_api_response_and_openapi_do_not_leak_ledger() -> None:
    row = _active()
    internal = WorkspaceSession._storage_only_fields
    assert internal.isdisjoint(row.model_dump())

    app = FastAPI()

    @app.get("/session", response_model=WorkspaceSession)
    def get_session() -> WorkspaceSession:
        return row

    response = TestClient(app).get("/session")
    assert response.status_code == 200
    assert internal.isdisjoint(response.json())
    schemas = app.openapi()["components"]["schemas"]
    assert "WorkspaceSession" in schemas
    assert "ResponderReservation" not in schemas
    assert internal.isdisjoint(schemas["WorkspaceSession"]["properties"])


def test_other_models_keep_existing_storage_secret_behavior() -> None:
    class OtherRow(Identifiable):
        token: SecretStr

    row = OtherRow(id="other-1", token=SecretStr("plain-secret"))
    assert row.model_dump(mode="json")["token"] == "**********"
    assert dump_for_storage(row) == {"id": "other-1", "token": "plain-secret"}
