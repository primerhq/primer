"""In-process tool source backed by a static registry.

Suitable for tools the application implements itself -- no remote server,
no transport, no auth. The registry maps each tool's wire name to a
:class:`matrix.model.chat.Tool` descriptor (the schema the LLM sees) and
an async handler that executes the call.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from matrix.int.toolset import ToolsetProvider
from matrix.model.chat import Tool, ToolCallResult
from matrix.model.except_ import ConfigError, UnsupportedContentError


logger = logging.getLogger(__name__)


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolCallResult]]
"""Async function that executes one tool call.

Receives the parsed argument dict, returns a :class:`ToolCallResult`.
The handler may raise; the provider does not catch -- exceptions
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
        for name, (tool, _) in self._registry.items():
            if tool.toolset_id != toolset_id:
                raise ConfigError(
                    f"Tool {name!r} declares toolset_id={tool.toolset_id!r} "
                    f"but provider toolset_id={toolset_id!r}"
                )

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
    ) -> ToolCallResult:
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
        return await handler(arguments)
