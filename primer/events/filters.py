"""Three-tier event filter evaluation.

Tier order is cheapest-first and short-circuits:

1. ``event_types`` glob list (then ``exclude_types``),
2. ``fields`` structural matchers (all must hold),
3. ``expr`` - a rego module in package ``primer.event_filter``
   defining a boolean ``match`` rule, evaluated with the event
   document as ``input``.

Failures fail CLOSED: a bad regex, a missing path, an expression that
references the host ``opa`` package or uses a template string, does not
compile, or does not produce a boolean ``match`` all mean "no match"
(logged), mirroring the approval gate's conservative stance.

Both user-written tiers run in the shared, leader-elected dispatcher, so
both are bounded in time (G6-11): a ``regex`` field matcher runs through
the timed ``regex`` engine (:mod:`primer.common.user_regex`, 10 ms per
search, as the channel ``text_pattern``), and an ``expr`` is evaluated on
a dedicated worker thread (regopy's calls release the GIL) that the caller
waits on for at most :data:`_EXPR_TIMEOUT_SECONDS`. rego-cpp cannot be
interrupted, so an evaluation that runs past the bound keeps its thread
until it ends; while it runs, the same expression is answered "no match"
at once instead of being started again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from fnmatch import fnmatchcase
from typing import Any

import regex

from primer.common.user_regex import (
    UserRegexError,
    UserRegexTimeout,
    compile_user_regex,
    timed_search,
)
from primer.model.event import Event, EventFilter, FieldMatcher

logger = logging.getLogger(__name__)

# One regex field search may run this long (the channel text_pattern bound).
_FIELD_REGEX_TIMEOUT_SECONDS = 0.01
# The longest a caller waits for one expr evaluation.
_EXPR_TIMEOUT_SECONDS = 0.5
# One worker: rego-cpp interpreters are evaluated one at a time, and a
# stuck evaluation cannot multiply threads (see _RUNNING_PAST_BOUND).
_REGO_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="event-filter-rego")
# Digests of expressions whose evaluation ran past the bound and has not
# ended yet: they answer "no match" without a new evaluation.
_RUNNING_PAST_BOUND: set[str] = set()
_RUNNING_LOCK = threading.Lock()

_PACKAGE_QUERY = "data.primer.event_filter"
_OPA_PACKAGE_REF = re.compile(r"(?<![\w.])opa\s*(?:\.|\[)")
_OPA_PACKAGE_REFUSAL = (
    "event filter expr may use only the event document through input; "
    "references to the opa package are not allowed"
)
# Rego template strings ($"..." and $`...`) evaluate the code inside their
# {...} parts, so a scan that blanks string contents cannot vet them (G6-2).
# They are refused outright: outside strings and comments ``$`` has no other
# use in Rego, so any ``$`` left in the code-only text opens one.
_TEMPLATE_STRING_REFUSAL = (
    "event filter expr may not use template strings ($\"...\" or $`...`): "
    "the code inside their {...} parts runs unchecked; compare input values "
    "with plain strings instead"
)

# Field patterns that would not compile, so each is logged once.
_BAD_PATTERNS_LOGGED: set[str] = set()
# Validated rego modules by content digest (regopy interpreters are
# cheap to build; the cache only dedupes the validation logging).
_EXPR_SEEN: set[str] = set()


def _rego_code_only(expr_text: str) -> str:
    """Replace Rego strings and comments with spaces for a token check.

    Rego has JSON-style quoted strings, raw backtick strings, and ``#``
    line comments. Keeping the remaining characters in place lets the
    package-reference check accept event values that merely contain the
    text ``opa.`` while still catching whitespace around the access dot.
    """
    chars = list(expr_text)
    index = 0
    while index < len(chars):
        char = chars[index]
        if char == "#":
            while index < len(chars) and chars[index] != "\n":
                chars[index] = " "
                index += 1
            continue
        if char not in {'"', "`"}:
            index += 1
            continue
        quote = char
        chars[index] = " "
        index += 1
        while index < len(chars):
            char = chars[index]
            chars[index] = " "
            index += 1
            if quote == '"' and char == "\\" and index < len(chars):
                chars[index] = " "
                index += 1
                continue
            if char == quote:
                break
    return "".join(chars)


def validate_filter_expr(expr_text: str | None) -> None:
    """Reject host-package references from an event-data expression.

    regopy does not expose an allow-list for builtins. Event filters are
    therefore limited to their ``input`` document and may not reference
    the root ``opa`` package. The check ignores strings and comments, and
    rejects both dotted and indexed root access. Template strings are
    refused outright, because the code in their ``{...}`` parts is inside
    the quotes the check blanks. The same function is the save-time check
    and the evaluation backstop (:func:`evaluate_filter_expr`).
    """
    if not expr_text:
        return
    code = _rego_code_only(expr_text)
    if "$" in code:
        raise ValueError(_TEMPLATE_STRING_REFUSAL)
    if _OPA_PACKAGE_REF.search(code):
        raise ValueError(_OPA_PACKAGE_REFUSAL)


def _log_expr_rejection(expr_text: str, exc: Exception) -> None:
    digest = hashlib.sha256(expr_text.encode("utf-8")).hexdigest()
    if digest not in _EXPR_SEEN:
        _EXPR_SEEN.add(digest)
        logger.warning("event filter expr rejected (no match): %s", exc)


def _compiled(pattern: str) -> regex.Pattern | None:
    """The field pattern compiled by the ``regex`` engine (cached), or ``None`` when it cannot be used.

    No length cap here: the write paths (:func:`validate_filter`) cap what a
    user stores, and the platform builds longer anchored alternations of
    escaped words itself (``primer.canvas.wait``). The repeat cap applies.
    """
    try:
        return compile_user_regex(pattern, max_length=None, what="event filter regex")
    except UserRegexError as exc:
        if pattern not in _BAD_PATTERNS_LOGGED and len(_BAD_PATTERNS_LOGGED) < 1024:
            _BAD_PATTERNS_LOGGED.add(pattern)
            logger.warning("event filter regex %r rejected: %s", pattern[:200], exc)
        return None


def validate_filter(filter_: EventFilter | None) -> None:
    """The save-time check of a whole filter: its ``expr`` (:func:`validate_filter_expr`) and its ``regex`` field patterns.

    A field pattern must compile with the engine that matches it and be at
    most 1000 characters (G6-11). Raises ``ValueError`` naming the field.
    """
    if filter_ is None:
        return
    validate_filter_expr(filter_.expr)
    for matcher in filter_.fields:
        if matcher.op != "regex":
            continue
        try:
            compile_user_regex(matcher.value, what=f"the regex for {matcher.path!r}")
        except UserRegexError as exc:
            raise ValueError(f"invalid regex {matcher.value[:200]!r} for {matcher.path!r}: {exc}") from None


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
    if pattern is None:
        return False
    try:
        return timed_search(pattern, text, timeout=_FIELD_REGEX_TIMEOUT_SECONDS) is not None
    except UserRegexTimeout:
        # A timeout is "no match", not a stalled dispatcher (G6-11).
        return False


def _run_expr(expr_text: str, input_doc: dict[str, Any]) -> bool:
    """Evaluate on the rego worker thread; raise on any failure."""
    import regopy

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


def _digest(expr_text: str) -> str:
    return hashlib.sha256(expr_text.encode("utf-8")).hexdigest()


def _start_expr(expr_text: str, input_doc: dict[str, Any]) -> "Future[bool] | bool":
    """The evaluation's future, or the answer when there is nothing to run (refused, or still running past the bound)."""
    try:
        validate_filter_expr(expr_text)
    except ValueError as exc:
        _log_expr_rejection(expr_text, exc)
        return False
    with _RUNNING_LOCK:
        if _digest(expr_text) in _RUNNING_PAST_BOUND:
            return False
    return _REGO_EXECUTOR.submit(_run_expr, expr_text, input_doc)


