"""Canonical responder sources and active-epoch reservation CAS.

This module is internal. Callers construct source identities and principals from
trusted server state; no request-supplied principal or timestamp is accepted as
authority merely because it reaches these helpers.
"""

from __future__ import annotations

import base64
import json
import math
import re
import secrets
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from hashlib import blake2b, sha256
from typing import Any

from primer.int.storage import Storage
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.principal import PrincipalRef, person_ref
from primer.model.workspace_session import (
    RESPONDER_RESERVATION_LIMIT,
    RESPONDER_SOURCE_DOMAIN_MAX_BYTES,
    RESPONDER_SOURCE_IDENTITY_MAX_BYTES,
    ResponderReservation,
    WorkspaceSession,
)
from primer.storage import raw_generation
from primer.storage.cas import patch_if_or_current


_DOMAIN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_FENCE_FIELDS = (
    "status",
    "turn_status",
    "turn_no",
    "binding_epoch",
    "parked_status",
    "parked_event_key",
    "parked_at",
    "parked_until",
    "pause_requested",
    "pause_requested_at",
    "cancel_requested",
    "cancel_requested_at",
    # Stop/interrupt uses cancel_requested_at as its timestamp; the model has
    # no separate interrupt_requested_at field.
    "interrupt_requested",
    "last_seq",
)
_JSON_SCALARS = (str, int, float, bool, type(None))
_MAX_IDENTITY_ITEMS = 256
_MAX_IDENTITY_DEPTH = 16


@dataclass(frozen=True)
class ResponderSource:
    key: str
    domain: str
    identity: str


@dataclass(frozen=True)
class ReservationResult:
    source_key: str
    reservation: ResponderReservation
    created: bool


@dataclass(frozen=True)
class RotationStarter:
    """A server-classified possible starter for a newly rotated epoch."""

    eligible: bool
    person: PrincipalRef | None = None
    domain: str | None = None
    identity: Any = None
    accepted_at: datetime | None = None


@dataclass(frozen=True)
class RotationResult:
    epoch: str
    row: WorkspaceSession
    rotated: bool
    starter: ResponderReservation | None


@dataclass(frozen=True)
class ProjectionResult:
    row: WorkspaceSession
    applied: bool


@dataclass(frozen=True)
class NewSessionResponderLedger:
    """Storage-only responder fields baked into a newly created session."""

    responder_epoch: str
    responder_seq: int
    responder_reservation_version: int
    responder_reservations: dict[str, ResponderReservation]
    turn_person: PrincipalRef | None

    def fields(self) -> dict[str, Any]:
        return {
            "responder_epoch": self.responder_epoch,
            "responder_seq": self.responder_seq,
            "responder_reservation_version": self.responder_reservation_version,
            "responder_reservations": dict(self.responder_reservations),
            "turn_person": self.turn_person,
        }


@dataclass(frozen=True)
class EmptyResponderRotation:
    """One empty-epoch patch and its scalar responder generations."""

    epoch: str
    patch: dict[str, Any]
    where: dict[str, list[str | int | float | bool | None]]


@dataclass(frozen=True)
class ReservationFences:
    """Canonical scalar generations of the caller's live session occurrence."""

    status: str
    turn_status: str | None
    turn_no: int
    binding_epoch: int
    parked_status: str | None
    parked_event_key: str | None
    parked_at: str | None
    parked_until: str | None
    pause_requested: bool
    pause_requested_at: str | None
    cancel_requested: bool
    cancel_requested_at: str | None
    interrupt_requested: bool
    last_seq: int

    @classmethod
    def from_session(cls, row: WorkspaceSession) -> "ReservationFences":
        return cls(**{field: raw_generation(row, field) for field in _FENCE_FIELDS})

    def where(self) -> dict[str, list[str | int | float | bool | None]]:
        values = {field: getattr(self, field) for field in _FENCE_FIELDS}
        for field, value in values.items():
            if not isinstance(value, _JSON_SCALARS):
                raise ValueError(f"reservation fence {field!r} must be a JSON scalar")
        return {field: [value] for field, value in values.items()}

    def matches(self, row: WorkspaceSession) -> bool:
        return all(raw_generation(row, field) == getattr(self, field) for field in _FENCE_FIELDS)


