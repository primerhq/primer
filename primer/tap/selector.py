"""TapSelector — predicate-based filtering for workspace tap streams.

Two public surfaces:

* :class:`TapSelector` — a Pydantic model holding optional predicates for
  the *sessions* query (storage layer) and the *events* stream (in-memory).
* :func:`session_predicate_for_storage` — builds a :class:`~primer.model.storage.Predicate`
  suitable for the storage ``find``/``list`` call, always scoping to the
  given ``workspace_id``.
* :func:`event_matches` — evaluates the ``events`` predicate in memory
  against a single :class:`~primer.tap.event.TapEvent`.
* :func:`match_events`, the readers' batch form: when the predicate holds a
  ``LIKE`` / ``ILIKE`` it runs in a worker thread under one time budget.
* :func:`check_selector`, the decode-time check of the ``LIKE`` patterns.

A ``LIKE`` pattern is the caller's (G6-4): ``%`` becomes ``.*``, so
``%a%a%a%a%!`` backtracks polynomially on a long value. Patterns are capped
at :data:`LIKE_PATTERN_MAX_LENGTH` characters, compiled with the ``regex``
engine (cached) and matched with the GIL released under one
:data:`TAP_MATCH_SECONDS` budget per read; a read that runs the budget out
raises :class:`TapSelectorTimeout`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import regex
from pydantic import BaseModel, PrivateAttr

from primer.common.user_regex import (
    MAX_PATTERN_LENGTH,
    MatchBudget,
    UserRegexError,
    compile_user_regex,
    run_interactive,
)
from primer.model.except_ import BadRequestError
from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.tap.event import TapEvent

# The longest LIKE / ILIKE pattern a selector's ``events`` predicate may hold.
LIKE_PATTERN_MAX_LENGTH = MAX_PATTERN_LENGTH
# The total time one read (an SSE tick, an MCP drain) may spend matching
# LIKE patterns, over every event it filters.
TAP_MATCH_SECONDS = 1.0


class TapSelectorError(ValueError):
    """A selector's ``events`` predicate cannot be evaluated safely (a LIKE pattern that is too long or invalid)."""


class TapSelectorTimeout(BadRequestError):
    """Matching a selector's LIKE patterns ran out of the read's time budget."""

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

    # The compiled LIKE / ILIKE patterns of ``events``, compiled once for as
    # long as this selector lives (one stream or one drain): a selector's
    # patterns are one-shot, so they never enter the process-wide cache.
    _like_compiled: dict[tuple[str, int], regex.Pattern] = PrivateAttr(default_factory=dict)

    def compiled_like(self, pattern: str, flags: int) -> regex.Pattern:
        """``pattern`` translated and compiled, once per selector (:class:`TapSelectorError` when refused)."""
        key = (pattern, flags)
        compiled = self._like_compiled.get(key)
        if compiled is None:
            compiled = _like_to_regex(pattern, flags=flags)
            self._like_compiled[key] = compiled
        return compiled


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


def pinned_session_ids(selector: TapSelector) -> frozenset[str] | None:
    """The session ids ``selector.sessions`` confines the tap to (``id`` EQ or IN, also under AND/OR), or ``None`` when it does not pin ``id``."""

    def pins(node: Any) -> frozenset[str] | None:
        if not isinstance(node, Predicate):
            return None
        if node.op in (Op.AND, Op.OR):
            left, right = pins(node.left), pins(node.right)
            if node.op is Op.AND:
                if left is not None and right is not None:
                    return left & right
                return left if left is not None else right
            return left | right if left is not None and right is not None else None
        if not (isinstance(node.left, FieldRef) and node.left.name == "id" and isinstance(node.right, Value)):
            return None
        value = node.right.value
        if node.op is Op.EQ and isinstance(value, str):
            return frozenset({value})
        if node.op is Op.IN and isinstance(value, list) and all(isinstance(v, str) for v in value):
            return frozenset(value)
        return None

    return pins(selector.sessions) if selector.sessions is not None else None


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


