"""Storage-only contracts for future responder input realization."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from primer.model.common import dump_for_storage, stored_row_context
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    AgentSessionBinding,
    PendingResponderIntent,
    PendingResponderProof,
    PendingSessionMessage,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.responder_attribution import responder_source
from primer.session.responder_wake_intent import promotion_patch
from primer.storage import raw_generation
from tests.conftest import _InMemoryStorage


NOW = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
PERSON = PrincipalRef(
    type="user",
    id="person-1",
    display="Person One",
    role="user",
    source="local",
    user_id="person-1",
)
OCCURRENCE = "1" * 32
CURRENT_EPOCH = "a" * 32
TARGET_EPOCH = "b" * 32
DIGEST = "d" * 64


def _intent(**updates) -> PendingResponderIntent:
    identity = {
        "intent_occurrence_id": OCCURRENCE,
        "payload_sha256": DIGEST,
        "session_id": "session-1",
        "user_input_seq": 7,
    }
    source = responder_source("user_input", identity)
    values = {
        "occurrence_id": OCCURRENCE,
        "target_epoch": TARGET_EPOCH,
        "starter": PERSON,
        "domain": source.domain,
        "source_identity": source.identity,
        "source_key": source.key,
        "accepted_at": NOW,
        "user_input_seq": 7,
        "payload_sha256": DIGEST,
        "receipt_required": False,
    }
    values.update(updates)
    return PendingResponderIntent(**values)


def _session(**updates) -> WorkspaceSession:
    values = {
        "id": "session-1",
        "workspace_id": "workspace-1",
        "binding": AgentSessionBinding(agent_id="agent-1"),
        "status": SessionStatus.WAITING,
        "created_at": NOW,
        "responder_seq": 0,
        "responder_epoch": CURRENT_EPOCH,
        "responder_reservation_version": 1,
        "responder_reservations": {},
    }
    values.update(updates)
    return WorkspaceSession(**values)


def _proof(**updates) -> PendingResponderProof:
    values = {
        "starter": PERSON,
        "domain": "user_input",
        "accepted_at": NOW,
        "payload_sha256": DIGEST,
    }
    values.update(updates)
    return PendingResponderProof(**values)


def _pending(**updates) -> PendingSessionMessage:
    values = {
        "id": "session-1:pending:1",
        "session_id": "session-1",
        "parts": [{"type": "text", "text": "never stored in proof"}],
        "enqueued_at": NOW,
        "created_at": NOW,
    }
    values.update(updates)
    return PendingSessionMessage(**values)


def test_legacy_rows_decode_with_no_intent_or_proof() -> None:
    session = WorkspaceSession.model_validate(
        dump_for_storage(_session()).copy() | {"pending_responder_intent": None},
        context=stored_row_context(),
    )
    pending = PendingSessionMessage.model_validate(
        {
            "id": "session-1:pending:legacy",
            "session_id": "session-1",
            "parts": [],
            "enqueued_at": NOW,
            "created_at": NOW,
        },
        context=stored_row_context(),
    )

    assert session.pending_responder_intent is None
    assert pending.responder_proof is None


def test_legacy_intent_decodes_with_receipt_policy_defaults() -> None:
    raw = dump_for_storage(
        _session(
            pending_responder_intent=_intent(
                receipt_required=True,
                pending_message_id="pending-legacy",
            ),
        ),
    )
    raw_intent = raw["pending_responder_intent"]
    raw_intent.pop("receipt_required")
    raw_intent.pop("pending_message_id")

    restored = WorkspaceSession.model_validate(
        raw,
        context=stored_row_context(),
    )

    assert restored.pending_responder_intent.receipt_required is True
    assert restored.pending_responder_intent.pending_message_id is None
    promoted = promotion_patch(restored)
    assert "last_responder_intent_receipt" not in promoted
    assert WorkspaceSession.model_validate({
        **dump_for_storage(restored),
        **promoted,
    }).pending_responder_intent is None

    receipt_raw = dump_for_storage(
        _session(
            last_responder_intent_receipt=_intent(
                receipt_required=True,
                pending_message_id="pending-1",
            ),
        ),
    )
    raw_receipt = receipt_raw["last_responder_intent_receipt"]
    raw_receipt.pop("receipt_required")
    raw_receipt.pop("pending_message_id")
    restored_receipt = WorkspaceSession.model_validate(
        receipt_raw,
        context=stored_row_context(),
    )
    assert restored_receipt.last_responder_intent_receipt.receipt_required is True
    assert restored_receipt.last_responder_intent_receipt.pending_message_id is None


def test_receipt_policy_fields_are_strict_and_receipt_is_queue_owned() -> None:
    with pytest.raises(ValidationError, match="receipt-free"):
        _intent(receipt_required=False, pending_message_id="pending-1")
    with pytest.raises(ValidationError, match="must name its pending message"):
        _intent(receipt_required=True)
    with pytest.raises(ValidationError, match="must require a receipt"):
        _session(last_responder_intent_receipt=_intent(receipt_required=False))

    receipt = _intent(receipt_required=True, pending_message_id="pending-1")
    row = _session(last_responder_intent_receipt=receipt)
    assert row.last_responder_intent_receipt == receipt


def test_intent_is_reservation_compatible_and_contains_no_raw_input() -> None:
    intent = _intent()
    row = _session(pending_responder_intent=intent)
    stored = dump_for_storage(row)["pending_responder_intent"]

    assert responder_source(intent.domain, {
        "intent_occurrence_id": OCCURRENCE,
        "payload_sha256": DIGEST,
        "session_id": "session-1",
        "user_input_seq": 7,
    }).key == intent.source_key
    assert "never stored in proof" not in str(stored)
    assert stored["payload_sha256"] == DIGEST


@pytest.mark.parametrize(
    "identity",
    [
        {"fraction": 0.1},
        {"large_integer": 10**100},
    ],
    ids=["producer-float", "large-integer"],
)
def test_intent_accepts_safe_canonical_identities_from_the_shared_producer(
    identity,
) -> None:
    source = responder_source("user_input", identity)

    intent = PendingResponderIntent(
        occurrence_id=OCCURRENCE,
        target_epoch=TARGET_EPOCH,
        starter=PERSON,
        domain=source.domain,
        source_identity=source.identity,
        source_key=source.key,
        accepted_at=NOW,
        user_input_seq=7,
        payload_sha256=DIGEST,
        receipt_required=False,
    )

    assert intent.source_identity == source.identity
    assert intent.source_key == source.key


def test_intent_rejects_huge_exponent_identity_without_numeric_expansion() -> None:
    with pytest.raises(ValidationError, match="canonical JSON"):
        PendingResponderIntent(
            occurrence_id=OCCURRENCE,
            target_epoch=TARGET_EPOCH,
            starter=PERSON,
            domain="user_input",
            source_identity="json:{\"n\":1e1000000000}",
            source_key="rs1_" + "0" * 64,
            accepted_at=NOW,
            user_input_seq=7,
            payload_sha256=DIGEST,
            receipt_required=False,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("occurrence_id", "1" * 31),
        ("target_epoch", "B" * 32),
        ("payload_sha256", "d" * 63),
        ("payload_sha256", "D" * 64),
        ("user_input_seq", 0),
        ("accepted_at", NOW.replace(tzinfo=None)),
        ("accepted_at", NOW.astimezone(timezone(timedelta(hours=4)))),
        ("starter", PrincipalRef.system()),
        ("source_identity", 'json:{"user_input_seq":7, "session_id":"session-1"}'),
        ("source_identity", "json:NaN"),
        ("source_key", "rs1_" + "0" * 64),
    ],
)
def test_intent_rejects_untrusted_or_noncanonical_fields(field, value) -> None:
    with pytest.raises(ValidationError):
        _intent(**{field: value})


@pytest.mark.parametrize(
    "updates",
    [
        {"user_input_seq": 8},
        {"payload_sha256": "e" * 64},
        {"occurrence_id": "2" * 32},
    ],
)
def test_session_rejects_intent_identity_mirror_mismatch(updates) -> None:
    with pytest.raises(ValidationError, match="identity must match"):
        _session(pending_responder_intent=_intent(**updates))


def test_intent_requires_active_ledger_and_a_fresh_target_epoch() -> None:
    with pytest.raises(ValidationError, match="active ledger"):
        WorkspaceSession(
            id="session-1",
            workspace_id="workspace-1",
            binding=AgentSessionBinding(agent_id="agent-1"),
            status=SessionStatus.WAITING,
            created_at=NOW,
            pending_responder_intent=_intent(),
        )
    with pytest.raises(ValidationError, match="fresh epoch"):
        _session(
            pending_responder_intent=_intent(target_epoch=CURRENT_EPOCH),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload_sha256", "f" * 63),
        ("domain", "User Input"),
        ("accepted_at", NOW.replace(tzinfo=None)),
        ("starter", PrincipalRef.system()),
    ],
)
def test_queue_proof_rejects_invalid_server_evidence(field, value) -> None:
    with pytest.raises(ValidationError):
        _proof(**{field: value})


def test_queue_proof_requires_utc_enqueued_occurrence() -> None:
    with pytest.raises(ValidationError, match="enqueued_at"):
        _pending(
            responder_proof=_proof(),
            enqueued_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValidationError, match="after enqueue"):
        _pending(
            responder_proof=_proof(accepted_at=NOW + timedelta(seconds=1)),
        )


async def test_storage_round_trip_and_nested_scalar_patch_are_canonical() -> None:
    sessions = _InMemoryStorage(WorkspaceSession)
    row = _session(pending_responder_intent=_intent())
    await sessions.create(row)
    later = NOW + timedelta(minutes=1)

    updated = await sessions.patch_if(
        row.id,
        {},
        where={"responder_epoch": [raw_generation(row, "responder_epoch")]},
        set_paths={
            ("pending_responder_intent", "accepted_at"): later.isoformat(),
        },
    )

    assert updated is not None
    assert updated.pending_responder_intent.accepted_at == later
    raw = raw_generation(updated, "pending_responder_intent")
    assert raw["accepted_at"] == "2026-11-07T10:01:00Z"
    assert WorkspaceSession.model_validate(
        dump_for_storage(updated), context=stored_row_context(),
    ) == updated

    pending = _pending(responder_proof=_proof())
    assert PendingSessionMessage.model_validate(
        dump_for_storage(pending), context=stored_row_context(),
    ) == pending
    assert raw_generation(pending, "responder_proof")["starter"]["id"] == "person-1"
    assert "never stored in proof" not in str(raw_generation(pending, "responder_proof"))


def test_public_dumps_responses_and_openapi_hide_both_contracts() -> None:
    row = _session(
        pending_responder_intent=_intent(),
        last_responder_intent_receipt=_intent(
            receipt_required=True,
            pending_message_id="pending-1",
        ),
    )
    pending = _pending(responder_proof=_proof())
    assert "pending_responder_intent" not in row.model_dump(mode="json")
    assert "last_responder_intent_receipt" not in row.model_dump(mode="json")
    assert "responder_proof" not in pending.model_dump(mode="json")

    app = FastAPI()

    @app.get("/session", response_model=WorkspaceSession)
    def get_session() -> WorkspaceSession:
        return row

    @app.get("/pending", response_model=PendingSessionMessage)
    def get_pending() -> PendingSessionMessage:
        return pending

    assert "pending_responder_intent" not in TestClient(app).get("/session").json()
    assert "last_responder_intent_receipt" not in TestClient(app).get("/session").json()
    assert "responder_proof" not in TestClient(app).get("/pending").json()
    schemas = app.openapi()["components"]["schemas"]
    assert "pending_responder_intent" not in schemas["WorkspaceSession"]["properties"]
    assert "last_responder_intent_receipt" not in schemas["WorkspaceSession"]["properties"]
    assert "responder_proof" not in schemas["PendingSessionMessage"]["properties"]
    assert "PendingResponderIntent" not in schemas
    assert "PendingResponderProof" not in schemas


def test_rows_without_internal_fields_keep_their_public_and_storage_shapes() -> None:
    row = _pending()
    assert row.model_dump(mode="json") == {
        "id": "session-1:pending:1",
        "session_id": "session-1",
        "parts": [{"type": "text", "text": "never stored in proof"}],
        "attribution": None,
        "client_msg_id": None,
        "enqueued_at": "2026-11-07T10:00:00Z",
        "created_at": "2026-11-07T10:00:00Z",
    }
    assert dump_for_storage(row)["responder_proof"] is None
