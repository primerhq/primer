"""S4.4c server proof persistence and queued realization ordering."""

from datetime import UTC, datetime

import pytest

from primer.model.common import dump_for_storage, stored_row_context
from primer.model.principal import PrincipalRef
from primer.model.responder_source import responder_source_key
from primer.model.workspace_session import (
    AgentSessionBinding,
    MACHINE_ATTRIBUTION_KEY,
    PEER_PAYLOAD_KEY,
    PendingSessionMessage,
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.enqueue import SessionWakeDeps, _arm_or_reserve, _write_the_wake
from primer.session.pending_messages import (
    _prepared_intent_for_pending,
    realize_next_pending,
    store_pending_steer,
)
from primer.session.responder_wake_intent import (
    prepare_responder_intent,
    promotion_patch,
)
from tests.session.test_pending_messages import _FakeRegistry, _FakeWorkspace, _SP, _wake_deps
from tests.session.test_enqueue import (
    _decode_records,
    _FakeEngine,
    _FakeRegistry as _WakeRegistry,
    _FakeScheduler,
    _FakeSlot,
    _FakeWorkspace as _WakeWorkspace,
)


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
PERSON = PrincipalRef(
    type="user", id="u1", display="User", role="user", source="local", user_id="u1",
)


def _session() -> WorkspaceSession:
    return WorkspaceSession(
        id="s",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        turn_status="idle",
        created_at=NOW,
        responder_epoch="a" * 32,
        responder_seq=0,
        responder_reservation_version=1,
        responder_reservations={},
    )


def _raw_legacy_document(row: WorkspaceSession, field: str) -> dict:
    raw = dump_for_storage(row)
    raw.pop("id")
    evidence = raw[field]
    evidence.pop("receipt_required")
    evidence.pop("pending_message_id")
    return raw


def _seed_raw_legacy_evidence(sp: _SP, row: WorkspaceSession, field: str) -> WorkspaceSession:
    """Install literal pre-S4.5A JSON while retaining reference patch semantics."""
    raw = _raw_legacy_document(row, field)
    decoded = WorkspaceSession.model_validate(
        {**raw, "id": row.id},
        context=stored_row_context(),
    )
    sp.sessions._data[row.id] = decoded
    sp.sessions._raw[row.id] = (decoded, raw)
    return decoded


async def _queued(sp: _SP, *, trusted: bool = True) -> PendingSessionMessage:
    session = await sp.sessions.get("s")
    return await store_pending_steer(
        storage_provider=sp,
        session=session,
        text="queued text",
        workspace_registry=None,
        by=PERSON,
        trusted_responder_starter=PERSON if trusted else None,
    )


async def test_authenticated_queue_stores_only_proof_and_legacy_by_is_ineligible() -> None:
    sp = _SP()
    await sp.sessions.create(_session())

    proved = await _queued(sp)
    legacy = await _queued(sp, trusted=False)

    assert proved.responder_proof is not None
    assert proved.responder_proof.starter == PERSON
    assert proved.responder_proof.accepted_at == proved.enqueued_at
    assert "queued text" not in proved.responder_proof.payload_sha256
    assert legacy.responder_proof is None
    session = await sp.sessions.get("s")
    assert (session.responder_seq, session.responder_reservation_version) == (0, 1)
    assert session.responder_reservations == {}
    assert "responder_proof" not in proved.model_dump(mode="json")


async def test_proof_requires_the_exact_authenticated_person_sender() -> None:
    sp = _SP()
    session = _session()
    await sp.sessions.create(session)
    other = PERSON.model_copy(update={"id": "u2", "user_id": "u2"})

    for by, starter, attribution in (
        (None, PERSON, None),
        (PrincipalRef.unattributed(), PERSON, None),
        (PERSON, other, None),
        (PERSON, PERSON, {MACHINE_ATTRIBUTION_KEY: True}),
        (PERSON, PERSON, {PEER_PAYLOAD_KEY: {"session_id": "peer"}}),
    ):
        with pytest.raises(ValueError, match="authenticated person sender"):
            await store_pending_steer(
                storage_provider=sp,
                session=session,
                text="queued text",
                workspace_registry=None,
                by=by,
                attribution=attribution,
                trusted_responder_starter=starter,
            )
    assert sp.pending.rows == {}


async def test_legacy_ledger_omits_proof_and_still_delivers(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    legacy = _session().model_copy(update={
        "responder_epoch": None,
        "responder_seq": None,
        "responder_reservation_version": None,
        "responder_reservations": None,
    })
    await sp.sessions.create(legacy)
    pending = await _queued(sp)
    assert pending.responder_proof is None
    seen = []

    async def wake(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr("primer.session.pending_messages.wake_session", wake)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert seen[0]["prepared_intent"] is None
    assert await sp.pending.get(pending.id) is None


async def test_realize_promotes_before_deleting_and_exact_retry_is_landed(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    seen_pending_during_close = False

    async def close(**kwargs):
        nonlocal seen_pending_during_close
        seen_pending_during_close = await sp.pending.get(pending.id) is not None
        prepared = kwargs["prepared_intent"]
        armed = await _arm_or_reserve(
            sp.sessions, original, arm=True, prepared_intent=prepared,
        )
        record = SessionMessageRecord(
            seq=prepared.intent.user_input_seq,
            kind=SessionMessageKind.USER_INPUT,
            payload={"text": "queued text"},
            created_at=NOW,
        )
        landed, _ = await _write_the_wake(
            sp.sessions,
            armed,
            decided=original,
            instruction="queued text",
            human_intent=False,
            peer=None,
            external_tools=None,
            armed=armed,
            by=PERSON,
            user_input_record=record,
        )
        return landed

    monkeypatch.setattr("primer.session.pending_messages.wake_session", close)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert seen_pending_during_close
    assert await sp.pending.get(pending.id) is None
    promoted = await sp.sessions.get("s")
    assert (promoted.responder_seq, promoted.responder_reservation_version) == (1, 2)
    assert promoted.last_responder_intent_receipt is not None
    assert promoted.last_responder_intent_receipt.receipt_required is True
    assert promoted.last_responder_intent_receipt.pending_message_id == pending.id
    assert "last_responder_intent_receipt" not in promoted.model_dump(mode="json")

    # Reinsert the same durable occurrence to model a crash after close but
    # before delete. The proof recognizes the landed reservation; no wake runs.
    await sp.pending.create(pending)

    async def must_not_wake(**_kwargs):
        raise AssertionError("landed proof must not append or arm twice")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    after = await sp.sessions.get("s")
    assert (after.responder_seq, after.responder_reservation_version) == (1, 2)


async def test_future_receipt_free_close_preserves_queue_cleanup_receipt() -> None:
    original = _session()
    pending_sp = _SP()
    await pending_sp.sessions.create(original)
    pending = await _queued(pending_sp)
    _, queued = _prepared_intent_for_pending(original, pending, "queued text")
    queued_row = original.model_copy(update={"pending_responder_intent": queued.intent})
    queued_closed = WorkspaceSession.model_validate({
        **dump_for_storage(queued_row),
        **promotion_patch(queued_row),
    })
    receipt = queued_closed.last_responder_intent_receipt

    direct = prepare_responder_intent(
        queued_closed,
        starter=PERSON,
        user_input_payload={"text": "future direct"},
        accepted_at=NOW,
        occurrence_id="d" * 32,
        target_epoch="e" * 32,
        receipt_required=False,
    )
    direct_row = queued_closed.model_copy(
        update={"pending_responder_intent": direct.intent},
    )
    direct_patch = promotion_patch(direct_row)
    assert "last_responder_intent_receipt" not in direct_patch
    direct_closed = WorkspaceSession.model_validate({
        **dump_for_storage(direct_row),
        **direct_patch,
    })

    assert direct_closed.last_responder_intent_receipt == receipt
    assert _prepared_intent_for_pending(
        direct_closed, pending, "queued text",
    ) == ("landed", None)


async def test_queue_receipt_must_name_the_exact_pending_row() -> None:
    sp = _SP()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    row = original.model_copy(update={"pending_responder_intent": prepared.intent})
    closed = WorkspaceSession.model_validate({
        **dump_for_storage(row),
        **promotion_patch(row),
    })
    bad_receipt = closed.last_responder_intent_receipt.model_copy(
        update={"pending_message_id": "another-pending-row"},
    )
    corrupt = WorkspaceSession.model_validate({
        **dump_for_storage(closed),
        "last_responder_intent_receipt": dump_for_storage(bad_receipt),
    })

    with pytest.raises(ValueError, match="receipt conflicts"):
        _prepared_intent_for_pending(corrupt, pending, "queued text")


async def test_legacy_armed_before_log_upgrades_then_delivers_once(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=prepared,
    )
    _seed_raw_legacy_evidence(sp, armed, "pending_responder_intent")

    async def must_not_wake(**_kwargs):
        raise AssertionError("legacy live intent must be upgraded before wake")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert not await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    upgraded = await sp.sessions.get("s")
    assert upgraded.pending_responder_intent.pending_message_id == pending.id
    upgraded_raw = sp.sessions._raw_doc("s", upgraded)
    assert upgraded_raw["pending_responder_intent"]["receipt_required"] is True
    assert upgraded_raw["pending_responder_intent"]["pending_message_id"] == pending.id
    # Model the production no-log arm recovery. The queued row remains the
    # sole source of a later wake rather than the old occurrence becoming new.
    recovered = upgraded.model_copy(update={
        "turn_status": "idle",
        "armed_at": None,
        "pending_responder_intent": None,
    })
    await sp.sessions.update(recovered)
    wakes = 0

    async def close(**kwargs):
        nonlocal wakes
        wakes += 1
        current = await sp.sessions.get("s")
        prepared_now = kwargs["prepared_intent"]
        armed_now = await _arm_or_reserve(
            sp.sessions, current, arm=True, prepared_intent=prepared_now,
        )
        record = SessionMessageRecord(
            seq=prepared_now.intent.user_input_seq,
            kind=SessionMessageKind.USER_INPUT,
            payload={"text": "queued text"},
            created_at=NOW,
        )
        landed, _ = await _write_the_wake(
            sp.sessions,
            armed_now,
            decided=current,
            instruction="queued text",
            human_intent=False,
            peer=None,
            external_tools=None,
            armed=armed_now,
            by=PERSON,
            user_input_record=record,
        )
        return landed

    monkeypatch.setattr("primer.session.pending_messages.wake_session", close)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert wakes == 1
    assert await sp.pending.get(pending.id) is None
    landed = await sp.sessions.get("s")
    assert (landed.responder_seq, landed.responder_reservation_version) == (1, 2)


@pytest.mark.parametrize("state", ["armed-after-log", "completed-before-delete"])
async def test_exact_legacy_landed_evidence_upgrades_and_deletes_without_rewake(
    monkeypatch, state: str,
) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=prepared,
    )
    if state == "armed-after-log":
        _seed_raw_legacy_evidence(sp, armed, "pending_responder_intent")
    else:
        closed = WorkspaceSession.model_validate({
            **dump_for_storage(armed),
            **promotion_patch(armed),
        })
        _seed_raw_legacy_evidence(sp, closed, "last_responder_intent_receipt")

    async def must_not_wake(**_kwargs):
        raise AssertionError("durable legacy occurrence must not wake twice")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    if state == "armed-after-log":
        assert not await realize_next_pending(
            storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
        )
        upgraded = await sp.sessions.get("s")
        assert upgraded.pending_responder_intent.pending_message_id == pending.id
        closed = WorkspaceSession.model_validate({
            **dump_for_storage(upgraded),
            **promotion_patch(upgraded),
        })
        await sp.sessions.update(closed)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert await sp.pending.get(pending.id) is None
    landed = await sp.sessions.get("s")
    assert landed.last_responder_intent_receipt.pending_message_id == pending.id
    landed_raw = sp.sessions._raw_doc("s", landed)
    assert landed_raw["last_responder_intent_receipt"]["receipt_required"] is True
    assert landed_raw["last_responder_intent_receipt"]["pending_message_id"] == pending.id
    assert (landed.responder_seq, landed.responder_reservation_version) == (1, 2)


async def test_raw_legacy_live_to_receipt_race_reclassifies_once(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=prepared,
    )
    _seed_raw_legacy_evidence(sp, armed, "pending_responder_intent")
    closed = WorkspaceSession.model_validate({
        **dump_for_storage(armed),
        **promotion_patch(armed),
    })
    receipt_raw = _raw_legacy_document(closed, "last_responder_intent_receipt")
    real_patch_if = sp.sessions.patch_if
    raced = False

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        nonlocal raced
        if not raced:
            raced = True
            decoded = WorkspaceSession.model_validate(
                {**receipt_raw, "id": id}, context=stored_row_context(),
            )
            sp.sessions._data[id] = decoded
            sp.sessions._raw[id] = (decoded, receipt_raw)
            return None
        return await real_patch_if(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )

    monkeypatch.setattr(sp.sessions, "patch_if", patch_if)

    async def must_not_wake(**_kwargs):
        raise AssertionError("legacy live-to-receipt race must not wake twice")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert raced
    assert await sp.pending.get(pending.id) is None
    landed = await sp.sessions.get("s")
    assert landed.last_responder_intent_receipt.pending_message_id == pending.id
    assert (landed.responder_seq, landed.responder_reservation_version) == (1, 2)


async def test_next_fifo_queue_close_replaces_deleted_rows_receipt() -> None:
    sp = _SP()
    original = _session()
    await sp.sessions.create(original)
    first = await _queued(sp)
    second = await _queued(sp)
    _, first_prepared = _prepared_intent_for_pending(original, first, "queued text")
    first_row = original.model_copy(
        update={"pending_responder_intent": first_prepared.intent},
    )
    first_closed = WorkspaceSession.model_validate({
        **dump_for_storage(first_row),
        **promotion_patch(first_row),
    })
    await sp.pending.delete(first.id)

    _, second_prepared = _prepared_intent_for_pending(
        first_closed, second, "queued text",
    )
    second_row = first_closed.model_copy(
        update={"pending_responder_intent": second_prepared.intent},
    )
    second_closed = WorkspaceSession.model_validate({
        **dump_for_storage(second_row),
        **promotion_patch(second_row),
    })

    assert second_closed.last_responder_intent_receipt.pending_message_id == second.id
    assert second_closed.last_responder_intent_receipt != first_closed.last_responder_intent_receipt


@pytest.mark.parametrize("progression", ["classic_projection", "structural_rotation"])
async def test_completed_receipt_survives_progression_before_delete_retry(
    monkeypatch, progression: str,
) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    assert prepared is not None
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=prepared,
    )
    record = SessionMessageRecord(
        seq=prepared.intent.user_input_seq,
        kind=SessionMessageKind.USER_INPUT,
        payload={"text": "queued text"},
        created_at=NOW,
    )
    promoted, _ = await _write_the_wake(
        sp.sessions, armed, decided=original, instruction="queued text",
        human_intent=False, peer=None, external_tools=None, armed=armed,
        by=PERSON, user_input_record=record,
    )
    assert promoted is not None
    if progression == "classic_projection":
        source_key, reservation = next(iter(promoted.responder_reservations.items()))
        progressed = promoted.model_copy(update={
            "turn_person_seq": reservation.seq,
            "turn_person_at": reservation.accepted_at,
            "turn_person_epoch": promoted.responder_epoch,
            "turn_person_source_key": source_key,
        })
    else:
        progressed = promoted.model_copy(update={
            "responder_epoch": "b" * 32,
            "responder_reservation_version": promoted.responder_reservation_version + 1,
            "responder_reservations": {},
            "turn_person": None,
            "turn_person_seq": None,
            "turn_person_at": None,
            "turn_person_epoch": None,
            "turn_person_source_key": None,
        })
    await sp.sessions.update(WorkspaceSession.model_validate(dump_for_storage(progressed)))

    async def must_not_wake(**_kwargs):
        raise AssertionError("completed receipt must prevent duplicate wake")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    after = await sp.sessions.get("s")
    assert (after.responder_seq, after.responder_reservation_version) == (
        progressed.responder_seq, progressed.responder_reservation_version,
    )


async def test_proved_queue_rebases_after_cancelled_reopen_once() -> None:
    sp = _SP()
    session = _session()
    await sp.sessions.create(session)
    pending = await _queued(sp)
    ended = session.model_copy(update={
        "status": SessionStatus.ENDED,
        "ended_reason": "cancelled",
        "ended_at": NOW,
        "metadata": {"invocation": 1},
    })
    await sp.sessions.update(ended)
    slot = _FakeSlot()
    workspace = _WakeWorkspace(slot)
    deps = SessionWakeDeps(
        storage_provider=sp,
        scheduler=_FakeScheduler(),
        claim_engine=_FakeEngine(),
        workspace_registry=_WakeRegistry(workspace),
    )

    assert await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=deps,
    )
    out = await sp.sessions.get("s")
    kinds = [record["kind"] for record in _decode_records(workspace)]
    assert kinds == ["invocation_divider", "user_input"]
    assert slot.reopened and slot.appended == ["queued text"]
    assert out.status == SessionStatus.RUNNING
    assert out.metadata["invocation"] == 2
    assert (out.responder_seq, out.responder_reservation_version) == (1, 3)
    assert out.last_responder_intent_receipt is not None
    assert await sp.pending.get(pending.id) is None


async def test_crash_before_close_preserves_queue_and_live_intent(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)

    async def crash(**kwargs):
        await _arm_or_reserve(
            sp.sessions, original, arm=True, prepared_intent=kwargs["prepared_intent"],
        )
        raise RuntimeError("crash before close")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", crash)
    with pytest.raises(RuntimeError, match="crash before close"):
        await realize_next_pending(
            storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
        )
    assert await sp.pending.get(pending.id) == pending
    armed = await sp.sessions.get("s")
    assert armed.pending_responder_intent is not None
    assert armed.turn_status == "arming"


async def test_corrupt_proof_is_quarantined_without_wake(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    await sp.sessions.create(_session())
    pending = await _queued(sp)
    bad = pending.model_copy(
        update={
            "responder_proof": pending.responder_proof.model_copy(
                update={"payload_sha256": "f" * 64},
            ),
        },
    )
    sp.pending.rows[pending.id] = bad

    async def must_not_wake(**_kwargs):
        raise AssertionError("corrupt proof must not wake")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert not await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert await sp.pending.get(pending.id) == bad
    current = await sp.sessions.get("s")
    assert current.turn_status == "idle"
    assert current.pending_responder_intent is None


@pytest.mark.parametrize("evidence", ["armed", "completed"])
@pytest.mark.parametrize("corruption", ["wrong-domain", "wrong-key"])
async def test_wrong_queue_intent_domain_or_key_is_quarantined(
    monkeypatch, evidence: str, corruption: str,
) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    intent = prepared.intent
    if corruption == "wrong-domain":
        intent = intent.model_copy(update={
            "domain": "other_input",
            "source_key": responder_source_key(
                "other_input", intent.source_identity,
            ),
        })
    else:
        intent = intent.model_copy(update={"source_key": "rs1_" + "0" * 64})
    intent = intent.model_copy(update={"pending_message_id": None})
    if evidence == "armed":
        armed = await _arm_or_reserve(
            sp.sessions, original, arm=True, prepared_intent=prepared,
        )
        corrupt = armed.model_copy(
            update={"pending_responder_intent": intent},
        )
    else:
        row = original.model_copy(
            update={"pending_responder_intent": prepared.intent},
        )
        closed = WorkspaceSession.model_validate({
            **dump_for_storage(row),
            **promotion_patch(row),
        })
        corrupt = closed.model_copy(
            update={"last_responder_intent_receipt": intent},
        )
    await sp.sessions.update(corrupt)

    async def must_not_wake(**_kwargs):
        raise AssertionError("corrupt queue evidence must not wake")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert not await realize_next_pending(
        storage_provider=sp,
        workspace_id="ws-1",
        session_id="s",
        wake_deps=_wake_deps(ws),
    )
    assert await sp.pending.get(pending.id) == pending


async def test_raw_legacy_corrupt_domain_is_quarantined(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=prepared,
    )
    intent = armed.pending_responder_intent
    corrupt_intent = intent.model_copy(update={
        "domain": "other_input",
        "source_key": responder_source_key("other_input", intent.source_identity),
    })
    corrupt = armed.model_copy(
        update={"pending_responder_intent": corrupt_intent},
    )
    _seed_raw_legacy_evidence(sp, corrupt, "pending_responder_intent")

    async def must_not_wake(**_kwargs):
        raise AssertionError("corrupt raw legacy evidence must not wake")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert not await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert await sp.pending.get(pending.id) == pending


async def test_raw_legacy_upgrade_refuses_moved_owner(monkeypatch) -> None:
    sp, ws = _SP(), _FakeWorkspace()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, prepared = _prepared_intent_for_pending(original, pending, "queued text")
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=prepared,
    )
    _seed_raw_legacy_evidence(sp, armed, "pending_responder_intent")
    real_patch_if = sp.sessions.patch_if
    moved = False

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        nonlocal moved
        if not moved:
            moved = True
            current = await sp.sessions.get(id)
            moved_intent = current.pending_responder_intent.model_copy(
                update={"pending_message_id": "different-row"},
            )
            await sp.sessions.update(current.model_copy(
                update={"pending_responder_intent": moved_intent},
            ))
        return await real_patch_if(
            id, patch, where=where, set_paths=set_paths, conn=conn,
        )

    monkeypatch.setattr(sp.sessions, "patch_if", patch_if)

    async def must_not_wake(**_kwargs):
        raise AssertionError("moved raw legacy evidence must not wake")

    monkeypatch.setattr("primer.session.pending_messages.wake_session", must_not_wake)
    assert not await realize_next_pending(
        storage_provider=sp, workspace_id="ws-1", session_id="s", wake_deps=_wake_deps(ws),
    )
    assert moved
    assert await sp.pending.get(pending.id) == pending
    current = await sp.sessions.get("s")
    assert current.pending_responder_intent.pending_message_id == "different-row"


async def test_two_realizers_cannot_share_one_ambiguous_arm() -> None:
    sp = _SP()
    original = _session()
    await sp.sessions.create(original)
    pending = await _queued(sp)
    _, first = _prepared_intent_for_pending(original, pending, "queued text")
    _, second = _prepared_intent_for_pending(original, pending, "queued text")

    assert first.intent == second.intent
    assert first.arm_token != second.arm_token
    armed = await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=first,
    )
    assert armed is not None
    assert await _arm_or_reserve(
        sp.sessions, original, arm=True, prepared_intent=second,
    ) is None
