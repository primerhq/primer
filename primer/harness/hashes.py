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


def operation_input_digests(harness: Any) -> dict[str, str]:
    """The SHA-256 of each input an INSTALL or SYNC of ``harness`` renders from, by name (ticket 01a16a29).

    Stamped on the row when the operation is enqueued (``Harness.operation_inputs``) and recomputed by the worker before it renders, so an
    install or sync applies the inputs it was asked for (and, for a model's call, approved on): the overrides, the harness context the
    templates read (``name``, ``description``, ``slug``), where the bundle comes from (``git_url``, ``ref``, ``subpath``), what was fetched
    (``commit``, ``bundle``) and the resolved dependencies. The git token is not an input: it only authenticates the clone, whose content
    the bundle hash pins. Values are read in their stored form (python mode), so a URL's userinfo counts as typed and is never masked.
    """
    dependencies = [dep.model_dump(mode="python") for dep in (harness.dependencies_resolved or [])]
    inputs: dict[str, Any] = {
        "overrides": harness.overrides,
        "name": harness.name,
        "description": harness.description,
        "slug": harness.slug,
        "git_url": None if harness.git_url is None else str(harness.git_url),
        "ref": harness.ref,
        "subpath": harness.subpath,
        "commit": harness.available_commit,
        "bundle": harness.available_bundle_hash,
        "dependencies": dependencies,
    }
    return {
        name: _sha256_hex(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8"))
        for name, value in inputs.items()
    }


def changed_operation_inputs(harness: Any) -> list[str] | None:
    """The names of the inputs that differ from the ones stamped when the pending operation was enqueued, in a fixed order; ``[]`` when none
    differs, ``None`` when nothing was stamped."""
    stamped = harness.operation_inputs
    if not isinstance(stamped, dict):
        return None
    current = operation_input_digests(harness)
    return [name for name in current if stamped.get(name) != current[name]]


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
    "changed_operation_inputs",
    "hash_bundle",
    "hash_overrides",
    "hash_rendered_payload",
    "hash_schema",
    "hash_template_source",
    "operation_input_digests",
]