def _like_to_regex(pattern: str, *, flags: int = 0) -> regex.Pattern:
    """Convert a SQL LIKE pattern to a compiled (cached) ``regex`` pattern.

    * ``%`` -> ``.*``
    * ``_`` -> ``.``
    * All other regex metacharacters are escaped.

    ``flags`` is passed through to the compile; pass ``regex.IGNORECASE``
    for a case-insensitive (SQL ILIKE) match. Raises
    :class:`TapSelectorError` for a pattern over
    :data:`LIKE_PATTERN_MAX_LENGTH` characters.
    """
    if len(pattern) > LIKE_PATTERN_MAX_LENGTH:
        raise TapSelectorError(
            f"a LIKE pattern in the selector is {len(pattern)} characters long; "
            f"the limit is {LIKE_PATTERN_MAX_LENGTH}"
        )
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(regex.escape(ch))
        i += 1
    try:
        # The translation, not the user's text, is compiled: escaping can
        # double its length, so the cap above is the one that applies.
        # DOTALL: % and _ cross newlines, as in SQL. \Z, not $: the pattern must match the WHOLE value (``$`` also matches before a trailing newline).
        return compile_user_regex("".join(["^"] + parts + [r"\Z"]), flags | regex.DOTALL, max_length=None)
    except UserRegexError as exc:
        raise TapSelectorError(f"a LIKE pattern in the selector cannot be used: {exc}") from None


def _like_patterns(pred: Predicate | None) -> list[tuple[Op, str]]:
    """Every (op, pattern) LIKE / ILIKE comparison in ``pred``."""
    found: list[tuple[Op, str]] = []
    stack = [pred] if pred is not None else []
    while stack:
        node = stack.pop()
        if node.op in (Op.AND, Op.OR):
            stack.extend(side for side in (node.left, node.right) if isinstance(side, Predicate))
        elif node.op in (Op.LIKE, Op.ILIKE) and isinstance(node.right, Value) and isinstance(node.right.value, str):
            found.append((node.op, node.right.value))
    return found


def _check_fields(pred: Predicate | None) -> None:
    """Refuse a field name an event cannot resolve, and a LIKE pattern that is not text, in every comparison of ``pred``."""
    stack = [pred] if pred is not None else []
    while stack:
        node = stack.pop()
        if node.op in (Op.AND, Op.OR):
            stack.extend(side for side in (node.left, node.right) if isinstance(side, Predicate))
            continue
        if isinstance(node.left, FieldRef):
            name = node.left.name
            if not (name in ("class", "class_") or name in _TAP_FIELDS or name.startswith("payload.")):
                raise TapSelectorError(
                    f"unknown tap field {name!r}: valid fields are 'class', {sorted(_TAP_FIELDS)} and 'payload.<key>'"
                )
        if node.op in (Op.LIKE, Op.ILIKE) and not (isinstance(node.right, Value) and isinstance(node.right.value, str)):
            raise TapSelectorError(f"a {node.op.value} comparison in the selector needs a text pattern")


def check_selector(selector: TapSelector) -> None:
    """Refuse a selector whose ``events`` LIKE patterns are too long or do not compile (:class:`TapSelectorError`).

    The SSE route and the MCP tap tool call it when they decode a selector,
    so the caller is told before a stream starts.
    """
    for op, pattern in _like_patterns(selector.events):
        selector.compiled_like(pattern, regex.IGNORECASE if op is Op.ILIKE else 0)
    _check_fields(selector.events)


def _timeout_error(budget: MatchBudget) -> TapSelectorTimeout:
    return TapSelectorTimeout(
        f"tap selector stopped: matching its LIKE patterns used the {budget.seconds:g} s "
        "budget of one read. A pattern of many % wildcards ('%a%a%a%a%!') backtracks "
        "on long values; simplify the selector."
    )


@dataclass(frozen=True)
class _Eval:
    """What one evaluation needs besides the event: the selector (its compiled LIKEs) and the read's budget."""

    selector: TapSelector
    budget: MatchBudget


