"""S4.4b direct-wake responder intent arm, promotion, and recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.enqueue import _arm_or_reserve, _settle_the_arm, _write_the_wake
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.responder_wake_intent import (
    classify_intent_user_input,
    prepare_responder_intent,
)
from primer.storage import raw_generation
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import _make_lease, fake_event_bus, fake_workspace_io  # noqa: F401


NOW = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
PERSON = PrincipalRef(
    type="user",
    id="person-1",
    display="Person One",
    role="user",
    source="local",
    user_id="person-1",
)
PAYLOAD = {"text": "private direct input", "typed": {"value": 1}}


def _row(**updates) -> WorkspaceSession:
    values = {
        "id": "session-1",
        "workspace_id": "workspace-1",
        "binding": AgentSessionBinding(agent_id="agent-1"),
        "status": SessionStatus.WAITING,
        "turn_status": "idle",
        "created_at": NOW,
        "last_seq": 6,
        "next_unprocessed_seq": 7,
        "responder_epoch": "a" * 32,
        "responder_seq": 2,
        "responder_reservation_version": 4,
        "responder_reservations": {},
    }
    values.update(updates)
    return WorkspaceSession(**values)


def _record(seq: int = 7, payload=PAYLOAD) -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=seq,
        kind=SessionMessageKind.USER_INPUT,
        payload=payload,
        created_at=NOW,
    )


async def _storage(row: WorkspaceSession):
    provider = _FakeStorageProvider()
    storage = provider.get_storage(WorkspaceSession)
    await storage.create(row)
    return provider, storage


class _Engine:
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def upsert(self, _kind, entity_id: str) -> None:
        self.ids.append(entity_id)


class _Scheduler:
    async def enqueue(self, _entity_id: str) -> None:
        return None


def _deps(provider, engine=None):
    return SimpleNamespace(
        claim_engine=engine,
        storage_provider=provider,
        workspace_registry=SimpleNamespace(),
    )


async def test_arm_and_promote_are_one_cas_each_and_retry_does_not_double_bump() -> None:
    original = _row()
    _provider, sessions = await _storage(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    assert "private direct input" not in prepared.intent.source_identity

    armed = await _arm_or_reserve(
        sessions, original, arm=True, prepared_intent=prepared,
    )
    assert armed is not None
    assert (armed.turn_status, armed.last_seq) == ("arming", 7)
    assert armed.pending_responder_intent == prepared.intent
    retried_arm = await _arm_or_reserve(
        sessions, original, arm=True, prepared_intent=prepared,
    )
    assert retried_arm == armed

    landed, outcome = await _write_the_wake(
        sessions,
        armed,
        decided=original,
        instruction="private direct input",
        human_intent=True,
        peer=None,
        external_tools=None,
        armed=armed,
        by=PERSON,
        user_input_record=_record(),
    )
    assert outcome == "promoted"
    assert landed is not None
    assert landed.pending_responder_intent is None
    assert landed.turn_status == "claimable"
    assert landed.responder_epoch == "b" * 32
    assert (landed.responder_seq, landed.responder_reservation_version) == (3, 5)
    assert landed.turn_person == PERSON
    assert landed.turn_person_seq is None

    retried, outcome = await _write_the_wake(
        sessions,
        landed,
        decided=original,
        instruction="private direct input",
        human_intent=True,
        peer=None,
        external_tools=None,
        armed=armed,
        by=PERSON,
        user_input_record=_record(),
    )
    assert outcome == "promoted"
    assert (retried.responder_seq, retried.responder_reservation_version) == (3, 5)


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"binding_epoch": 1}, "binding_epoch"),
        ({"status": SessionStatus.PAUSED.value}, "status"),
        ({"last_seq": 8}, "last_seq"),
        ({"responder_reservation_version": 5}, "responder_reservation_version"),
    ],
)
async def test_arm_refuses_moved_lifecycle_log_or_responder_generation(patch, field) -> None:
    original = _row()
    _provider, sessions = await _storage(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    await sessions.patch_if(original.id, patch, where={field: [raw_generation(original, field)]})

    assert await _arm_or_reserve(
        sessions, original, arm=True, prepared_intent=prepared,
    ) is None
    current = await sessions.get(original.id)
    assert current.pending_responder_intent is None
    assert current.turn_status != "arming"


async def test_append_failure_clears_intent_without_rotating(monkeypatch) -> None:
    original = _row()
    provider, sessions = await _storage(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    armed = await _arm_or_reserve(sessions, original, arm=True, prepared_intent=prepared)

    async def no_log(_io, _row):
        return []

    monkeypatch.setattr("primer.session.dispatch._read_message_lines", no_log)

    await _settle_the_arm(
        sessions,
        _deps(provider),
        original.id,
        armed=armed,
        to_a_pause=False,
        message_written=False,
    )

    after = await sessions.get(original.id)
    assert after.pending_responder_intent is None
    assert after.turn_status == "idle"
    assert (after.responder_epoch, after.responder_reservation_version) == ("a" * 32, 4)


async def test_sweeper_recovers_exact_log_before_claiming(monkeypatch) -> None:
    original = _row()
    provider, sessions = await _storage(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    armed = await _arm_or_reserve(sessions, original, arm=True, prepared_intent=prepared)
    engine = _Engine()

    async def read_lines(_io, _row):
        return [_record().model_dump_json()]

    monkeypatch.setattr("primer.session.dispatch._read_message_lines", read_lines)
    sweeper = SessionMessagingSweeper(
        storage_provider=provider,
        scheduler=_Scheduler(),
        claim_engine=engine,
        workspace_registry=SimpleNamespace(),
    )

    assert await sweeper._heal_an_arm(sessions, armed)
    after = await sessions.get(original.id)
    assert after.turn_status == "claimable"
    assert after.pending_responder_intent is None
    assert after.responder_epoch == "b" * 32
    assert (after.responder_seq, after.responder_reservation_version) == (3, 5)
    assert len(after.responder_reservations) == 1
    assert after.last_responder_intent_receipt is None
    assert engine.ids == [original.id]


async def test_sweeper_recovers_canvas_event_intent_with_generated_digest_metadata(monkeypatch) -> None:
    original = _row()
    provider, sessions = await _storage(original)
    canvas_payload = {
        "text": '<canvas_event id="e1">click</canvas_event>',
        "canvas_event": {"id": "e1", "seq": 1, "element": "play", "kind": "click"},
    }
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        domain="canvas_event",
        user_input_payload=canvas_payload,
        accepted_at=NOW,
        occurrence_id="c" * 32,
        target_epoch="b" * 32,
    )
    armed = await _arm_or_reserve(sessions, original, arm=True, prepared_intent=prepared)
    engine = _Engine()

    async def read_lines(_io, _row):
        return [_record(payload={
            **canvas_payload,
            "canvas_digest": {"rev": 2, "events": []},
        }).model_dump_json()]

    monkeypatch.setattr("primer.session.dispatch._read_message_lines", read_lines)
    sweeper = SessionMessagingSweeper(
        storage_provider=provider,
        scheduler=_Scheduler(),
        claim_engine=engine,
        workspace_registry=SimpleNamespace(),
    )

    assert await sweeper._heal_an_arm(sessions, armed)
    after = await sessions.get(original.id)
    assert after is not None and after.turn_status == "claimable"
    assert (after.responder_seq, after.responder_reservation_version) == (3, 5)
    [reservation] = after.responder_reservations.values()
    assert reservation.domain == "canvas_event"
    assert engine.ids == [original.id]


@pytest.mark.parametrize(
    "move",
    [
        {"binding_epoch": 1},
        {"pause_requested": True, "pause_requested_at": NOW},
    ],
    ids=["binding", "lifecycle"],
)
async def test_stale_prepared_authority_refuses_moved_session(move) -> None:
    original = _row()
    _provider, sessions = await _storage(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        domain="canvas_event",
        user_input_payload={
            "text": '<canvas_event id="e1">click</canvas_event>',
            "canvas_event": {"id": "e1", "seq": 1},
        },
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    await sessions.update(original.model_copy(update=move))

    assert await _arm_or_reserve(
        sessions, original, arm=True, prepared_intent=prepared,
    ) is None
    current = await sessions.get(original.id)
    assert current.pending_responder_intent is None
    assert current.responder_epoch == "a" * 32
    assert (current.responder_seq, current.responder_reservation_version) == (2, 4)


@pytest.mark.parametrize(
    "lines",
    [
        ["{"],
        [json.dumps({"seq": 7})],
        [json.dumps({"seq": 7, "kind": "user_input", "payload": PAYLOAD})],
        [json.dumps({"seq": True, "kind": "user_input", "payload": PAYLOAD, "created_at": NOW.isoformat()})],
        [_record().model_dump_json(), _record().model_dump_json()],
        [json.dumps({"seq": 7, "kind": "user_input", "payload": {"text": "wrong"}, "created_at": NOW.isoformat()})],
    ],
    ids=["malformed-json", "partial-target", "missing-created-at", "bool-seq", "duplicate", "wrong-digest"],
)
async def test_malformed_or_conflicting_log_evidence_is_corrupt(lines) -> None:
    prepared = prepare_responder_intent(
        _row(),
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )

    assert classify_intent_user_input(lines, prepared.intent) == ("corrupt", None)


async def test_exact_and_clean_absent_log_evidence_are_distinct() -> None:
    prepared = prepare_responder_intent(
        _row(),
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    unrelated = _record(seq=6).model_dump_json()

    state, record = classify_intent_user_input([_record().model_dump_json()], prepared.intent)
    assert state == "exact"
    assert record is not None and record["seq"] == 7
    assert classify_intent_user_input([unrelated], prepared.intent) == ("absent", None)


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["{"],
        [json.dumps({"seq": 7})],
        [json.dumps({"seq": True, "kind": "user_input", "payload": PAYLOAD, "created_at": NOW.isoformat()})],
        [_record().model_dump_json(), _record().model_dump_json()],
        [json.dumps({"seq": 7, "kind": "user_input", "payload": {"text": "wrong"}, "created_at": NOW.isoformat()})],
    ],
    ids=["absent", "malformed-json", "partial-target", "bool-seq", "duplicate", "wrong-digest"],
)
async def test_sweeper_clears_absent_log_but_quarantines_mismatch(monkeypatch, lines) -> None:
    original = _row()
    provider, sessions = await _storage(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    armed = await _arm_or_reserve(sessions, original, arm=True, prepared_intent=prepared)

    async def read_lines(_io, _row):
        return lines

    monkeypatch.setattr("primer.session.dispatch._read_message_lines", read_lines)
    sweeper = SessionMessagingSweeper(
        storage_provider=provider,
        scheduler=_Scheduler(),
        claim_engine=_Engine(),
        workspace_registry=SimpleNamespace(),
    )

    healed = await sweeper._heal_an_arm(sessions, armed)
    after = await sessions.get(original.id)
    if lines:
        assert not healed
        assert after.turn_status == "arming"
        assert after.pending_responder_intent == prepared.intent
    else:
        assert healed
        assert after.turn_status == "idle"
        assert after.pending_responder_intent is None
    assert after.responder_epoch == "a" * 32


@pytest.mark.parametrize(
    ("request_patch", "final_status", "ended_reason"),
    [
        ({"pause_requested": True, "pause_requested_at": NOW.isoformat()}, SessionStatus.PAUSED, None),
        ({"cancel_requested": True, "cancel_requested_at": NOW.isoformat()}, SessionStatus.ENDED, "cancelled"),
    ],
    ids=["pause-after-arm", "cancel-after-arm"],
)
async def test_absent_log_lifecycle_request_is_claimed_only_for_pre_executor_convergence(
    monkeypatch,
    fake_storage_provider,
    fake_workspace_io,
    fake_event_bus,
    request_patch,
    final_status,
    ended_reason,
) -> None:
    original = _row(status=SessionStatus.RUNNING)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(original)
    prepared = prepare_responder_intent(
        original,
        starter=PERSON,
        user_input_payload=PAYLOAD,
        accepted_at=NOW,
        occurrence_id="1" * 32,
        target_epoch="b" * 32,
    )
    armed = await _arm_or_reserve(sessions, original, arm=True, prepared_intent=prepared)
    moved = await sessions.patch_if(
        original.id,
        request_patch,
        where={"turn_status": ["arming"]},
    )

    async def no_log(_io, _row):
        return []

    monkeypatch.setattr("primer.session.dispatch._read_message_lines", no_log)
    engine = _Engine()
    sweeper = SessionMessagingSweeper(
        storage_provider=fake_storage_provider,
        scheduler=_Scheduler(),
        claim_engine=engine,
        workspace_registry=SimpleNamespace(),
    )
    assert await sweeper._heal_an_arm(sessions, moved)
    recovered = await sessions.get(original.id)
    assert recovered.pending_responder_intent is None
    assert recovered.turn_status == "claimable"
    assert engine.ids == [original.id]

    executor_called = False

    async def build_executor(_row):
        nonlocal executor_called
        executor_called = True
        raise AssertionError("lifecycle guard must run before executor construction")

    outcome = await run_one_session_turn(
        _make_lease(original.id),
        SessionDispatchDeps(
            storage_provider=fake_storage_provider,
            workspace_io=fake_workspace_io,
            event_bus=fake_event_bus,
            build_executor=build_executor,
        ),
    )

    final = await sessions.get(original.id)
    assert outcome.success and outcome.drop_lease
    assert not executor_called
    assert final.status == final_status
    assert final.ended_reason == ended_reason
    assert final.pending_responder_intent is None


async def test_legacy_no_intent_arm_and_promotion_are_unchanged() -> None:
    original = _row()
    _provider, sessions = await _storage(original)
    armed = await _arm_or_reserve(sessions, original, arm=True)

    landed, outcome = await _write_the_wake(
        sessions,
        armed,
        decided=original,
        instruction="ordinary",
        human_intent=True,
        peer=None,
        external_tools=None,
        armed=armed,
        by=PERSON,
        user_input_record=_record(payload={"text": "ordinary"}),
    )

    assert outcome == "promoted"
    assert landed.turn_status == "claimable"
    assert landed.pending_responder_intent is None
    assert landed.responder_epoch == "a" * 32
    assert "pending_responder_intent" not in landed.model_dump(mode="json")
