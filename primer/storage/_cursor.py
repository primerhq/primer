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

    The values are the SERVED (JSON-mode) ones, never the storage form: a
    client can decode the cursor. The backend compares them with the stored
    document, which is the same value for every key a request may sort by:
    a field whose served form differs (a secret the read masks) is refused
    as an ``order_by`` key (:mod:`primer.storage.secret_fields`), so it never
    reaches a cursor.
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


_KEY_FIELDS = {"field", "value", "direction", "is_null"}
_NOT_THIS_REQUEST = (
    "cursor does not belong to this request: pass the next_cursor of the previous page, with the same order_by"
)


def _is_seek_value(value: Any, is_null: Any) -> bool:
    if not isinstance(is_null, bool) or is_null != (value is None):
        return False
    return value is None or isinstance(value, (str, int, float))   # bool is an int


def _decode_cursor_for(cursor: str, order_by: list[OrderBy] | None) -> dict[str, Any]:
    """Decode ``cursor`` and check that THIS request could have issued it.

    The seek compares the cursor's keys with the STORED document, so a key a
    client chose itself would be a search on any field of it, a secret
    included (a forged ``{"field": "git_token", "value": "g"}`` on a list
    route that sorts by nothing at all was a binary search on the token).
    A cursor is accepted only when it carries exactly the keys
    :func:`_encode_cursor_for` emits for this ``order_by``: its fields, in its
    order and directions, then the ``id`` tiebreaker, each with a JSON
    scalar (a non-null string for ``id``). Anything else is a
    :class:`BadRequestError`, as a malformed cursor is.
    """
    state = _decode_cursor(cursor)
    expected = [(ob.field, ob.direction) for ob in order_by or []] + [("id", "asc")]
    keys = state.get("keys") if isinstance(state, dict) else None
    if not isinstance(keys, list) or len(keys) != len(expected) or set(state) != {"keys"}:
        raise BadRequestError(_NOT_THIS_REQUEST)
    for key, (field, direction) in zip(keys, expected, strict=True):
        if (
            not isinstance(key, dict)
            or set(key) != _KEY_FIELDS
            or key["field"] != field
            or key["direction"] != direction
            or not _is_seek_value(key["value"], key["is_null"])
        ):
            raise BadRequestError(_NOT_THIS_REQUEST)
    if not isinstance(keys[-1]["value"], str):
        raise BadRequestError(_NOT_THIS_REQUEST)
    return state


def _resolve_dotted(d: dict[str, Any], path: str) -> Any:
    """Walk a dotted path through a dumped model dict."""
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


__all__ = ["_decode_cursor", "_decode_cursor_for", "_encode_cursor_for", "_resolve_dotted"]
