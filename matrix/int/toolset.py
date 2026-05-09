"""Abstract base class for toolset providers.

A *toolset* is the matrix term for a source of tools the application
can offer to LLMs. Implementations bind to a configured provider at
construction time and expose two operations:

* :meth:`ToolsetProvider.list_tools` -- enumerate the tools this provider
  exposes (returned as :class:`matrix.model.chat.Tool` descriptors so
  they can be passed straight to the LLM adapter's ``tools`` parameter).
* :meth:`ToolsetProvider.call` -- invoke one tool by name with an
  argument dict, returning a :class:`matrix.model.chat.ToolCallResult`.

The optional ``principal`` parameter on both methods is the
caller-supplied identity of the end user on whose behalf the operation
runs. Providers that hold per-user state (an OAuth token cache, for
instance) use it as the cache key; providers that don't (the in-process
internal registry, MCP without OAuth) ignore it. ``None`` is permitted
and is treated as an anonymous principal by providers that distinguish.

Adapters that need OAuth consent before they can answer raise
:class:`matrix.model.except_.AuthRequiredError` (added in sub-project
#10). Callers MUST handle that case explicitly so the URL reaches the
end user before any generic ``except MatrixError`` swallows it.

See the design spec at
``docs/superpowers/specs/2026-04-26-toolset-provider-oauth-design.md``
for the full contract and the per-provider mapping rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from matrix.model.chat import Tool, ToolCallResult


class ToolsetProvider(ABC):
    """Provider-agnostic interface to a source of tools.

    Sibling of :class:`matrix.int.LLM` and :class:`matrix.int.Embedder`.
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
            Async iterator of :class:`matrix.model.chat.Tool` descriptors.
            Each descriptor's :attr:`Tool.toolset_id` MUST match the
            provider's configured toolset id so callers can route
            tool-call invocations back to the right provider.

        Raises
        ------
        matrix.model.except_.AuthRequiredError
            OAuth consent required before this provider can answer.
        matrix.model.except_.ProviderError
        matrix.model.except_.NetworkError
            Standard upstream / transport failures.
        """

    @abstractmethod
    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
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

        Returns
        -------
        ToolCallResult
            Result of executing the tool. ``is_error=True`` indicates a
            tool-level failure that should be reported back to the model
            on the next turn (rather than aborted).

        Raises
        ------
        matrix.model.except_.UnsupportedContentError
            ``tool_name`` is not exposed by this provider.
        matrix.model.except_.AuthRequiredError
            OAuth consent required before this provider can answer.
        matrix.model.except_.ProviderError
        matrix.model.except_.NetworkError
            Standard upstream / transport failures.
        """

    async def aclose(self) -> None:
        """Release backend resources held by this provider. Default no-op."""
        return