def _past_bound(expr_text: str, future: "Future[bool]") -> None:
    digest = _digest(expr_text)
    if not future.cancel():
        with _RUNNING_LOCK:
            _RUNNING_PAST_BOUND.add(digest)

        def _ended(_done: "Future[bool]") -> None:
            with _RUNNING_LOCK:
                _RUNNING_PAST_BOUND.discard(digest)

        future.add_done_callback(_ended)
    logger.warning(
        "event filter expr ran past its %g s bound (no match); until that "
        "evaluation ends the same expr answers no match without running", _EXPR_TIMEOUT_SECONDS,
    )


def _answer(expr_text: str, future: "Future[bool]") -> bool:
    """The finished evaluation's answer; any failure is logged and is no match."""
    try:
        return future.result(timeout=0)
    except (ImportError, OSError) as exc:
        logger.warning("regopy unavailable for event filters: %s", exc)
    except Exception as exc:  # noqa: BLE001 - fail closed by contract
        _log_expr_rejection(expr_text, exc)
    return False


def evaluate_filter_expr(expr_text: str, input_doc: dict[str, Any]) -> bool:
    """Evaluate a ``primer.event_filter`` rego module; fail closed.

    The module must define a boolean ``match`` rule. Any failure -
    regopy missing, compile error, non-boolean result - is logged and
    treated as no-match, and so is an evaluation that runs past
    :data:`_EXPR_TIMEOUT_SECONDS` (G6-11). This form blocks the calling
    thread for at most that long; the dispatcher awaits
    :func:`evaluate_filter_expr_async` instead.
    """
    started = _start_expr(expr_text, input_doc)
    if isinstance(started, bool):
        return started
    done, _pending = wait_for_futures([started], timeout=_EXPR_TIMEOUT_SECONDS)
    if not done:
        _past_bound(expr_text, started)
        return False
    return _answer(expr_text, started)