def responder_source(domain: str, identity: Any) -> ResponderSource:
    """Return the canonical typed identity and its fixed-width ``rs1_`` key."""
    domain_bytes = _validated_domain(domain)
    canonical = _canonical_identity(identity)
    identity_bytes = canonical.encode("utf-8")
    if len(identity_bytes) > RESPONDER_SOURCE_IDENTITY_MAX_BYTES:
        raise ValueError("responder source identity exceeds the byte limit")
    framed = (
        b"rs1\x00"
        + len(domain_bytes).to_bytes(2, "big")
        + domain_bytes
        + len(identity_bytes).to_bytes(4, "big")
        + identity_bytes
    )
    return ResponderSource(
        key="rs1_" + _source_digest(framed),
        domain=domain,
        identity=canonical,
    )


def new_session_responder_ledger(
    *,
    session_id: str,
    starter: PrincipalRef | None = None,
    creation_occurrence_id: str | None = None,
    user_input_seq: int | None = None,
    user_input_payload: Any = None,
    accepted_at: datetime | None = None,
    epoch: str | None = None,
) -> NewSessionResponderLedger:
    """Build the complete active responder ledger for a new session row.

    A starter is accepted only through the explicit ``starter`` argument. The
    caller must derive it from trusted server authentication state; this helper
    never infers authority from session text or ``initiated_by``.
    """
    target = epoch or secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-f]{32}", target):
        raise ValueError("responder epoch must be exactly 32 lowercase hex")
    if starter is None:
        return NewSessionResponderLedger(target, 0, 1, {}, None)
    if person_ref(starter) != starter:
        raise ValueError("responder starter must name an attributable person")
    if (
        creation_occurrence_id is None
        or not re.fullmatch(r"[0-9a-f]{32}", creation_occurrence_id)
    ):
        raise ValueError("creation occurrence id must be exactly 32 lowercase hex")
    if user_input_seq is None or user_input_seq < 1:
        raise ValueError("responder starter requires a positive USER_INPUT seq")
    if accepted_at is None:
        raise ValueError("responder starter requires accepted_at")
    canonical_payload = _canonical_json_payload(user_input_payload)
    source = responder_source(
        "user_input",
        {
            "session_id": session_id,
            "creation_occurrence_id": creation_occurrence_id,
            "user_input_seq": user_input_seq,
            "payload_sha256": sha256(canonical_payload).hexdigest(),
        },
    )
    reservation = ResponderReservation(
        domain=source.domain,
        source_identity=source.identity,
        person=starter,
        seq=1,
        accepted_at=accepted_at,
    )
    return NewSessionResponderLedger(
        target,
        1,
        1,
        {source.key: reservation},
        starter,
    )


def empty_responder_rotation(
    row: WorkspaceSession, *, epoch: str | None = None,
) -> EmptyResponderRotation:
    """Build the responder portion of an atomic structural epoch rotation."""
    target = epoch or secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-f]{32}", target):
        raise ValueError("responder epoch must be exactly 32 lowercase hex")
    seq = row.responder_seq if row.responder_seq is not None else 0
    version = (
        row.responder_reservation_version
        if row.responder_reservation_version is not None
        else 0
    )
    fence_fields = (
        "responder_epoch",
        "responder_seq",
        "responder_reservation_version",
        "turn_person_seq",
        "turn_person_at",
        "turn_person_epoch",
        "turn_person_source_key",
    )
    where = {field: [raw_generation(row, field)] for field in fence_fields}
    return EmptyResponderRotation(
        epoch=target,
        patch={
            "responder_epoch": target,
            "responder_seq": seq,
            "responder_reservation_version": version + 1,
            "responder_reservations": {},
            "turn_person": None,
            "turn_person_seq": None,
            "turn_person_at": None,
            "turn_person_epoch": None,
            "turn_person_source_key": None,
        },
        where=where,
    )


