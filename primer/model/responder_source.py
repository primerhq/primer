"""Canonical, bounded responder source identities and source keys.

This is a model-layer codec so persisted responder contracts and session writers
share one definition without making models import runtime session code.
"""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from hashlib import blake2b
from typing import Any


RESPONDER_SOURCE_IDENTITY_MAX_BYTES = 2048
RESPONDER_SOURCE_DOMAIN_MAX_BYTES = 64
RESPONDER_SOURCE_IDENTITY_MAX_ITEMS = 256
RESPONDER_SOURCE_IDENTITY_MAX_DEPTH = 16

_DOMAIN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_FIXED_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")


@dataclass(frozen=True)
class CanonicalResponderSource:
    key: str
    domain: str
    identity: str


@dataclass(frozen=True)
class _NumberToken:
    value: str

    @classmethod
    def parse(cls, value: str) -> "_NumberToken":
        # The producer never emits exponent notation. Checking the short token
        # lexically prevents values such as 1e1000000000 from expanding.
        if not _FIXED_NUMBER.fullmatch(value) or value in {"-0", "-0.0"}:
            raise ValueError("responder source identity number is not canonical")
        return cls(value)


def canonical_responder_source(domain: str, identity: Any) -> CanonicalResponderSource:
    """Canonicalize one typed identity and derive its fixed-width ``rs1_`` key."""
    domain_bytes = _validated_domain(domain)
    canonical = canonical_responder_identity(identity)
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
    return CanonicalResponderSource(
        key="rs1_" + _source_digest(framed),
        domain=domain,
        identity=canonical,
    )


def responder_source_key(domain: str, identity: str) -> str:
    """Derive the source key for an already-canonical identity."""
    domain_bytes = _validated_domain(domain)
    validate_canonical_responder_identity(identity)
    identity_bytes = identity.encode("utf-8")
    framed = (
        b"rs1\x00"
        + len(domain_bytes).to_bytes(2, "big")
        + domain_bytes
        + len(identity_bytes).to_bytes(4, "big")
        + identity_bytes
    )
    return "rs1_" + _source_digest(framed)


def _source_digest(framed: bytes) -> str:
    return blake2b(framed, digest_size=32).hexdigest()


def canonical_responder_identity(identity: Any) -> str:
    """Return the bounded canonical identity spelling used in reservations."""
    if isinstance(identity, bytes):
        encoded = base64.urlsafe_b64encode(identity).rstrip(b"=").decode("ascii")
        return f"bytes:{encoded}"
    _validate_json(identity, depth=0, budget=[0])
    try:
        encoded = _canonical_json(identity)
        encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("responder source identity is not strict JSON") from exc
    canonical = f"json:{encoded}"
    if len(canonical.encode("utf-8")) > RESPONDER_SOURCE_IDENTITY_MAX_BYTES:
        raise ValueError("responder source identity exceeds the byte limit")
    return canonical


def canonical_responder_payload(payload: Any) -> bytes:
    """Return the canonical JSON bytes used for responder payload digests."""
    _validate_json(payload, depth=0, budget=[0])
    try:
        return _canonical_json(payload).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("responder payload is not strict JSON") from exc


def validate_canonical_responder_identity(value: str) -> Any:
    """Validate and decode one producer-created canonical JSON identity.

    The 2048-byte bound is applied before JSON parsing. Numeric tokens are kept
    as strings and checked against the producer's fixed-decimal grammar, so no
    Decimal context, integer conversion, exponent expansion or float rounding
    participates in validation.
    """
    if not isinstance(value, str) or not value.startswith("json:"):
        raise ValueError("responder source identity must be canonical JSON")
    try:
        encoded_bytes = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("responder source identity must be valid UTF-8") from exc
    if len(encoded_bytes) > RESPONDER_SOURCE_IDENTITY_MAX_BYTES:
        raise ValueError("responder source identity exceeds the byte limit")
    encoded = value[5:]

    def _reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON number {constant}")

    def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in pairs:
            if key in out:
                raise ValueError("canonical responder identity has a duplicate key")
            out[key] = item
        return out

    try:
        decoded = json.loads(
            encoded,
            parse_int=_NumberToken.parse,
            parse_float=_NumberToken.parse,
            parse_constant=_reject_constant,
            object_pairs_hook=_object,
        )
        _validate_decoded(decoded, depth=0, budget=[0])
        rendered = _render_decoded(decoded)
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("responder source identity is not canonical JSON") from exc
    if rendered != encoded:
        raise ValueError("responder source identity is not canonical JSON")
    return decoded


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
    if depth > RESPONDER_SOURCE_IDENTITY_MAX_DEPTH:
        raise ValueError("responder source identity is too deeply nested")
    budget[0] += 1
    if budget[0] > RESPONDER_SOURCE_IDENTITY_MAX_ITEMS:
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


def _validate_decoded(value: Any, *, depth: int, budget: list[int]) -> None:
    if depth > RESPONDER_SOURCE_IDENTITY_MAX_DEPTH:
        raise ValueError("canonical responder identity is too deeply nested")
    budget[0] += 1
    if budget[0] > RESPONDER_SOURCE_IDENTITY_MAX_ITEMS:
        raise ValueError("canonical responder identity has too many values")
    if value is None or isinstance(value, (str, bool, _NumberToken)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_decoded(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_decoded(key, depth=depth + 1, budget=budget)
            _validate_decoded(item, depth=depth + 1, budget=budget)
        return
    raise ValueError(
        f"canonical responder identity cannot contain {type(value).__name__}"
    )


def _render_decoded(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, _NumberToken):
        return value.value
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_render_decoded(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_render_decoded(key)}:{_render_decoded(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise TypeError(type(value).__name__)


__all__ = [
    "RESPONDER_SOURCE_DOMAIN_MAX_BYTES",
    "RESPONDER_SOURCE_IDENTITY_MAX_BYTES",
    "RESPONDER_SOURCE_IDENTITY_MAX_DEPTH",
    "RESPONDER_SOURCE_IDENTITY_MAX_ITEMS",
    "CanonicalResponderSource",
    "canonical_responder_identity",
    "canonical_responder_payload",
    "canonical_responder_source",
    "responder_source_key",
    "validate_canonical_responder_identity",
]