async def evaluate_filter_expr_async(expr_text: str, input_doc: dict[str, Any]) -> bool:
    """:func:`evaluate_filter_expr` without blocking the event loop while it waits."""
    started = _start_expr(expr_text, input_doc)
    if isinstance(started, bool):
        return started
    waiter = asyncio.wrap_future(started)
    done, _pending = await asyncio.wait([waiter], timeout=_EXPR_TIMEOUT_SECONDS)
    if not done:
        # Unhook the loop from the evaluation that keeps running.
        waiter.cancel()
        _past_bound(expr_text, started)
        return False
    return _answer(expr_text, started)


def _type_tiers_pass(event: Event, filter_: EventFilter) -> bool:
    if not any(
        fnmatchcase(event.event_type, glob) for glob in filter_.event_types
    ):
        return False
    return not any(
        fnmatchcase(event.event_type, glob) for glob in filter_.exclude_types
    )


async def matches_async(event: Event, filter_: EventFilter) -> bool:
    """:func:`matches` for the dispatcher loop: an ``expr`` is awaited, never waited on in the loop's thread."""
    if not _type_tiers_pass(event, filter_):
        return False
    if filter_.fields or filter_.expr:
        doc = event.model_dump(mode="json")
        if not all(_field_holds(doc, m) for m in filter_.fields):
            return False
        if filter_.expr and not await evaluate_filter_expr_async(filter_.expr, doc):
            return False
    return True


def matches(event: Event, filter_: EventFilter) -> bool:
    """Whether ``event`` passes every tier of ``filter_``."""
    if not _type_tiers_pass(event, filter_):
        return False
    if filter_.fields or filter_.expr:
        doc = event.model_dump(mode="json")
        if not all(_field_holds(doc, m) for m in filter_.fields):
            return False
        if filter_.expr and not evaluate_filter_expr(filter_.expr, doc):
            return False
    return True


__all__ = [
    "evaluate_filter_expr",
    "evaluate_filter_expr_async",
    "matches",
    "matches_async",
    "validate_filter",
    "validate_filter_expr",
]
