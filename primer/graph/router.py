"""Router primitives for the agent-graph runtime.

Two router kinds are supported (per spec):

* :class:`matrix.model.graph._JsonPathRouter` -- branch routing by
  matching dotted-path key/value pairs against a node's parsed
  structured output. The matching function is :func:`match_json_path`.
* :class:`matrix.model.graph._CallableRouter` -- looks up a Python
  callable in a :class:`RouterRegistry` and delegates routing to it.

The graph executor (G2/G3) consumes both via the same dispatch
surface. Callable routers may be sync or async; the registry
normalises both into an awaitable resolve call.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Union

from primer.model.except_ import ConfigError
from primer.model.graph import GraphContext, JsonPathBranch, NodeOutput


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON-path matching
# ---------------------------------------------------------------------------


def _resolve_path(parsed: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """Walk a dotted path through a nested dict.

    Returns ``(found, value)``: ``found=False`` means the path
    didn't resolve (intermediate non-dict or missing key) and
    ``value`` is :data:`None` in that case.
    """
    keys = dotted.split(".")
    current: Any = parsed
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def match_json_path(parsed: dict[str, Any], when: dict[str, Any]) -> bool:
    """Return True iff every ``(path, expected)`` pair is satisfied.

    Empty ``when`` matches anything (degenerate case useful as a
    catch-all branch).
    """
    for path, expected in when.items():
        found, actual = _resolve_path(parsed, path)
        if not found:
            return False
        if actual != expected:
            return False
    return True


def first_matching_branch(
    parsed: dict[str, Any],
    branches: list[JsonPathBranch],
) -> JsonPathBranch | None:
    """Return the first branch whose ``when`` matches, or :data:`None`."""
    for branch in branches:
        if match_json_path(parsed, branch.when):
            return branch
    return None


# ---------------------------------------------------------------------------
# RouterRegistry (callable routers)
# ---------------------------------------------------------------------------


_RouterFn = Union[
    Callable[[GraphContext, NodeOutput], str],
    Callable[[GraphContext, NodeOutput], Awaitable[str]],
]


class RouterRegistry:
    """In-process registry that resolves ``callable_id`` to a router fn.

    Mirrors :class:`matrix.agent.ToolExecutionManager`'s registration
    pattern so :class:`Graph` definitions stay JSON-serialisable
    while the actual routing logic lives in code. Constructed by the
    application; the executor accepts one optional registry at init
    time.

    Both sync and async router callables are accepted; :meth:`resolve`
    awaits the result uniformly.
    """

    def __init__(self) -> None:
        self._routers: dict[str, _RouterFn] = {}

    def register(self, callable_id: str, router: _RouterFn) -> None:
        """Register ``router`` under ``callable_id``.

        Re-registering an existing id raises :class:`ConfigError`
        (a typo at registration-time should fail loudly).
        """
        if not callable_id:
            raise ConfigError("RouterRegistry: callable_id must be non-empty")
        if callable_id in self._routers:
            raise ConfigError(
                f"RouterRegistry: callable_id {callable_id!r} already registered"
            )
        self._routers[callable_id] = router

    async def resolve(
        self,
        callable_id: str,
        *,
        context: GraphContext,
        source: NodeOutput,
    ) -> str:
        """Look up the router and call it; return the destination node id.

        Raises :class:`ConfigError` if ``callable_id`` is not
        registered. The router callable's return value is type-checked
        only by the caller (graph executor verifies the returned
        string maps to a real node).
        """
        if callable_id not in self._routers:
            raise ConfigError(
                f"RouterRegistry: callable_id {callable_id!r} not registered; "
                f"known: {sorted(self._routers)!r}"
            )
        router = self._routers[callable_id]
        result = router(context, source)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, str) or not result:
            raise ConfigError(
                f"RouterRegistry: router {callable_id!r} returned a "
                f"non-string or empty value: {result!r}"
            )
        return result

    def __contains__(self, callable_id: str) -> bool:
        return callable_id in self._routers


__all__ = [
    "RouterRegistry",
    "first_matching_branch",
    "match_json_path",
]
