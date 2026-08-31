"""Three-tier event filter evaluation.

Tier order is cheapest-first and short-circuits:

1. ``event_types`` glob list (then ``exclude_types``),
2. ``fields`` structural matchers (all must hold),
3. ``expr`` - a rego module in package ``primer.event_filter``
   defining a boolean ``match`` rule, evaluated with the event
   document as ``input``.

Failures fail CLOSED: a bad regex, a missing path, an expression that
does not compile or does not produce a boolean ``match`` all mean "no
match" (logged), mirroring the approval gate's conservative stance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from fnmatch import fnmatchcase
from typing import Any

from primer.model.event import Event, EventFilter, FieldMatcher

logger = logging.getLogger(__name__)

_PACKAGE_QUERY = "data.primer.event_filter"

# Compiled-regex cache; bad patterns cache as None so each is logged once.
_REGEX_CACHE: dict[str, re.Pattern | None] = {}
# Validated rego modules by content digest (regopy interpreters are
# cheap to build; the cache only dedupes the validation logging).
_EXPR_SEEN: set[str] = set()


def _compiled(pattern: str) -> re.Pattern | None:
    if pattern not in _REGEX_CACHE:
        try:
            _REGEX_CACHE[pattern] = re.compile(pattern)
        except re.error as exc:
            logger.warning(
                "event filter regex %r rejected: %s", pattern, exc,
            )
            _REGEX_CACHE[pattern] = None
    return _REGEX_CACHE[pattern]


def _lookup_path(doc: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """Resolve a dotted path in the envelope, falling back to payload.

    Returns ``(found, value)`` so a stored ``None`` is distinguishable
    from a missing path.
    """

    def _walk(root: Any, parts: list[str]) -> tuple[bool, Any]:
        node = root
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return False, None
            node = node[part]
        return True, node

    parts = dotted.split(".")
    found, value = _walk(doc, parts)
    if not found and parts[0] != "payload":
        found, value = _walk(doc.get("payload") or {}, parts)
    return found, value


def _field_holds(doc: dict[str, Any], matcher: FieldMatcher) -> bool:
    found, value = _lookup_path(doc, matcher.path)
    if not found:
        return False
    text = value if isinstance(value, str) else json.dumps(value)
    if matcher.op == "eq":
        return text == matcher.value or (
            not isinstance(value, str) and str(value) == matcher.value
        )
    if matcher.op == "prefix":
        return text.startswith(matcher.value)
    pattern = _compiled(matcher.value)
    return bool(pattern.search(text)) if pattern else False


def evaluate_filter_expr(expr_text: str, input_doc: dict[str, Any]) -> bool:
    """Evaluate a ``primer.event_filter`` rego module; fail closed.

    The module must define a boolean ``match`` rule. Any failure -
    regopy missing, compile error, non-boolean result - is logged and
    treated as no-match.
    """
    try:
        import regopy
    except (ImportError, OSError) as exc:
        logger.warning("regopy unavailable for event filters: %s", exc)
        return False
    try:
        interpreter = regopy.Interpreter()
        interpreter.add_module("primer_event_filter", expr_text)
        interpreter.set_input(input_doc)
        raw = interpreter.query(_PACKAGE_QUERY)
        if not raw.ok():
            raise ValueError(str(raw))
        parsed = json.loads(str(raw))
        if isinstance(parsed, dict) and "expressions" in parsed:
            expressions = parsed["expressions"]
            doc = expressions[0] if expressions else {}
        else:
            doc = parsed
        match = doc.get("match") if isinstance(doc, dict) else None
        if not isinstance(match, bool):
            raise ValueError(
                "expression must define a boolean `match` rule"
            )
        return match
    except Exception as exc:  # noqa: BLE001 - fail closed by contract
        digest = hashlib.sha256(expr_text.encode("utf-8")).hexdigest()
        if digest not in _EXPR_SEEN:
            _EXPR_SEEN.add(digest)
            logger.warning("event filter expr rejected (no match): %s", exc)
        return False


def matches(event: Event, filter_: EventFilter) -> bool:
    """Whether ``event`` passes every tier of ``filter_``."""
    if not any(
        fnmatchcase(event.event_type, glob) for glob in filter_.event_types
    ):
        return False
    if any(
        fnmatchcase(event.event_type, glob) for glob in filter_.exclude_types
    ):
        return False
    if filter_.fields or filter_.expr:
        doc = event.model_dump(mode="json")
        if not all(_field_holds(doc, m) for m in filter_.fields):
            return False
        if filter_.expr and not evaluate_filter_expr(filter_.expr, doc):
            return False
    return True


__all__ = ["matches", "evaluate_filter_expr"]