def _source_digest(framed: bytes) -> str:
    return blake2b(framed, digest_size=32).hexdigest()


def _validated_domain(domain: str) -> bytes:
    if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain):
        raise ValueError("responder source domain must be a canonical lowercase identifier")
    try:
        encoded = domain.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("responder source domain must be valid UTF-8") from exc
    if len(encoded) > RESPONDER_SOURCE_DOMAIN_MAX_BYTES:
        raise ValueError("responder source domain exceeds the byte limit")
    return encoded


def _canonical_identity(identity: Any) -> str:
    if isinstance(identity, bytes):
        encoded = base64.urlsafe_b64encode(identity).rstrip(b"=").decode("ascii")
        return f"bytes:{encoded}"
    budget = [0]
    _validate_json(identity, depth=0, budget=budget)
    try:
        encoded = _canonical_json(identity)
        encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("responder source identity is not strict JSON") from exc
    return f"json:{encoded}"


def _canonical_json_payload(payload: Any) -> bytes:
    budget = [0]
    _validate_json(payload, depth=0, budget=budget)
    try:
        return _canonical_json(payload).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("responder payload is not strict JSON") from exc


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        number = Decimal.from_float(value)
        return "0" if number.is_zero() else format(number, "f")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_canonical_json(key)}:{_canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise TypeError(type(value).__name__)


def _validate_json(value: Any, *, depth: int, budget: list[int]) -> None:
    if depth > _MAX_IDENTITY_DEPTH:
        raise ValueError("responder source identity is too deeply nested")
    budget[0] += 1
    if budget[0] > _MAX_IDENTITY_ITEMS:
        raise ValueError("responder source identity has too many values")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("responder source identity contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("responder source identity object keys must be strings")
        for key, item in value.items():
            _validate_json(key, depth=depth + 1, budget=budget)
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    raise ValueError(f"responder source identity cannot contain {type(value).__name__}")


async def reserve_source(
    sessions: Storage[WorkspaceSession],
    session_id: str,
    *,
    responder_epoch: str,
    person: PrincipalRef,
    domain: str,
    identity: Any,
    accepted_at: datetime,
    fences: ReservationFences,
    max_attempts: int = 8,
) -> ReservationResult:
    """Reserve one source in an already-active responder epoch."""
    if not isinstance(person, PrincipalRef) or person_ref(person) != person:
        raise ValueError("responder reservation requires an attributable person")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    fence_where = fences.where()
    source = responder_source(domain, identity)
    current = await sessions.get(session_id)

    for _ in range(max_attempts):
        if current is None:
            raise ConflictError(f"session {session_id!r} no longer exists")
        _require_occurrence(current, responder_epoch, fences)
        reservations = current.responder_reservations
        assert current.responder_seq is not None
        assert current.responder_reservation_version is not None
        assert reservations is not None

        existing = reservations.get(source.key)
        if existing is not None:
            if (
                existing.domain != source.domain
                or existing.source_identity != source.identity
                or existing.person != person
            ):
                raise ConflictError("responder source digest collision or authority mismatch")
            return ReservationResult(source.key, existing, False)
        if len(reservations) >= RESPONDER_RESERVATION_LIMIT:
            raise ConflictError("responder reservation map is full for this epoch")

        reservation = ResponderReservation(
            domain=source.domain,
            source_identity=source.identity,
            person=person,
            seq=current.responder_seq + 1,
            accepted_at=accepted_at,
        )
        new_reservations = {
            key: value.model_dump(mode="json")
            for key, value in reservations.items()
        }
        new_reservations[source.key] = reservation.model_dump(mode="json")
        where = {
            "responder_epoch": [current.responder_epoch],
            "responder_seq": [current.responder_seq],
            "responder_reservation_version": [current.responder_reservation_version],
            **fence_where,
        }
        try:
            written, current = await patch_if_or_current(
                sessions,
                session_id,
                {
                    "responder_seq": reservation.seq,
                    "responder_reservation_version": current.responder_reservation_version + 1,
                    "responder_reservations": new_reservations,
                },
                where=where,
            )
        except NotFoundError as exc:
            raise ConflictError(f"session {session_id!r} no longer exists") from exc
        if written is not None:
            return ReservationResult(source.key, reservation, True)

    raise ConflictError("responder reservation changed too often; retry")


async def rotate_responder_epoch(
    sessions: Storage[WorkspaceSession],
    session_id: str,
    *,
    current_epoch: str | None,
    fences: ReservationFences,
    new_epoch: str | None = None,
    starter: RotationStarter | None = None,
    max_attempts: int = 8,
) -> RotationResult:
    """Rotate one stable session occurrence into a fresh responder epoch."""
    target = new_epoch or secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-f]{32}", target):
        raise ValueError("new responder epoch must be exactly 32 lowercase hex")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    fence_where = fences.where()
    source, starter_person = _rotation_starter(starter)
    current = await sessions.get(session_id)

    for _ in range(max_attempts):
        if current is None:
            raise ConflictError(f"session {session_id!r} no longer exists")
        if current.responder_epoch == target:
            existing = _matching_rotation(
                current,
                target,
                source,
                starter_person,
            )
            return RotationResult(target, current, False, existing)
        if not fences.matches(current):
            raise ConflictError("session lifecycle, binding, or park occurrence moved")
        if current.responder_epoch != current_epoch:
            raise ConflictError("responder epoch moved before rotation")

        seq = current.responder_seq if current.responder_seq is not None else 0
        version = (
            current.responder_reservation_version
            if current.responder_reservation_version is not None
            else 0
        )
        reservation: ResponderReservation | None = None
        reservations: dict[str, Any] = {}
        next_seq = seq
        if source is not None and starter_person is not None:
            assert starter is not None and starter.accepted_at is not None
            next_seq += 1
            reservation = ResponderReservation(
                domain=source.domain,
                source_identity=source.identity,
                person=starter_person,
                seq=next_seq,
                accepted_at=starter.accepted_at,
            )
            reservations[source.key] = reservation.model_dump(mode="json")

        where = {
            "responder_epoch": [raw_generation(current, "responder_epoch")],
            "responder_seq": [raw_generation(current, "responder_seq")],
            "responder_reservation_version": [
                raw_generation(current, "responder_reservation_version")
            ],
            **fence_where,
        }
        patch = {
            "responder_epoch": target,
            "responder_seq": next_seq,
            "responder_reservation_version": version + 1,
            "responder_reservations": reservations,
            "turn_person": (
                starter_person.model_dump(mode="json")
                if starter_person is not None
                else None
            ),
            "turn_person_seq": None,
            "turn_person_at": None,
            "turn_person_epoch": None,
            "turn_person_source_key": None,
        }
        try:
            written, current = await patch_if_or_current(
                sessions, session_id, patch, where=where,
            )
        except NotFoundError as exc:
            raise ConflictError(f"session {session_id!r} no longer exists") from exc
        if written is not None:
            return RotationResult(target, written, True, reservation)

    raise ConflictError("responder epoch rotation changed too often; retry")


async def project_reservation(
    sessions: Storage[WorkspaceSession],
    session_id: str,
    *,
    responder_epoch: str,
    source_key: str,
    fences: ReservationFences,
    max_attempts: int = 8,
) -> ProjectionResult:
    """Project an existing current-epoch reservation, without regressing."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    fence_where = fences.where()
    current = await sessions.get(session_id)

    for _ in range(max_attempts):
        if current is None:
            raise ConflictError(f"session {session_id!r} no longer exists")
        _require_occurrence(current, responder_epoch, fences)
        assert current.responder_reservations is not None
        reservation = current.responder_reservations.get(source_key)
        if reservation is None:
            raise ConflictError("responder reservation does not exist in this epoch")

        projected_seq = current.turn_person_seq
        if projected_seq is not None and projected_seq > reservation.seq:
            return ProjectionResult(current, False)
        if projected_seq == reservation.seq:
            if (
                current.turn_person_epoch == responder_epoch
                and current.turn_person_source_key == source_key
                and current.turn_person == reservation.person
                and current.turn_person_at == reservation.accepted_at
            ):
                return ProjectionResult(current, False)
            raise ConflictError("responder projection conflicts at the same sequence")

        where = {
            "responder_epoch": [current.responder_epoch],
            "responder_seq": [current.responder_seq],
            "responder_reservation_version": [current.responder_reservation_version],
            "turn_person_seq": [raw_generation(current, "turn_person_seq")],
            "turn_person_at": [raw_generation(current, "turn_person_at")],
            "turn_person_epoch": [raw_generation(current, "turn_person_epoch")],
            "turn_person_source_key": [
                raw_generation(current, "turn_person_source_key")
            ],
            **fence_where,
        }
        patch = {
            "turn_person": reservation.person.model_dump(mode="json"),
            "turn_person_seq": reservation.seq,
            "turn_person_at": reservation.accepted_at.isoformat(),
            "turn_person_epoch": responder_epoch,
            "turn_person_source_key": source_key,
        }
        try:
            written, current = await patch_if_or_current(
                sessions, session_id, patch, where=where,
            )
        except NotFoundError as exc:
            raise ConflictError(f"session {session_id!r} no longer exists") from exc
        if written is not None:
            return ProjectionResult(written, True)

    raise ConflictError("responder projection changed too often; retry")


def _rotation_starter(
    starter: RotationStarter | None,
) -> tuple[ResponderSource | None, PrincipalRef | None]:
    if starter is None or not starter.eligible:
        return None, None
    if starter.person is None or person_ref(starter.person) != starter.person:
        raise ValueError("eligible responder starter requires an attributable person")
    if starter.domain is None or starter.accepted_at is None:
        raise ValueError("eligible responder starter requires source and accepted_at")
    return responder_source(starter.domain, starter.identity), starter.person


def _matching_rotation(
    row: WorkspaceSession,
    epoch: str,
    source: ResponderSource | None,
    starter_person: PrincipalRef | None,
) -> ResponderReservation | None:
    if row.responder_epoch != epoch or row.responder_reservations is None:
        raise ConflictError("responder rotation target is corrupt")
    if source is None or starter_person is None:
        return None
    existing = row.responder_reservations.get(source.key)
    if (
        existing is None
        or existing.domain != source.domain
        or existing.source_identity != source.identity
        or existing.person != starter_person
    ):
        raise ConflictError("responder rotation target has a different starter")
    return existing


def _require_occurrence(
    row: WorkspaceSession,
    responder_epoch: str,
    fences: ReservationFences,
) -> None:
    if row.responder_epoch != responder_epoch:
        raise ConflictError("responder epoch is stale or inactive")
    if row.responder_seq is None or row.responder_reservation_version is None or row.responder_reservations is None:
        raise ConflictError("responder ledger is not active")
    if not fences.matches(row):
        raise ConflictError("session lifecycle, binding, or park occurrence moved")


__all__ = [
    "EmptyResponderRotation",
    "NewSessionResponderLedger",
    "ReservationFences",
    "ReservationResult",
    "ProjectionResult",
    "ResponderSource",
    "RotationResult",
    "RotationStarter",
    "empty_responder_rotation",
    "new_session_responder_ledger",
    "project_reservation",
    "reserve_source",
    "responder_source",
    "rotate_responder_epoch",
]
