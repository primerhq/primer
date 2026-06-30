"""TapSelector — predicate-based filtering for workspace tap streams.

Two public surfaces:

* :class:`TapSelector` — a Pydantic model holding optional predicates for
  the *sessions* query (storage layer) and the *events* stream (in-memory).
* :func:`session_predicate_for_storage` — builds a :class:`~primer.model.storage.Predicate`
  suitable for the storage ``find``/``list`` call, always scoping to the
  given ``workspace_id``.
* :func:`event_matches` — evaluates the ``events`` predicate in memory
  against a single :class:`~primer.tap.event.TapEvent`.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.tap.event import TapEvent

# ---------------------------------------------------------------------------
# Public model
# ---------------------------------------------------------------------------


class TapSelector(BaseModel):
    """Filter spec for a workspace tap subscription.

    Both fields are optional; ``None`` means "no additional constraint".

    * ``sessions`` — extra predicate ANDed with the ``workspace_id`` scope
      when querying the session store.
    * ``events`` — predicate evaluated in memory against each
      :class:`~primer.tap.event.TapEvent` before it is emitted.
    """

    sessions: Predicate | None = None
    events: Predicate | None = None


# ---------------------------------------------------------------------------
# session_predicate_for_storage
# ---------------------------------------------------------------------------

_WORKSPACE_FIELD = "workspace_id"


def session_predicate_for_storage(workspace_id: str, selector: TapSelector) -> Predicate:
    """Return a storage predicate that scopes sessions to *workspace_id*.

    When *selector.sessions* is ``None`` the result is simply::

        workspace_id == workspace_id

    When *selector.sessions* is provided the result is::

        (workspace_id == workspace_id) AND selector.sessions

    This guarantees that callers can never accidentally query across workspace
    boundaries regardless of what the user passes in *selector*.
    """
    ws_pred = Predicate(
        left=FieldRef(name=_WORKSPACE_FIELD),
        op=Op.EQ,
        right=Value(value=workspace_id),
    )
    if selector.sessions is None:
        return ws_pred
    return Predicate(
        left=ws_pred,
        op=Op.AND,
        right=selector.sessions,
    )


# ---------------------------------------------------------------------------
# event_matches
# ---------------------------------------------------------------------------

# Known TapEvent field names (excluding class_ which has the alias "class").
_TAP_FIELDS = frozenset(
    {
        "cursor",
        "workspace_id",
        "session_id",
        "agent_id",
        "graph_id",
        "node_id",
        "ts",
        "payload",
    }
)


def _resolve_field(name: str, event: TapEvent) -> Any:
    """Resolve a :class:`~primer.model.storage.FieldRef` name to its value.

    Recognised names:

    * ``"class"`` or ``"class_"`` → ``event.class_.value`` (the string value
      of the :class:`~primer.tap.event.TapEventClass` enum).
    * Any other :attr:`TapEvent` attribute name → the attribute value.
    * ``"payload.<key>"`` → ``event.payload.get(key)``, or ``None`` when the
      key is absent.

    Raises :class:`ValueError` for any name that cannot be resolved.
    """
    if name in ("class", "class_"):
        return event.class_.value

    if name.startswith("payload."):
        key = name[len("payload."):]
        return event.payload.get(key)

    if name in _TAP_FIELDS:
        return getattr(event, name)

    raise ValueError(
        f"Unknown TapEvent field reference: {name!r}. "
        f"Valid fields: 'class', 'class_', {sorted(_TAP_FIELDS)}, or 'payload.<key>'."
    )


def _like_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a SQL LIKE pattern to a compiled regex (case-sensitive).

    * ``%`` → ``.*``
    * ``_`` → ``.``
    * All other regex metacharacters are escaped.
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
        i += 1
    return re.compile("".join(["^"] + parts + ["$"]))


def _eval_predicate(pred: Predicate, event: TapEvent) -> bool:
    """Recursively evaluate *pred* against *event*."""
    op = pred.op

    # ------------------------------------------------------------------
    # Logical operators — both sides are sub-predicates
    # ------------------------------------------------------------------
    if op is Op.AND:
        assert isinstance(pred.left, Predicate) and isinstance(pred.right, Predicate)
        return _eval_predicate(pred.left, event) and _eval_predicate(pred.right, event)

    if op is Op.OR:
        assert isinstance(pred.left, Predicate) and isinstance(pred.right, Predicate)
        return _eval_predicate(pred.left, event) or _eval_predicate(pred.right, event)

    # ------------------------------------------------------------------
    # Comparison operators — left is FieldRef, right is Value
    # ------------------------------------------------------------------
    assert isinstance(pred.left, FieldRef), (
        f"Expected FieldRef on left side of {op!r}, got {type(pred.left)}"
    )
    assert isinstance(pred.right, Value), (
        f"Expected Value on right side of {op!r}, got {type(pred.right)}"
    )

    field_val = _resolve_field(pred.left.name, event)
    rhs = pred.right.value

    if op is Op.EQ:
        return field_val == rhs

    if op is Op.NE:
        return field_val != rhs

    if op is Op.GT:
        return field_val > rhs  # type: ignore[operator]

    if op is Op.LT:
        return field_val < rhs  # type: ignore[operator]

    if op is Op.GE:
        return field_val >= rhs  # type: ignore[operator]

    if op is Op.LE:
        return field_val <= rhs  # type: ignore[operator]

    if op is Op.IN:
        assert isinstance(rhs, list), f"Op.IN expects a list right-hand side, got {type(rhs)}"
        return field_val in rhs

    if op is Op.CONTAINS:
        # field_val is a JSON array; rhs is the scalar to look for.
        if not isinstance(field_val, list):
            return False
        return rhs in field_val

    if op is Op.IS_NULL:
        return field_val is None

    if op is Op.IS_NOT_NULL:
        return field_val is not None

    if op is Op.LIKE:
        assert isinstance(rhs, str), f"Op.LIKE expects a string pattern, got {type(rhs)}"
        assert isinstance(field_val, str), (
            f"Op.LIKE can only match string fields, got {type(field_val)}"
        )
        return bool(_like_to_regex(rhs).match(field_val))

    # Should be unreachable if Op enum is exhaustive, but guard anyway.
    raise NotImplementedError(f"Unsupported Op: {op!r}")  # pragma: no cover


def event_matches(selector: TapSelector, event: TapEvent) -> bool:
    """Return ``True`` if *event* satisfies *selector.events*.

    When ``selector.events`` is ``None`` every event matches (pass-through).
    Otherwise the predicate tree is evaluated in memory against *event*.

    Raises :class:`ValueError` if a :class:`~primer.model.storage.FieldRef`
    names a field that does not exist on :class:`~primer.tap.event.TapEvent`.
    """
    if selector.events is None:
        return True
    return _eval_predicate(selector.events, event)
