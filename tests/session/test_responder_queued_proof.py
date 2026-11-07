"""S4.4c server proof persistence and queued realization ordering."""

from datetime import UTC, datetime

import pytest

from primer.model.common import dump_for_storage
from primer.model.principal import PrincipalRef
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