def _eval_predicate(pred: Predicate, event: TapEvent, budget: _Eval) -> bool:
    """Recursively evaluate *pred* against *event*."""
    op = pred.op

    # ------------------------------------------------------------------
    # Logical operators — both sides are sub-predicates
    # ------------------------------------------------------------------
    if op is Op.AND:
        assert isinstance(pred.left, Predicate) and isinstance(pred.right, Predicate)
        return _eval_predicate(pred.left, event, budget) and _eval_predicate(pred.right, event, budget)

    if op is Op.OR:
        assert isinstance(pred.left, Predicate) and isinstance(pred.right, Predicate)
        return _eval_predicate(pred.left, event, budget) or _eval_predicate(pred.right, event, budget)

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
        return _ordered(field_val, rhs, lambda a, b: a > b)

    if op is Op.LT:
        return _ordered(field_val, rhs, lambda a, b: a < b)

    if op is Op.GE:
        return _ordered(field_val, rhs, lambda a, b: a >= b)

    if op is Op.LE:
        return _ordered(field_val, rhs, lambda a, b: a <= b)

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
        # SQL: LIKE on a value that is not text (a missing key, a number) is not a match, never an error.
        return isinstance(field_val, str) and _like_matches(rhs, field_val, 0, budget)

    if op is Op.ILIKE:
        assert isinstance(rhs, str), f"Op.ILIKE expects a string pattern, got {type(rhs)}"
        return isinstance(field_val, str) and _like_matches(rhs, field_val, regex.IGNORECASE, budget)

    # Should be unreachable if Op enum is exhaustive, but guard anyway.
    raise NotImplementedError(f"Unsupported Op: {op!r}")  # pragma: no cover


def _ordered(left: Any, right: Any, compare: Any) -> bool:
    """``compare(left, right)`` with SQL semantics: a missing value or values that cannot be ordered against each other are not a match."""
    if left is None or right is None:
        return False
    try:
        return bool(compare(left, right))
    except TypeError:
        return False


def _like_matches(pattern: str, value: str, flags: int, ctx: _Eval) -> bool:
    compiled = ctx.selector.compiled_like(pattern, flags)
    try:
        return ctx.budget.match(compiled, value) is not None
    except TimeoutError as exc:
        raise _timeout_error(ctx.budget) from exc


def _new_budget() -> MatchBudget:
    return MatchBudget(TAP_MATCH_SECONDS, what="tap selector LIKE pattern")


def event_matches(selector: TapSelector, event: TapEvent, *, budget: MatchBudget | None = None) -> bool:
    """Return ``True`` if *event* satisfies *selector.events*.

    When ``selector.events`` is ``None`` every event matches (pass-through).
    Otherwise the predicate tree is evaluated in memory against *event*,
    its LIKE patterns under ``budget`` (a fresh one when not given).

    Raises :class:`ValueError` if a :class:`~primer.model.storage.FieldRef`
    names a field that does not exist on :class:`~primer.tap.event.TapEvent`,
    :class:`TapSelectorError` for a LIKE pattern that is too long, and
    :class:`TapSelectorTimeout` when the budget runs out.
    """
    if selector.events is None:
        return True
    return _eval_predicate(selector.events, event, _Eval(selector, budget if budget is not None else _new_budget()))


def new_read_budget() -> MatchBudget:
    """The budget of one read (an SSE tick or a drain): pass it to every :func:`match_events` batch of that read."""
    return _new_budget()


async def match_events(
    selector: TapSelector, events: list[TapEvent], *, budget: MatchBudget | None = None,
) -> list[bool]:
    """``event_matches`` for each of ``events``, in order.

    A predicate with a LIKE / ILIKE runs on the interactive user-regex executor (the
    engine releases the GIL) under ONE :data:`TAP_MATCH_SECONDS` budget for
    the read (``budget``, shared by the read's batches; a fresh one when not
    given), so a hostile pattern neither stalls the event loop nor runs past
    the bound; any other predicate is evaluated inline.
    """
    if selector.events is None:
        return [True] * len(events)
    ctx = _Eval(selector, budget if budget is not None else _new_budget())
    if not events or not _like_patterns(selector.events):
        return [_eval_predicate(selector.events, event, ctx) for event in events]

    def _all() -> list[bool]:
        return [_eval_predicate(selector.events, event, ctx) for event in events]

    return await run_interactive(_all)
