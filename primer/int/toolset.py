"""Abstract base class for toolset providers.

A *toolset* is the primer term for a source of tools the application
can offer to LLMs. Implementations bind to a configured provider at
construction time and expose two operations:

* :meth:`ToolsetProvider.list_tools` -- enumerate the tools this provider
  exposes (returned as :class:`primer.model.chat.Tool` descriptors so
  they can be passed straight to the LLM adapter's ``tools`` parameter).
* :meth:`ToolsetProvider.call` -- invoke one tool by name with an
  argument dict, returning a :class:`primer.model.chat.ToolCallResult`.

The optional ``principal`` parameter on both methods is the
caller-supplied identity of the end user on whose behalf the operation
runs. Providers that hold per-user state (an OAuth token cache, for
instance) use it as the cache key; providers that don't (the in-process
internal registry, MCP without OAuth) ignore it. ``None`` is permitted
and is treated as an anonymous principal by providers that distinguish.

Adapters that need OAuth consent before they can answer raise
:class:`primer.model.except_.AuthRequiredError` (added in sub-project
#10). Callers MUST handle that case explicitly so the URL reaches the
end user before any generic ``except PrimerError`` swallows it.

See the design spec at
``docs/superpowers/specs/2026-04-26-toolset-provider-oauth-design.md``
for the full contract and the per-provider mapping rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from primer.model.chat import Tool, ToolCallResult

if TYPE_CHECKING:
    from primer.model.yield_ import ToolContext


class ToolsetProvider(ABC):
    """Provider-agnostic interface to a source of tools.

    Sibling of :class:`primer.int.LLM` and :class:`primer.int.Embedder`.
    Subclasses are bound to one configured toolset; ``list_tools`` /
    ``call`` are the only operations.
    """

    @abstractmethod
    def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        """Yield every tool this provider exposes.

        Concrete implementations are async generators
        (``async def list_tools(...): ... yield tool``).

        Parameters
        ----------
        principal
            Caller-supplied end-user identity. Providers that scope
            state per user (OAuth token cache) use it as the cache key.
            Providers that don't ignore it. ``None`` is the anonymous
            principal.

        Returns
        -------
        AsyncIterator[Tool]
            Async iterator of :class:`primer.model.chat.Tool` descriptors.
            Each descriptor's :attr:`Tool.toolset_id` MUST match the
            provider's configured toolset id so callers can route
            tool-call invocations back to the right provider.

        Raises
        ------
        primer.model.except_.AuthRequiredError
            OAuth consent required before this provider can answer.
        primer.model.except_.ProviderError
        primer.model.except_.NetworkError
            Standard upstream / transport failures.
        """

    @abstractmethod
    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: "ToolContext | None" = None,
    ) -> ToolCallResult:
        """Invoke a tool by name and return its result.

        Parameters
        ----------
        tool_name
            Wire name of the tool -- matches one of the names returned
            by :meth:`list_tools` (i.e. each :class:`Tool`'s ``id``).
        arguments
            Pre-parsed argument object the tool was invoked with. Caller
            is responsible for parsing the model's ``ToolCallPart``
            arguments before dispatch.
        principal
            See :meth:`list_tools`.
        ctx
            Optional :class:`primer.model.yield_.ToolContext` injected
            for yielding tools (carries ``tool_call_id``,
            ``session_id``, ``workspace_id``, and on resume
            ``parked_at``). Providers ignore it for non-yielding
            handlers; yielding handlers use it to form unique event
            keys. ``None`` is permitted — providers MUST tolerate
            ``ctx=None`` and only inject when both the handler
            declares it and the caller supplied one.

        Returns
        -------
        ToolCallResult
            Result of executing the tool. ``is_error=True`` indicates a
            tool-level failure that should be reported back to the model
            on the next turn (rather than aborted).

        Raises
        ------
        primer.model.except_.UnsupportedContentError
            ``tool_name`` is not exposed by this provider.
        primer.model.except_.AuthRequiredError
            OAuth consent required before this provider can answer.
        primer.model.except_.ProviderError
        primer.model.except_.NetworkError
            Standard upstream / transport failures.
        primer.model.yield_.YieldToWorker
            The tool yielded — its turn is paused until the named
            event fires. Caller (typically the agent's tool manager)
            propagates this up to the worker pool which writes the
            parked-state blob and releases the lease.
        """

    def is_yielding(self, tool_name: str) -> bool:
        """Return True if this tool yields control (raises :class:`YieldToWorker`).

        Used by the MCP server endpoint to filter out tools it cannot
        round-trip — MCP has no park/resume protocol in v1, so yielding
        tools cannot be exposed.

        Default: ``False``. Providers that host yielding handlers
        override this and return ``True`` for the relevant names. The
        ``tool_name`` is the bare wire name (matches :attr:`Tool.id`),
        NOT the ``toolset_id__name`` scoped form.
        """
        del tool_name
        return False

    def requires_session(self, tool_name: str) -> bool:
        """Return True if this tool requires :class:`AgentSession` context.

        Used by the MCP server endpoint to exclude workspace-style
        tools that only make sense inside a running agent loop (they
        depend on ``ctx.session_id`` / ``ctx.workspace_id`` injected
        by the worker).

        Default: ``False``. Providers whose handlers read
        ``ctx.session_id`` override this and return ``True`` for the
        relevant names.
        """
        del tool_name
        return False

    def requires_workspace(self, tool_name: str) -> bool:
        """Return True if this tool requires a live workspace.

        Such tools read ``ctx.workspace_id`` for file I/O and only make
        sense inside a workspace session. Consulted by the chat
        suppression choke point (:meth:`ToolExecutionManager.list_tools`)
        to drop them from chat tool context, and by the MCP exposure
        guard (:func:`primer.mcp.safety.is_exposable`) to keep them off
        the stateless MCP surface.

        Default: ``False``. Providers whose handlers read
        ``ctx.workspace_id`` override this and return ``True`` for the
        relevant names.
        """
        del tool_name
        return False

    def required_role(self, tool_name: str) -> str:
        """RBAC role required to invoke ``tool_name`` over MCP.

        Consulted by :func:`primer.mcp.dispatch.invoke_exposed` for
        *every* exposed tool (not just reserved ``system`` mutations) so
        the declared role is the single source of truth for the gate.

        Default: ``"admin"`` (fail-closed) for providers that don't
        maintain their own per-tool role registry -- e.g. an
        externally-configured :class:`primer.toolset.mcp.McpToolsetProvider`
        proxying a third-party MCP server, which has no ``required_role``
        concept of its own. :class:`primer.toolset.internal.InternalToolsetProvider`
        overrides this to read the role declared on the :class:`Tool`.
        """
        del tool_name
        return "admin"

    async def aclose(self) -> None:
        """Release backend resources held by this provider. Default no-op."""
        return
