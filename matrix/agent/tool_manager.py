"""Central tool-dispatch registry for the agent executors.

Composes any number of :class:`matrix.int.ToolsetProvider`s with an
optional list of :class:`matrix.workspace.WorkspaceTool`s and presents
a single ``list_tools`` / ``execute`` surface to the agent loop.

Routing rules:

* If ``call.name`` matches a key in the workspace-tool registry,
  dispatch via ``WorkspaceTool.execute`` (after building a
  :class:`matrix.workspace.tool.ToolCallContext`).
* Otherwise, look up which :class:`ToolsetProvider` owns the tool
  (built lazily on the first ``list_tools`` call) and dispatch via
  ``ToolsetProvider.call``.

Errors:

* Unknown tool name -> :class:`UnsupportedContentError`.
* :class:`AuthRequiredError` from an MCP toolset -> propagates so the
  workspace executor can transition the session to WAITING /
  ``_ToolApprovalWaiting``.
* Any other :class:`MatrixError` -> caught and converted to a
  :class:`ToolResultPart` with ``error=True`` so the LLM can react
  rather than the executor crashing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from matrix.int.toolset import ToolsetProvider
from matrix.model.chat import Tool, ToolCallPart, ToolResultPart
from matrix.model.except_ import (
    AuthRequiredError,
    ConfigError,
    MatrixError,
    UnsupportedContentError,
)


if TYPE_CHECKING:
    from matrix.workspace.session import AgentSession
    from matrix.workspace.tool import WorkspaceTool


logger = logging.getLogger(__name__)

WORKSPACE_TOOLSET_ID = "workspace"


class ToolExecutionManager:
    """Registry that owns every tool the agent can invoke.

    Two kinds of dispatch entries:

    * **Toolset entry** -- backed by a :class:`ToolsetProvider`.
      Dispatch calls
      ``provider.call(tool_name=..., arguments=..., principal=...)``.
      Used for system tools and MCP servers.
    * **Workspace entry** -- backed by a :class:`WorkspaceTool` +
      its parent :class:`AgentSession`. Dispatch builds a
      :class:`ToolCallContext` and calls ``tool.execute(args, ctx)``.
      Used only by :class:`WorkspaceAgentExecutor`.

    Workspace-tool dispatch additionally wraps the result in the
    truncation envelope: if the returned ``output`` exceeds the
    workspace's truncation thresholds and the tool didn't already
    truncate, the manager writes the full output to
    ``session.cache_output(...)`` and replaces the result's ``output``
    field with a preview-plus-hint string.
    """

    def __init__(
        self,
        *,
        toolset_providers: dict[str, ToolsetProvider] | None = None,
        workspace_tools: "dict[str, WorkspaceTool] | None" = None,
        workspace_session: "AgentSession | None" = None,
    ) -> None:
        self._toolsets: dict[str, ToolsetProvider] = dict(toolset_providers or {})
        self._workspace_tools: dict[str, "WorkspaceTool"] = dict(workspace_tools or {})
        self._workspace_session = workspace_session
        # Built lazily on first list_tools / execute.
        self._tool_to_toolset: dict[str, str] = {}
        self._catalogue: list[Tool] | None = None
        self._index_lock = asyncio.Lock()

        if self._workspace_tools and self._workspace_session is None:
            raise ConfigError(
                "ToolExecutionManager: workspace_tools requires a "
                "workspace_session for dispatch context"
            )

    @classmethod
    def for_workspace(
        cls,
        *,
        toolset_providers: dict[str, ToolsetProvider],
        session: "AgentSession",
    ) -> "ToolExecutionManager":
        """Build a manager pre-wired for a :class:`WorkspaceAgentExecutor`.

        Pulls the workspace tool list off ``session.workspace_tools``
        and registers it; ``toolset_providers`` are passed through.
        """
        ws_tools = {t.id: t for t in session.workspace_tools}
        return cls(
            toolset_providers=toolset_providers,
            workspace_tools=ws_tools,
            workspace_session=session,
        )

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> list[Tool]:
        """Merged catalogue across every dispatcher."""
        async with self._index_lock:
            if self._catalogue is not None:
                return list(self._catalogue)
            catalogue: list[Tool] = []
            # Toolset-provider tools.
            for toolset_id, provider in self._toolsets.items():
                async for t in provider.list_tools(principal=principal):
                    catalogue.append(t)
                    self._tool_to_toolset[t.id] = toolset_id
            # Workspace tools.
            for ws_tool in self._workspace_tools.values():
                catalogue.append(_workspace_tool_descriptor(ws_tool))
            self._catalogue = catalogue
            return list(self._catalogue)

    async def execute(
        self,
        call: ToolCallPart,
        *,
        principal: str | None = None,
    ) -> ToolResultPart:
        """Dispatch one tool call; return a ToolResultPart for the LLM."""
        # Lazy-build the index if list_tools wasn't called yet.
        if self._catalogue is None:
            await self.list_tools(principal=principal)

        if call.name in self._workspace_tools:
            return await self._dispatch_workspace(call)

        toolset_id = self._tool_to_toolset.get(call.name)
        if toolset_id is None:
            raise UnsupportedContentError(
                f"unknown tool {call.name!r}; not registered with any toolset "
                "or workspace"
            )
        return await self._dispatch_toolset(call, toolset_id, principal=principal)

    # ---- Internals -------------------------------------------------------

    async def _dispatch_toolset(
        self,
        call: ToolCallPart,
        toolset_id: str,
        *,
        principal: str | None,
    ) -> ToolResultPart:
        provider = self._toolsets[toolset_id]
        try:
            result = await provider.call(
                tool_name=call.name,
                arguments=call.arguments,
                principal=principal,
            )
        except AuthRequiredError:
            raise
        except MatrixError as exc:
            logger.warning(
                "ToolExecutionManager: toolset call failed; surfacing as "
                "tool result error",
                extra={
                    "tool": call.name,
                    "toolset": toolset_id,
                    "error": str(exc),
                },
            )
            return ToolResultPart(id=call.id, output=str(exc), error=True)
        return ToolResultPart(
            id=call.id,
            output=result.output,
            error=result.is_error,
        )

    async def _dispatch_workspace(self, call: ToolCallPart) -> ToolResultPart:
        from matrix.workspace.tool import ToolCallContext

        tool = self._workspace_tools[call.name]
        sess = self._workspace_session
        assert sess is not None  # invariant from constructor

        try:
            args_model = tool.parameters().model_validate(call.arguments)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ToolExecutionManager: workspace-tool args validation failed",
                extra={"tool": call.name, "error": str(exc)},
            )
            return ToolResultPart(
                id=call.id,
                output=f"invalid arguments for {call.name}: {exc}",
                error=True,
            )

        # ``model_construct`` skips Pydantic's ``is_instance_of`` check on the
        # ``session`` field. The check would otherwise prevent test doubles
        # from being passed through; the runtime call site has already
        # validated the session by handing us a live ``AgentSession``.
        ctx = ToolCallContext.model_construct(
            workspace_id=sess.workspace_id,
            session_id=sess.session_id,
            agent_id=sess.agent_id,
            call_id=call.id,
            abort=asyncio.Event(),
            session=sess,
            metadata_callback=None,
            ask_callback=None,
        )

        try:
            result = await tool.execute(args_model, ctx)
        except MatrixError as exc:
            logger.warning(
                "ToolExecutionManager: workspace tool failed; surfacing as "
                "tool result error",
                extra={"tool": call.name, "error": str(exc)},
            )
            return ToolResultPart(id=call.id, output=str(exc), error=True)

        # Apply the outer truncation envelope unless the tool already truncated.
        output = result.output
        if not result.truncated:
            output = await self._maybe_truncate_output(output, sess)

        return ToolResultPart(id=call.id, output=output, error=False)

    async def _maybe_truncate_output(
        self,
        output: str,
        session: "AgentSession",
    ) -> str:
        # Threshold mirrors opencode's defaults: 2000 lines, 50 KiB.
        max_bytes = 50 * 1024
        max_lines = 2000
        if len(output.encode("utf-8")) <= max_bytes and output.count("\n") <= max_lines:
            return output
        cache_path = await session.cache_output(output)
        # Show the head as preview, plus the standard hint.
        preview_lines = output.splitlines()[:50]
        preview = "\n".join(preview_lines)
        return (
            f"{preview}\n\n"
            "[the tool succeeded but the output was truncated]\n"
            f"Full output saved to: {cache_path}\n"
            "Use the read tool with offset/limit or grep to inspect "
            "specific sections; do NOT try to dump the file with cat."
        )


def _workspace_tool_descriptor(ws_tool: "WorkspaceTool") -> Tool:
    """Convert a WorkspaceTool's ClassVars + parameters() into a Tool."""
    return Tool(
        id=ws_tool.id,
        description=ws_tool.description,
        toolset_id=WORKSPACE_TOOLSET_ID,
        schema=ws_tool.parameters().model_json_schema(),
    )


__all__ = ["ToolExecutionManager", "WORKSPACE_TOOLSET_ID"]
