"""JSON Schema validation whose regex keywords run bounded, off the event loop (G6-4).

A graph node's ``output_schema`` is the graph author's, and the instance it validates is model or tool output. jsonschema runs
``pattern`` (and ``patternProperties``, which ``additionalProperties`` and ``unevaluatedProperties`` consult too) with stdlib
``re.search``: no time bound, and the GIL held throughout, so ``{"pattern": "(a|aa)+$"}`` against a long string stalled the
worker's event loop, the hazard #764 refused for ``ask_user``.

jsonschema has no hook for its regex engine; its keyword modules call ``re.search`` through their module-level ``re``. Those
names are pointed at :class:`_BoundedRe`, which is the stdlib module for every caller EXCEPT inside :func:`validate_bounded`,
where a context variable holds a :class:`~primer.common.user_regex.MatchBudget`: there a pattern is compiled through
:func:`~primer.common.user_regex.compile_user_regex` (1000 characters, the engine-parsed repeat cap, the ``regex`` engine;
a stored pattern, so the compile and its verdict are cached) and searched under the budget with the GIL released, on the
interactive user-regex executor. Every other jsonschema user in the process (the MCP argument check, the
harness overrides, the ask_user answer) sees no change. ``tests/common/test_bounded_jsonschema.py`` pins each keyword route, so
a jsonschema release that stops reading ``re`` through those modules fails the suite instead of silently unbounding them.

The rest of the validation is bounded as well (GS-17). An instance over its size bound - the default
:data:`MAX_INSTANCE_BYTES` (1 MiB); callers validating bodies the app admits further pass a larger
``max_instance_bytes``, measured as the instance's real UTF-8 JSON - or 64 levels deep is refused before
validating, a ``RecursionError`` is a violation, and every subschema step and every ``uniqueItems`` comparison checks one
deadline of the same ``seconds`` (a spent deadline raises :class:`ValidationTimeout`). Each bound raises ``jsonschema.ValidationError``,
so callers report it as a violation.
"""

from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from typing import Any

import jsonschema
from jsonschema import _keywords, _legacy_keywords, _utils

from primer.common.user_regex import MatchBudget, UserRegexTimeout, compile_user_regex, run_interactive

# The total time one validation may spend in its regex keywords, and in the whole validation.
OUTPUT_SCHEMA_MATCH_SECONDS = 1.0

# The largest instance validated: its compact JSON, and how many containers deep it nests.
MAX_INSTANCE_BYTES = 1024 * 1024
MAX_INSTANCE_DEPTH = 64

_BUDGET: ContextVar[MatchBudget | None] = ContextVar("primer_jsonschema_regex_budget", default=None)
# perf_counter() past which a validation stops (GS-17): uniqueItems compares objects pairwise, and a schema that branches at
# every level is exponential in the depth, so a regex budget alone left both unbounded.
_DEADLINE: ContextVar[float | None] = ContextVar("primer_jsonschema_deadline", default=None)


def _check_deadline() -> None:
    deadline = _DEADLINE.get()
    if deadline is not None and time.perf_counter() > deadline:
        raise ValidationTimeout(
            "validation stopped: it used its time budget (uniqueItems over many objects, or a schema that branches "
            "at every level, is slow)"
        )


class ValidationTimeout(jsonschema.ValidationError):
    """The whole-validation deadline expired mid-validation (GS-17).

    A :class:`jsonschema.ValidationError` subclass, so the handlers that
    already catch ``ValidationError`` (the graph node checks, the yields
    response_schema) keep reporting it as a violation with the same
    message. Callers that can tell "the schema is too slow to run safely"
    apart from "the arguments are wrong" (the MCP meta-tools) catch this
    first and answer the tool-side refusal instead of blaming the caller.
    """


class _BoundedRe:
    """The ``re`` module as jsonschema's keyword modules see it: stdlib, except ``search`` inside :func:`validate_bounded`."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def search(self, pattern: Any, string: Any, flags: int = 0) -> Any:
        budget = _BUDGET.get()
        if budget is None or not isinstance(pattern, str) or not isinstance(string, str) or flags:
            return self._real.search(pattern, string, flags)
        if budget.exhausted:
            # Spent: answer before compiling anything (#859 round 3, R2-9).
            raise UserRegexTimeout(
                f"{budget.what} matching used its {budget.seconds:g} s budget", seconds=budget.seconds,
            )
        started = time.perf_counter()
        try:
            # A schema is stored with its graph and runs again: a stored pattern on its match path (cached, verdict included).
            compiled = compile_user_regex(pattern, what=budget.what, stored=True)
        finally:
            # A compile is work done for this validation: it is charged like a search.
            budget.charge(time.perf_counter() - started)
        return budget.search(compiled, string)


_PATCHED_MODULES = (_keywords, _legacy_keywords, _utils)

# Every validator class validate_bounded can pick: each has its own descend, called once per subschema evaluated.
_VALIDATORS = (
    jsonschema.Draft3Validator, jsonschema.Draft4Validator, jsonschema.Draft6Validator, jsonschema.Draft7Validator,
    jsonschema.Draft201909Validator, jsonschema.Draft202012Validator,
)


def _checked(real: Any) -> Any:
    """``real`` that first checks the validation's deadline (only inside :func:`validate_bounded`)."""
    if getattr(real, "_primer_checked", False):
        return real

    def checked(*args: Any, **kwargs: Any) -> Any:
        _check_deadline()
        return real(*args, **kwargs)

    checked._primer_checked = True  # type: ignore[attr-defined]
    return checked


