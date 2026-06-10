"""Opaque-cursor encode/decode shared by every Storage backend.

The cursor payload (an ordered list of seek keys plus the implicit
``id`` tiebreaker) is provider-agnostic. The per-backend keyset-seek
``WHERE`` clause that consumes the decoded keys is built by the
backend's own predicate translator.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from primer.model.common import Identifiable
from primer.model.except_ import BadRequestError
from primer.model.storage import OrderBy


def _encode_cursor_for(
    entity: Identifiable,
    order_by: list[OrderBy] | None,
) -> str:
    """Build the cursor that seeks past ``entity``.

    Encodes the values of every ``order_by`` key + the entity's id
    (the implicit ASC tiebreaker). The result is opaque
    base64-urlsafe JSON.
    """
    keys: list[dict[str, Any]] = []
    dumped = entity.model_dump(mode="json")
    for ob in order_by or []:
        if ob.field == "id":
            value: Any = dumped.get("id")
        else:
            value = _resolve_dotted(dumped, ob.field)
        keys.append(
            {
                "field": ob.field,
                "value": value,
                "direction": ob.direction,
                # NULL-flag for null-safe keyset seeks. Both backends
                # order by ``(field IS NULL, field, id)`` with NULLs
                # sorted LAST, so the seek predicate must compare this
                # flag lexicographically ahead of the value.
                "is_null": value is None,
            }
        )
    keys.append(
        {"field": "id", "value": dumped["id"], "direction": "asc", "is_null": False}
    )
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    """Inverse of :func:`_encode_cursor_for`. Raises on malformed input."""
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BadRequestError(f"malformed cursor: {exc}", cause=exc) from exc


def _resolve_dotted(d: dict[str, Any], path: str) -> Any:
    """Walk a dotted path through a dumped model dict."""
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


__all__ = ["_decode_cursor", "_encode_cursor_for", "_resolve_dotted"]
