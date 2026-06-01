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
ToolHandler = Callable[..., Awaitable[Union[ToolCallResult, Yielded]]]
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
        # Cache: which handlers yield (return-type annotation includes
        # ``Yielded``) and which require AgentSession (source reads
        # ``ctx.session_id``). Surfaced via :meth:`is_yielding` and
        # :meth:`requires_session` — the MCP server endpoint uses them
        # to filter the exposable tool set.
        self._yielding_names: set[str] = set()
        self._session_names: set[str] = set()
        for name, (tool, handler) in self._registry.items():
            if tool.toolset_id != toolset_id:
                raise ConfigError(
                    f"Tool {name!r} declares toolset_id={tool.toolset_id!r} "
                    f"but provider toolset_id={toolset_id!r}"
                )
            self._handler_takes_ctx[name] = _handler_takes_ctx(handler)
            if _handler_is_yielding(handler):
                self._yielding_names.add(name)
            if _handler_requires_session(handler):
                self._session_names.add(name)

    def is_yielding(self, tool_name: str) -> bool:
        """Return True iff ``tool_name``'s handler can yield.

        Derived at construction time from the handler's return-type
        annotation — a handler annotated to return :class:`Yielded`
        (alongside :class:`ToolCallResult`) is treated as yielding.
        Unknown names return ``False``.
        """
        return tool_name in self._yielding_names

    def requires_session(self, tool_name: str) -> bool:
        """Return True iff ``tool_name``'s handler needs an AgentSession.

        Derived at construction time from the handler's source code —
        a handler that reads ``ctx.session_id`` is treated as
        session-bound. Unknown names return ``False``.
        """
        return tool_name in self._session_names

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


def _handler_is_yielding(handler: ToolHandler) -> bool:
    """Introspect a handler's return-type annotation for ``Yielded``.

    A handler is treated as yielding when its return annotation
    mentions :class:`Yielded` (typically as part of a union, e.g.
    ``ToolCallResult | Yielded``). The check looks at both the
    resolved object identity (``Yielded`` is in the annotation's
    args) and a textual fallback so it still works on string-form
    annotations under ``from __future__ import annotations``.

    The MCP server endpoint uses this to filter out tools whose
    pause/resume semantics it cannot honour over a stateless
    request/response transport.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return False
    ann = sig.return_annotation
    if ann is inspect.Signature.empty:
        return False
    # Stringified annotation (PEP 563 / `from __future__ import annotations`).
    if isinstance(ann, str):
        return "Yielded" in ann
    # Direct reference.
    if ann is Yielded:
        return True
    # Union / parameterised type: look at args.
    args = getattr(ann, "__args__", ())
    if any(a is Yielded for a in args):
        return True
    # Final textual fallback — covers exotic typing constructs.
    return "Yielded" in repr(ann)


def _handler_requires_session(handler: ToolHandler) -> bool:
    """Introspect a handler's source for ``ctx.session_id`` reads.

    A handler that reads ``ctx.session_id`` is treated as requiring
    an :class:`AgentSession` — i.e. it only works inside an agent
    loop where the worker injects a live session id via
    :class:`ToolContext`. The MCP server endpoint uses this to
    exclude such tools from its exposable set.

    Falls back to ``False`` when the source isn't accessible (lambdas,
    C-backed callables, partials without ``__wrapped__``); those are
    rare in the internal registry and safe to expose by default.
    """
    try:
        src = inspect.getsource(handler)
    except (TypeError, OSError):
        return False
    return "ctx.session_id" in src