def _install() -> None:
    for module in _PATCHED_MODULES:
        current = getattr(module, "re", None)
        if current is re:
            module.re = _BoundedRe(re)
        elif not isinstance(current, _BoundedRe):
            raise RuntimeError(f"{module.__name__} no longer reads the re module; G6-4's bounded validation needs a new hook")
    # uniq() reaches equal() through _utils' module global, and equal() recurses through it too.
    _utils.equal = _checked(_utils.equal)
    for cls in _VALIDATORS:
        cls.descend = _checked(cls.descend)


_install()


def _deeper_than(value: Any, limit: int) -> bool:
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else None
        if children is None:
            continue
        if depth >= limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


# The size check's encoder (review 2026-11-23, X3.6.2): the same compact
# form the old json.dumps call built (separators, ensure_ascii=False), but
# iterencode yields the JSON in chunks, so the byte size is summed without
# materializing the whole string. The old dumps + encode held ~2x a 32 MiB
# body per validation thread (the string, then its UTF-8 encoding); the
# chunk sum holds the largest chunk.
_COMPACT = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False)


def _compact_json_size(instance: Any, max_bytes: int) -> int | None:
    """The UTF-8 byte length of ``instance``'s compact JSON, summed over
    ``iterencode`` chunks (never materialized), or ``None`` as soon as the
    running total passes ``max_bytes``: the refusal needs no further
    measuring (review 2026-11-23, X3.6.2). A chunk's UTF-8 length is its
    ``len`` when it is ASCII, and the ``surrogatepass`` encoding otherwise
    (a lone surrogate a ``json.loads`` admits is 3 bytes, never an encode
    error). Serialization errors (a TypeError, a ValueError, a circular
    structure's RecursionError) propagate exactly as the old dumps call's
    did.
    """
    total = 0
    for chunk in _COMPACT.iterencode(instance):
        total += len(chunk) if chunk.isascii() else len(chunk.encode("utf-8", errors="surrogatepass"))
        if total > max_bytes:
            return None
    return total


def validate_bounded(
    instance: Any,
    schema: dict[str, Any],
    *,
    seconds: float = OUTPUT_SCHEMA_MATCH_SECONDS,
    label: str = "output_schema pattern",
    max_instance_bytes: int = MAX_INSTANCE_BYTES,
) -> None:
    """``jsonschema.validate``, its regex keywords under one ``seconds`` budget and the whole validation under ``seconds``.

    ``label`` names the schema in the budget and pattern errors
    (``output_schema pattern`` for the existing callers; ``input_schema``
    for the session-create pre-check of a graph's ``input_schema``).

    ``max_instance_bytes`` sizes the largest instance that validates: the
    default is :data:`MAX_INSTANCE_BYTES` (the historical bound for the
    model/tool-output callers). Callers that receive bodies the app lets
    through further (the MCP meta-tools, which the body-cap middleware
    admits up to the app's request-body cap) pass that cap, so a body the
    middleware accepted is not refused here. The size is the instance's
    real UTF-8 JSON (``ensure_ascii=False``), so a multi-byte string is
    measured as its actual size, not its ASCII-escaped inflation; it is
    summed over the stdlib encoder's chunks, so the check never holds a
    copy of the instance's JSON (review 2026-11-23, X3.6.2).

    Raises ``jsonschema.ValidationError`` as before, and also when the
    instance is over ``max_instance_bytes`` or :data:`MAX_INSTANCE_DEPTH`,
    when the schema or the instance nests too deeply to validate, or when the
    validation runs past ``seconds`` (that one as
    :class:`ValidationTimeout`); and
    :class:`~primer.common.user_regex.UserRegexError` (a schema pattern over
    the size caps) or :class:`~primer.common.user_regex.UserRegexTimeout`
    (the regex budget ran out).
    """
    try:
        # surrogatepass: a lone surrogate (json.loads accepts a \\ud800 escape) is a string to jsonschema, 3 bytes here, never an encode error.
        if _compact_json_size(instance, max_instance_bytes) is None:
            raise jsonschema.ValidationError(
                f"the value is larger than {max_instance_bytes >> 20} MiB of JSON, the most a schema validates"
            )
        if _deeper_than(instance, MAX_INSTANCE_DEPTH):
            raise jsonschema.ValidationError(
                f"the value nests deeper than {MAX_INSTANCE_DEPTH} levels, the most a schema validates"
            )
        token = _BUDGET.set(MatchBudget(seconds, what=label))
        try:
            # jsonschema.validate, with the deadline on the instance's validation (the schema check is linear in the schema).
            cls = jsonschema.validators.validator_for(schema)
            cls.check_schema(schema)
            deadline = _DEADLINE.set(time.perf_counter() + seconds)
            try:
                error = jsonschema.exceptions.best_match(cls(schema).iter_errors(instance))
            finally:
                _DEADLINE.reset(deadline)
            if error is not None:
                raise error
        finally:
            _BUDGET.reset(token)
    except RecursionError:
        raise jsonschema.ValidationError("the schema or the value nests too deeply to validate") from None


async def validate_bounded_off_loop(
    instance: Any, schema: dict[str, Any], *, seconds: float | None = None,
    label: str = "output_schema pattern", max_instance_bytes: int = MAX_INSTANCE_BYTES,
) -> None:
    """:func:`validate_bounded` in a worker thread (the regex engine releases the GIL while it matches)."""
    bound = OUTPUT_SCHEMA_MATCH_SECONDS if seconds is None else seconds
    await run_interactive(
        validate_bounded, instance, schema,
        seconds=bound, label=label, max_instance_bytes=max_instance_bytes,
    )


__all__ = [
    "MAX_INSTANCE_BYTES", "MAX_INSTANCE_DEPTH", "OUTPUT_SCHEMA_MATCH_SECONDS",
    "ValidationTimeout", "validate_bounded", "validate_bounded_off_loop",
]
