"""In-process tool source backed by a static registry.

Suitable for tools the application implements itself -- no remote server,
no transport, no auth. The registry maps each tool's wire name to a
:class:`primer.model.chat.Tool` descriptor (the schema the LLM sees) and
an async handler that executes the call.

Handlers may yield. A handler that returns a :class:`Yielded` instance
instead of a :class:`ToolCallResult` signals that the calling agent's
turn should be parked until the named event fires. The provider
recognises the sentinel, stamps the registered tool name into it (so
the resume path can look it up from the parked-state blob alone), and
re-raises as :class:`YieldToWorker` for the worker pool to catch.

Handlers that need their own ``tool_call_id`` / ``session_id`` /
``workspace_id`` (every yielding handler does) declare a keyword
argument ``ctx: ToolContext``. The provider inspects each handler's
signature at registration time and only injects the context when the
parameter is declared, so legacy handlers (no ``ctx``) keep working
unchanged.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Union

from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import ConfigError, UnsupportedContentError
from primer.model.yield_ import ToolContext, YieldToWorker, Yielded


logger = logging.getLogger(__name__)


# Two handler shapes coexist:
#   * Legacy: ``(arguments)`` only — returns ToolCallResult.
#   * Yielding: ``(arguments, ctx=ToolContext)`` — returns
#     ToolCallResult OR Yielded.
# The provider supports both; introspection on the handler's signature
# picks the right call shape at dispatch time.
ToolHandler = Callable[..., Awaitable[ToolCallResult | Yielded]]
"""Async function that executes one tool call.

Receives the parsed argument dict and (optionally) a
:class:`ToolContext`. Returns a :class:`ToolCallResult` for normal
tools or a :class:`Yielded` sentinel for yielding tools (the
provider re-raises the sentinel as :class:`YieldToWorker`).

The handler may raise; the provider does not catch — exceptions
propagate to the caller of :meth:`InternalToolsetProvider.call`.
"""


class InternalToolsetProvider(ToolsetProvider):
    """In-process :class:`ToolsetProvider` over a static registry."""

    def __init__(
        self,
        toolset_id: str,
        registry: dict[str, tuple[Tool, ToolHandler]],
    ) -> None:
        self._toolset_id = toolset_id
        # Defensive copy -- caller mutations after construction must not
        # alter the provider's view.
        self._registry: dict[str, tuple[Tool, ToolHandler]] = dict(registry)
        # Cache: does each handler accept a ToolContext via `ctx`?
        # Computed once at registration time so dispatch doesn't pay
        # the introspection cost per call. A True entry tells
        # dispatch to inject the context kwarg.
        self._handler_takes_ctx: dict[str, bool] = {}
        # Cache: which tools yield (park the turn) and which require an
        # AgentSession. Both are declared explicitly at the ``make_tool``
        # call site via the ``yields`` / ``requires_session`` flags (which
        # replaced the old handler source/annotation introspection - see
        # :func:`primer.toolset._describe.make_tool`). Surfaced via
        # :meth:`is_yielding` and :meth:`requires_session`; the MCP server
        # endpoint uses them to filter the exposable tool set.
        self._yielding_names: set[str] = set()
        self._session_names: set[str] = set()
        self._required_roles: dict[str, str] = {}
        for name, (tool, handler) in self._registry.items():
            if tool.toolset_id != toolset_id:
                raise ConfigError(
                    f"Tool {name!r} declares toolset_id={tool.toolset_id!r} "
                    f"but provider toolset_id={toolset_id!r}"
                )
            self._handler_takes_ctx[name] = _handler_takes_ctx(handler)
            if tool.yields:
                self._yielding_names.add(name)
            if tool.requires_session:
                self._session_names.add(name)
            if tool.required_role is not None:
                self._required_roles[name] = tool.required_role

    def is_yielding(self, tool_name: str) -> bool:
        """Return True iff ``tool_name`` can yield (park the agent turn).

        Read at construction time from the tool's explicit ``yields``
        flag (declared at the ``make_tool`` call site). Unknown names
        return ``False``.
        """
        return tool_name in self._yielding_names

    def requires_session(self, tool_name: str) -> bool:
        """Return True iff ``tool_name`` needs a live AgentSession.

        Read at construction time from the tool's explicit
        ``requires_session`` flag (declared at the ``make_tool`` call
        site). Unknown names return ``False``.
        """
        return tool_name in self._session_names

    def required_role(self, tool_name: str) -> str:
        """RBAC role required to invoke ``tool_name`` over MCP.

        Read at construction from the tool's explicit ``required_role``
        flag. Fail-closed: an undeclared or unknown tool returns
        ``"admin"`` so a new reserved tool can never be a silent hole.
        """
        return self._required_roles.get(tool_name, "admin")

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        del principal  # explicitly ignored -- internal registry has no per-user state
        for _, (tool, _) in self._registry.items():
            yield tool

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        """Dispatch ``tool_name`` with ``arguments``.

        Yielding handlers see the optional ``ctx`` if their signature
        declares it; non-yielding handlers are called with arguments
        only. If a handler returns a :class:`Yielded` sentinel, the
        provider stamps the registered tool name onto it (so the
        resume path doesn't need to walk the LLM message history)
        and raises :class:`YieldToWorker` for the worker pool to
        catch.
        """
        del principal  # explicitly ignored
        entry = self._registry.get(tool_name)
        if entry is None:
            raise UnsupportedContentError(
                f"tool {tool_name!r} not in toolset {self._toolset_id!r}"
            )
        _, handler = entry
        logger.debug(
            "InternalToolsetProvider dispatching %r in toolset %r",
            tool_name,
            self._toolset_id,
        )
        if self._handler_takes_ctx.get(tool_name) and ctx is not None:
            result = await handler(arguments, ctx=ctx)
        else:
            result = await handler(arguments)

        if isinstance(result, Yielded):
            # Tools don't set tool_name themselves — stamp the
            # registered name so the resume path can look up the
            # handler from the parked-state blob alone.
            stamped = Yielded(
                tool_name=tool_name,
                event_key=result.event_key,
                timeout=result.timeout,
                resume_metadata=result.resume_metadata,
            )
            # tool_call_id is required to form unique event keys;
            # if the handler didn't have ctx (legacy signature)
            # there's nothing to yield with — that's a programming
            # bug, not a runtime condition, so fail loud.
            if ctx is None:
                raise ConfigError(
                    f"tool {tool_name!r} returned Yielded but its "
                    f"handler doesn't accept ToolContext — yielding "
                    f"handlers must declare `ctx: ToolContext`"
                )
            raise YieldToWorker(stamped, tool_call_id=ctx.tool_call_id)
        return result


def _handler_takes_ctx(handler: ToolHandler) -> bool:
    """Introspect a handler to see if it declares ``ctx``.

    Returns True if the handler's signature has a parameter named
    ``ctx`` (any kind: keyword-only, positional-or-keyword, etc.).
    Used at registration time to decide whether to inject the
    :class:`ToolContext` at call time.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        # Some C-backed callables don't expose a signature — treat
        # them as legacy (no ctx).
        return False
    return "ctx" in sig.parameters
