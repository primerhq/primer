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
user-regex executor. Every other jsonschema user in the process (the MCP argument check, the
harness overrides, the ask_user answer) sees no change. ``tests/common/test_bounded_jsonschema.py`` pins each keyword route, so
a jsonschema release that stops reading ``re`` through those modules fails the suite instead of silently unbounding them.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

import jsonschema
from jsonschema import _keywords, _legacy_keywords, _utils

from primer.common.user_regex import MatchBudget, compile_user_regex, run_off_loop

# The total time one validation may spend in its regex keywords.
OUTPUT_SCHEMA_MATCH_SECONDS = 1.0

_BUDGET: ContextVar[MatchBudget | None] = ContextVar("primer_jsonschema_regex_budget", default=None)


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
        # A schema is stored with its graph and runs again: a stored pattern (cached, verdict included).
        return budget.search(compile_user_regex(pattern, what="output_schema pattern", stored=True), string)


_PATCHED_MODULES = (_keywords, _legacy_keywords, _utils)


def _install() -> None:
    for module in _PATCHED_MODULES:
        current = getattr(module, "re", None)
        if current is re:
            module.re = _BoundedRe(re)
        elif not isinstance(current, _BoundedRe):
            raise RuntimeError(f"{module.__name__} no longer reads the re module; G6-4's bounded validation needs a new hook")


_install()


def validate_bounded(instance: Any, schema: dict[str, Any], *, seconds: float = OUTPUT_SCHEMA_MATCH_SECONDS) -> None:
    """``jsonschema.validate``, its regex keywords under one ``seconds`` budget.

    Raises ``jsonschema.ValidationError`` as before, and also
    :class:`~primer.common.user_regex.UserRegexError` (a schema pattern over
    the size caps) or :class:`~primer.common.user_regex.UserRegexTimeout`
    (the budget ran out).
    """
    token = _BUDGET.set(MatchBudget(seconds, what="output_schema pattern"))
    try:
        jsonschema.validate(instance=instance, schema=schema)
    finally:
        _BUDGET.reset(token)


async def validate_bounded_off_loop(
    instance: Any, schema: dict[str, Any], *, seconds: float | None = None,
) -> None:
    """:func:`validate_bounded` in a worker thread (the regex engine releases the GIL while it matches)."""
    bound = OUTPUT_SCHEMA_MATCH_SECONDS if seconds is None else seconds
    await run_off_loop(validate_bounded, instance, schema, seconds=bound)


__all__ = ["OUTPUT_SCHEMA_MATCH_SECONDS", "validate_bounded", "validate_bounded_off_loop"]
