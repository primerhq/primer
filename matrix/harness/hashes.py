"""Canonical-input SHA-256 hash helpers for harness diff detection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


def canonical_json(obj: Any) -> str:
    """JSON with sorted keys and minimal separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_overrides(overrides: dict[str, Any]) -> str:
    return _sha256_hex(canonical_json(overrides).encode("utf-8"))


def hash_schema(schema: dict[str, Any]) -> str:
    return _sha256_hex(canonical_json(schema).encode("utf-8"))


def hash_template_source(source: bytes) -> str:
    return _sha256_hex(source)


def hash_rendered_payload(payload: dict[str, Any]) -> str:
    return _sha256_hex(canonical_json(payload).encode("utf-8"))


def hash_bundle(files: Iterable[tuple[str, bytes]]) -> str:
    """Hash an entire bundle directory.

    ``files`` yields ``(relative_path, content_bytes)`` for every file
    that contributes to the bundle (harness.yaml, overrides.schema.json,
    everything under templates/ and any data files). The hash is order-
    independent: we sort by path, then concatenate ``path\\x00content\\x00``.
    """
    h = hashlib.sha256()
    for path, content in sorted(files, key=lambda x: x[0]):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(content)
        h.update(b"\x00")
    return h.hexdigest()


__all__ = [
    "canonical_json",
    "hash_bundle",
    "hash_overrides",
    "hash_rendered_payload",
    "hash_schema",
    "hash_template_source",
]
