"""Templatize-by-pointing core.

Pure functions; no I/O. Tests cover round-trip semantics so the UI
builder + the outbound build can both rely on these as the single
source of truth.
"""

from __future__ import annotations

import copy
from typing import Any

from primer.model.harness import OverrideMapping


class OverrideSchemaCollisionError(Exception):
    """Raised when two override paths produce incompatible schemas."""

    def __init__(self, override_path: str, detail: str) -> None:
        super().__init__(f"override schema collision at {override_path!r}: {detail}")
        self.override_path = override_path


def infer_schema_fragment(value: Any) -> dict[str, Any]:
    """Infer a JSON Schema fragment + default from a concrete Python value.

    Note: bool must be checked before int because ``bool`` subclasses ``int``.
    """
    if isinstance(value, bool):
        return {"type": "boolean", "default": value}
    if isinstance(value, int):
        return {"type": "integer", "default": value}
    if isinstance(value, float):
        return {"type": "number", "default": value}
    if isinstance(value, str):
        return {"type": "string", "default": value}
    if isinstance(value, list):
        if not value:
            return {"type": "array", "items": {}, "default": []}
        item = infer_schema_fragment(value[0])
        # Drop default from item; only the outer array carries one.
        item.pop("default", None)
        return {"type": "array", "items": item, "default": value}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: infer_schema_fragment(v) for k, v in value.items()},
            "default": value,
        }
    if value is None:
        return {"type": ["null", "string"], "default": None}
    return {"default": value}


def _pointer_segments(field_path: str) -> list[str]:
    """Parse a JSON pointer into its (unescaped) segments.

    ``~1`` → ``/``, ``~0`` → ``~`` (RFC 6901 unescape).
    The root pointer ``"/"`` returns an empty list.
    """
    if not field_path.startswith("/"):
        raise ValueError(f"field_path must be a JSON pointer (got {field_path!r})")
    if field_path == "/":
        return []
    return [seg.replace("~1", "/").replace("~0", "~") for seg in field_path[1:].split("/")]


def _resolve_pointer(entity: Any, field_path: str) -> Any:
    """Walk ``entity`` per ``field_path``; raise KeyError on any miss."""
    cur: Any = entity
    for seg in _pointer_segments(field_path):
        if isinstance(cur, dict):
            if seg not in cur:
                raise KeyError(field_path)
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                idx = int(seg)
            except ValueError as exc:
                raise KeyError(field_path) from exc
            if idx < 0 or idx >= len(cur):
                raise KeyError(field_path)
            cur = cur[idx]
        else:
            raise KeyError(field_path)
    return cur


def _set_pointer(entity: Any, field_path: str, value: Any) -> None:
    """In-place set at ``field_path``. Refuses to assign to the root."""
    segs = _pointer_segments(field_path)
    if not segs:
        raise ValueError("cannot replace root via pointer")
    cur: Any = entity
    for seg in segs[:-1]:
        if isinstance(cur, list):
            cur = cur[int(seg)]
        else:
            cur = cur[seg]
    last = segs[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def apply_override_mappings(
    entity: dict[str, Any],
    mappings: list[OverrideMapping],
) -> dict[str, Any]:
    """Replace each mapped pointer in a deep-copy with ``{{ overrides.<path> }}``.

    Raises ``KeyError(field_path)`` if any mapping's pointer doesn't resolve.
    """
    out = copy.deepcopy(entity)
    for m in mappings:
        # Verify path resolves (raises KeyError if not).
        _resolve_pointer(out, m.field_path)
        token = "{{ overrides." + m.override_path + " }}"
        _set_pointer(out, m.field_path, token)
    return out


def compose_overrides_schema_from_mappings(
    mappings: list[OverrideMapping],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Build a JSON Schema document from a flat list of mappings.

    ``values`` is a dict keyed by ``field_path`` carrying the current value
    used to infer the type + default at each override path. ``widget`` (when
    set) becomes ``x-primer-widget`` on the leaf; ``schema_override`` (when
    set) shallow-merges OVER the inferred fragment.

    Raises ``OverrideSchemaCollisionError`` when two paths produce
    incompatible schemas or an intermediate segment is non-object.
    """
    root: dict[str, Any] = {"type": "object", "properties": {}}
    for m in mappings:
        value = values.get(m.field_path)
        fragment = infer_schema_fragment(value)
        if m.widget is not None:
            fragment["x-primer-widget"] = m.widget
        if m.schema_override:
            fragment = {**fragment, **m.schema_override}
        segs = m.override_path.split(".")
        cur = root
        for seg in segs[:-1]:
            props = cur.setdefault("properties", {})
            sub = props.get(seg)
            if sub is None:
                sub = {"type": "object", "properties": {}}
                props[seg] = sub
            elif sub.get("type") != "object":
                raise OverrideSchemaCollisionError(
                    m.override_path,
                    f"intermediate segment {seg!r} is not object",
                )
            cur = sub
        leaf_key = segs[-1]
        props = cur.setdefault("properties", {})
        existing = props.get(leaf_key)
        if existing is not None and existing != fragment:
            raise OverrideSchemaCollisionError(
                m.override_path,
                f"two mappings produce different schemas: {existing!r} vs {fragment!r}",
            )
        props[leaf_key] = fragment
    return root


__all__ = [
    "OverrideSchemaCollisionError",
    "apply_override_mappings",
    "compose_overrides_schema_from_mappings",
    "infer_schema_fragment",
]
