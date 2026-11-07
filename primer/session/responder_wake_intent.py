"""Internal S4.4b responder intent construction and atomic patch helpers.

Nothing in this module classifies a public caller as authoritative.  A later
slice may build :class:`PreparedResponderIntent` only after authenticating a
person; this slice supplies the crash-safe arm/log/promote protocol.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from primer.model.common import dump_for_storage
from primer.model.principal import PrincipalRef, person_ref
from primer.model.responder_source import canonical_responder_payload
from primer.model.workspace_session import (
    PendingResponderIntent,
    ResponderReservation,
    SessionMessageKind,
    SessionMessageRecord,
    WorkspaceSession,
)
from primer.session.responder_attribution import ReservationFences, responder_source
from primer.storage import raw_generation


_RESPONDER_SCALARS = (
    "responder_epoch",
    "responder_seq",
    "responder_reservation_version",
    "turn_person_seq",
    "turn_person_at",
    "turn_person_epoch",
    "turn_person_source_key",
)


@dataclass(frozen=True)
class PreparedResponderIntent:
    """One trusted input intent plus the exact row generation it may arm."""

    session_id: str
    intent: PendingResponderIntent
    fences: ReservationFences
    responder_generations: dict[str, Any]


def prepare_responder_intent(
    row: WorkspaceSession,
    *,
    starter: PrincipalRef,
    user_input_payload: Any,
    accepted_at: datetime | None = None,
    occurrence_id: str | None = None,
    target_epoch: str | None = None,
) -> PreparedResponderIntent:
    """Prepare, but do not persist, one authenticated direct USER_INPUT.

    ``starter`` is deliberately explicit: callers must derive it from trusted
    server authentication state.  The payload contributes only its digest;
    raw input is never stored in the intent identity.
    """
    if person_ref(starter) != starter:
        raise ValueError("responder intent starter must name a person")
    if row.responder_epoch is None:
        raise ValueError("responder intent requires an active responder ledger")
    stamped_at = accepted_at or datetime.now(UTC)
    if stamped_at.tzinfo is None or stamped_at.utcoffset() is None:
        raise ValueError("accepted_at must be timezone-aware")
    stamped_at = stamped_at.astimezone(UTC)
    occurrence = occurrence_id or secrets.token_hex(16)
    epoch = target_epoch or secrets.token_hex(16)
    payload_digest = sha256(canonical_responder_payload(user_input_payload)).hexdigest()
    identity = {
        "intent_occurrence_id": occurrence,
        "payload_sha256": payload_digest,
        "session_id": row.id,
        "user_input_seq": row.last_seq + 1,
    }
    source = responder_source("user_input", identity)
    intent = PendingResponderIntent(
        occurrence_id=occurrence,
        target_epoch=epoch,
        starter=starter,
        domain=source.domain,
        source_identity=source.identity,
        source_key=source.key,
        accepted_at=stamped_at,
        user_input_seq=row.last_seq + 1,
        payload_sha256=payload_digest,
    )
    return PreparedResponderIntent(
        session_id=row.id,
        intent=intent,
        fences=ReservationFences.from_session(row),
        responder_generations={field: raw_generation(row, field) for field in _RESPONDER_SCALARS},
    )


def prepared_arm_where(prepared: PreparedResponderIntent) -> dict[Any, list[Any]]:
    """Scalar-only CAS fence for the row on which ``prepared`` was built."""
    where: dict[Any, list[Any]] = prepared.fences.where()
    where.update({field: [value] for field, value in prepared.responder_generations.items()})
    where[("pending_responder_intent", "occurrence_id")] = [None]
    return where


def prepared_arm_is_current(
    row: WorkspaceSession,
    prepared: PreparedResponderIntent,
    *,
    owned: bool,
) -> bool:
    """Whether an ambiguous arm retry observes exactly its already-landed write."""
    intent = row.pending_responder_intent
    if row.id != prepared.session_id or intent != prepared.intent:
        return False
    expected = prepared.fences
    unchanged = (
        raw_generation(row, "status") == expected.status
        and raw_generation(row, "turn_no") == expected.turn_no
        and raw_generation(row, "binding_epoch") == expected.binding_epoch
        and raw_generation(row, "parked_status") == expected.parked_status
        and raw_generation(row, "parked_event_key") == expected.parked_event_key
        and raw_generation(row, "parked_at") == expected.parked_at
        and raw_generation(row, "parked_until") == expected.parked_until
        and raw_generation(row, "pause_requested") == expected.pause_requested
        and raw_generation(row, "pause_requested_at") == expected.pause_requested_at
        and raw_generation(row, "cancel_requested") == expected.cancel_requested
        and raw_generation(row, "cancel_requested_at") == expected.cancel_requested_at
        and raw_generation(row, "interrupt_requested") == expected.interrupt_requested
        and row.last_seq == prepared.intent.user_input_seq
        and all(raw_generation(row, field) == value for field, value in prepared.responder_generations.items())
    )
    if not unchanged:
        return False
    if owned:
        return row.turn_status == "arming" and raw_generation(row, "armed_at") is not None
    return row.turn_status == "arming" and raw_generation(row, "armed_at") is not None


def exact_intent_where(row: WorkspaceSession) -> dict[Any, list[Any]]:
    """Every scalar leaf of the stored intent plus its responder generation."""
    intent = row.pending_responder_intent
    if intent is None:
        raise ValueError("session has no pending responder intent")
    dumped = dump_for_storage(intent)
    where: dict[Any, list[Any]] = {
        field: [raw_generation(row, field)] for field in _RESPONDER_SCALARS
    }

    def add_leaves(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                add_leaves(child, (*path, key))
            return
        if isinstance(value, list):
            raise ValueError("pending responder intent contains an unsupported list leaf")
        where[("pending_responder_intent", *path)] = [value]

    add_leaves(dumped, ())
    return where


def classify_intent_user_input(
    raw_lines: list[str], intent: PendingResponderIntent,
) -> tuple[Literal["exact", "absent", "corrupt"], dict[str, Any] | None]:
    """Find the unique exact USER_INPUT owned by ``intent`` without guessing."""
    at_seq: list[SessionMessageRecord] = []
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            return "corrupt", None
        if not isinstance(value, dict):
            return "corrupt", None
        has_seq = "seq" in value
        has_kind = "kind" in value
        if not has_seq and not has_kind:
            continue  # a plain role/parts Message line, not a session record
        if not has_seq or not has_kind:
            return "corrupt", None
        seq = value.get("seq")
        if type(seq) is not int or seq < 1:  # bool is an int subclass; never let True match seq 1
            return "corrupt", None
        try:
            record = SessionMessageRecord.model_validate(value)
        except ValueError:
            return "corrupt", None
        if record.created_at.tzinfo is None or record.created_at.utcoffset() is None:
            return "corrupt", None
        if record.seq == intent.user_input_seq:
            at_seq.append(record)
    if not at_seq:
        return "absent", None
    if len(at_seq) != 1:
        return "corrupt", None
    record = at_seq[0]
    if record.kind != SessionMessageKind.USER_INPUT:
        return "corrupt", None
    digest = sha256(canonical_responder_payload(record.payload)).hexdigest()
    if digest != intent.payload_sha256:
        return "corrupt", None
    return "exact", record.model_dump(mode="json")


def intent_matches_record(intent: PendingResponderIntent, record: Any) -> bool:
    """Whether a just-written record is the exact durable input for ``intent``."""
    if hasattr(record, "model_dump"):
        record = record.model_dump(mode="json")
    return classify_intent_user_input([json.dumps(record)], intent)[0] == "exact"


def promotion_patch(row: WorkspaceSession) -> dict[str, Any]:
    """Responder portion of one atomic intent-to-baseline promotion."""
    intent = row.pending_responder_intent
    if intent is None:
        raise ValueError("session has no pending responder intent")
    seq = (row.responder_seq or 0) + 1
    version = (row.responder_reservation_version or 0) + 1
    reservation = ResponderReservation(
        domain=intent.domain,
        source_identity=intent.source_identity,
        person=intent.starter,
        seq=seq,
        accepted_at=intent.accepted_at,
    )
    return {
        "responder_epoch": intent.target_epoch,
        "responder_seq": seq,
        "responder_reservation_version": version,
        "responder_reservations": {intent.source_key: dump_for_storage(reservation)},
        "turn_person": intent.starter.model_dump(mode="json"),
        "turn_person_seq": None,
        "turn_person_at": None,
        "turn_person_epoch": None,
        "turn_person_source_key": None,
        "pending_responder_intent": None,
    }


def intent_is_promoted(row: WorkspaceSession, intent: PendingResponderIntent) -> bool:
    """Recognise an exact successful promotion after an ambiguous response."""
    reservation = (row.responder_reservations or {}).get(intent.source_key)
    return (
        row.pending_responder_intent is None
        and row.responder_epoch == intent.target_epoch
        and reservation is not None
        and reservation.domain == intent.domain
        and reservation.source_identity == intent.source_identity
        and reservation.person == intent.starter
        and row.turn_person == intent.starter
        and row.turn_person_seq is None
        and row.turn_person_at is None
        and row.turn_person_epoch is None
        and row.turn_person_source_key is None
    )
