"""Central tool-dispatch registry for the agent executors.

Composes any number of :class:`primer.int.ToolsetProvider`s with an
optional list of :class:`primer.workspace.WorkspaceTool`s and presents
a single ``list_tools`` / ``execute`` surface to the agent loop.

Routing rules:

* If ``call.name`` matches a key in the workspace-tool registry,
  dispatch via ``WorkspaceTool.execute`` (after building a
  :class:`primer.workspace.tool.ToolCallContext`).
* Otherwise, look up which :class:`ToolsetProvider` owns the tool
  (built lazily on the first ``list_tools`` call) and dispatch via
  ``ToolsetProvider.call``.

Errors:

* Unknown tool name -> :class:`UnsupportedContentError`.
* :class:`AuthRequiredError` from an MCP toolset -> propagates so the
  workspace executor can transition the session to WAITING /
  ``_ToolApprovalWaiting``.
* Any other :class:`PrimerError` -> caught and converted to a
  :class:`ToolResultPart` with ``error=True`` so the LLM can react
  rather than the executor crashing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from primer.agent.approval import (
    ApprovalContext,
    ApprovalResolver,
    evaluate_approval_gate,
)
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallPart, ToolCallResult, ToolResultPart
from primer.model.except_ import (
    AuthRequiredError,
    ConfigError,
    PrimerError,
    UnsupportedContentError,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics


if TYPE_CHECKING:
    from primer.workspace.session import AgentSession
    from primer.workspace.tool import WorkspaceTool


logger = logging.getLogger(__name__)

WORKSPACE_TOOLSET_ID = "workspace"

# Tool ids surfaced to the LLM are scoped as ``toolset_id<sep>bare_name`` so
# tools with colliding bare names across different toolsets stay
# distinguishable. The separator is ``__`` (two underscores) — chosen
# because most tool names are kebab-case or snake_case and rarely contain
# a double underscore in practice. ``ToolExecutionManager.list_tools``
# rejects bare ids that contain this separator.
_SCOPE_SEPARATOR = "__"


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
        approval_resolver: ApprovalResolver | None = None,
        provider_registry: object | None = None,
        tools: list[str] | None = None,
    ) -> None:
        self._toolsets: dict[str, ToolsetProvider] = dict(toolset_providers or {})
        self._workspace_tools: dict[str, "WorkspaceTool"] = dict(workspace_tools or {})
        self._workspace_session = workspace_session
        self._approval_resolver = approval_resolver
        self._provider_registry = provider_registry
        # The agent's scoped-tool surface. Filters list_tools() to just
        # the listed ids and execute() rejects calls for anything else.
        # ``None`` means "no filter" — useful for callers that aren't
        # agent-driven (graph executors using a parent manager directly,
        # tests that want to enumerate everything a toolset exposes).
        # An *empty list* still means "no filter" so unconfigured manager
        # paths don't accidentally lock themselves out; callers that
        # want zero tools should construct the manager with an empty
        # ``toolset_providers`` dict instead. Stored as a frozenset for
        # O(1) membership tests on the hot dispatch path.
        self._tools_allowlist: frozenset[str] | None = (
            frozenset(tools) if tools else None
        )
        # Built lazily on first list_tools / execute.
        # Scoped tool id (``toolset_id__bare_name``) -> (toolset_id, bare_name).
        # Tool ids surfaced to the LLM are scoped to avoid collisions across
        # toolsets; dispatch splits the scope back to the bare name before
        # calling the underlying provider.
        self._tool_to_toolset: dict[str, tuple[str, str]] = {}
        # Scoped workspace-tool id (``workspace__bare_name``) -> bare_name.
        # Separate map so dispatch can look up the WorkspaceTool from
        # ``_workspace_tools`` (still keyed by bare name).
        self._workspace_scoped: dict[str, str] = {}
        self._catalogue: list[Tool] | None = None
        self._index_lock = asyncio.Lock()

        if self._workspace_tools and self._workspace_session is None:
            raise ConfigError(
                "ToolExecutionManager: workspace_tools requires a "
                "workspace_session for dispatch context"
            )

    @property
    def toolset_providers(self) -> dict[str, ToolsetProvider]:
        """Snapshot of registered toolset providers, keyed by toolset id.

        Returned as a shallow copy so callers can iterate or reuse the
        mapping without mutating internal state. Used by composers
        (e.g. :class:`primer.graph.WorkspaceGraphExecutor`) that need
        to merge a base manager's providers with workspace-bound
        tools into a fresh manager.
        """
        return dict(self._toolsets)

    @classmethod
    def for_workspace(
        cls,
        *,
        toolset_providers: dict[str, ToolsetProvider],
        session: "AgentSession",
        approval_resolver: ApprovalResolver | None = None,
        provider_registry: object | None = None,
        tools: list[str] | None = None,
    ) -> "ToolExecutionManager":
        """Build a manager pre-wired for a :class:`WorkspaceAgentExecutor`.

        Pulls the workspace tool list off ``session.workspace_tools``
        and registers it; ``toolset_providers`` are passed through.
        ``tools`` (when supplied) is the agent's scoped tool surface —
        see :class:`primer.model.agent.Agent.tools`.
        """
        ws_tools = {t.id: t for t in session.workspace_tools}
        return cls(
            toolset_providers=toolset_providers,
            workspace_tools=ws_tools,
            workspace_session=session,
            approval_resolver=approval_resolver,
            provider_registry=provider_registry,
            tools=tools,
        )

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> list[Tool]:
        """Merged catalogue across every dispatcher.

        Emitted ``Tool.id`` values are scoped: ``toolset_id__bare_name``.
        Bare tool ids that already contain ``__`` raise :class:`ConfigError`
        because the double underscore is reserved as the scope separator.
        """
        async with self._index_lock:
            if self._catalogue is not None:
                return list(self._catalogue)
            catalogue: list[Tool] = []
            # Toolset-provider tools (each provider yields tools by their
            # bare name; we scope on the way out).
            for toolset_id, provider in self._toolsets.items():
                async for t in provider.list_tools(principal=principal):
                    if _SCOPE_SEPARATOR in t.id:
                        raise ConfigError(
                            f"tool {t.id!r} from toolset {toolset_id!r} "
                            f"contains {_SCOPE_SEPARATOR!r} which is "
                            "reserved as the scope separator"
                        )
                    scoped_id = f"{toolset_id}{_SCOPE_SEPARATOR}{t.id}"
                    # Routing table is built unconditionally so an
                    # allowlist hit still resolves; the visible
                    # catalogue is filtered below.
                    self._tool_to_toolset[scoped_id] = (toolset_id, t.id)
                    if self._tools_allowlist is not None and scoped_id not in self._tools_allowlist:
                        continue
                    scoped_tool = t.model_copy(update={"id": scoped_id})
                    catalogue.append(scoped_tool)
            # Workspace tools (always under the WORKSPACE_TOOLSET_ID scope).
            # Workspace tools are agent-implicit and bypass the
            # allowlist — they're injected by the workspace binding,
            # not picked from a registered toolset, so the operator
            # never has to enumerate them in the agent definition.
            for ws_tool in self._workspace_tools.values():
                if _SCOPE_SEPARATOR in ws_tool.id:
                    raise ConfigError(
                        f"workspace tool {ws_tool.id!r} contains "
                        f"{_SCOPE_SEPARATOR!r} which is reserved as the "
                        "scope separator"
                    )
                scoped_id = (
                    f"{WORKSPACE_TOOLSET_ID}{_SCOPE_SEPARATOR}{ws_tool.id}"
                )
                catalogue.append(
                    _workspace_tool_descriptor(ws_tool, scoped_id=scoped_id)
                )
                self._workspace_scoped[scoped_id] = ws_tool.id
            self._catalogue = catalogue
            return list(self._catalogue)

    async def execute(
        self,
        call: ToolCallPart,
        *,
        principal: str | None = None,
        bypass_approval: bool = False,
    ) -> ToolResultPart:
        """Dispatch one tool call; return a ToolResultPart for the LLM.

        ``call.name`` is the **scoped** id (``toolset_id__bare_name``) the
        catalog returned from :meth:`list_tools`. The dispatcher splits
        the scope and forwards the bare name to the underlying provider.

        If ``bypass_approval`` is True, the approval gate is skipped even
        when a resolver is configured. The worker's resume path sets this
        after the operator has approved the call.
        """
        import time as _time
        _tracer = _tracing.get_tracer("primer.tool")
        _t0 = _time.monotonic()
        with _tracer.start_as_current_span("tool.exec") as _span:
            _span.set_attribute("tool.name", call.name)
            try:
                result = await self._execute_inner(
                    call, principal=principal, bypass_approval=bypass_approval,
                )
                _metrics.tool_calls_total.labels(call.name, "ok").inc()
                return result
            except Exception as _exc:
                _span.record_exception(_exc)
                _metrics.tool_calls_total.labels(call.name, "fail").inc()
                raise
            finally:
                _metrics.tool_duration_seconds.labels(call.name).observe(
                    _time.monotonic() - _t0
                )

    async def _execute_inner(
        self,
        call: ToolCallPart,
        *,
        principal: str | None = None,
        bypass_approval: bool = False,
    ) -> ToolResultPart:
        """Internal dispatch — routes to workspace or toolset provider."""
        # Lazy-build the index if list_tools wasn't called yet.
        if self._catalogue is None:
            await self.list_tools(principal=principal)

        # Workspace tools first: they share the toolset-call routing table
        # via ``_workspace_scoped`` (scoped_id -> bare_name).
        ws_bare = self._workspace_scoped.get(call.name)
        if ws_bare is not None:
            # Resolve toolset/bare name for workspace tools for the gate.
            toolset_id = "workspace"
            bare_name = ws_bare
        else:
            entry = self._tool_to_toolset.get(call.name)
            if entry is None:
                raise UnsupportedContentError(
                    f"unknown tool {call.name!r}; not registered with any toolset "
                    "or workspace"
                )
            toolset_id, bare_name = entry
            # Enforce the agent's scoped-tool surface: a model trying
            # to invoke a tool the agent didn't register must be
            # refused. The toolset provider knows about the tool (so
            # it's in the routing table) but the agent's ``tools``
            # list didn't include it.
            if (
                self._tools_allowlist is not None
                and call.name not in self._tools_allowlist
            ):
                raise UnsupportedContentError(
                    f"tool {call.name!r} is provided by the toolset "
                    f"but is not in the agent's registered tool list"
                )

        # Approval gate — runs after routing resolution, before dispatch.
        if not bypass_approval and self._approval_resolver is not None:
            policy = await self._approval_resolver.find(
                toolset_id=toolset_id, tool_name=bare_name,
            )
            if policy is not None and policy.enabled:
                ctx = ApprovalContext(
                    tool_name=bare_name,
                    toolset_id=toolset_id,
                    arguments=call.arguments or {},
                    agent_id=getattr(self, "_agent_id", None),
                    session_id=getattr(self, "_session_id", None),
                    chat_id=getattr(self, "_chat_id", None),
                    requested_at=datetime.now(UTC),
                )
                verdict = await evaluate_approval_gate(
                    policy=policy,
                    context=ctx,
                    provider_registry=self._provider_registry,
                )
                if verdict.required:
                    session_or_chat = (
                        ctx.session_id or ctx.chat_id or "unknown"
                    )
                    raise YieldToWorker(
                        Yielded(
                            tool_name="_approval",
                            event_key=(
                                f"tool_approval:{session_or_chat}:{call.id}"
                            ),
                            timeout=policy.timeout_seconds,
                            resume_metadata={
                                "policy_id": policy.id,
                                "approval_type": policy.approval.type.value,
                                "gate_reason": verdict.reason,
                                "original_call": {
                                    "id": call.id,
                                    "name": call.name,
                                    "arguments": call.arguments or {},
                                },
                            },
                        ),
                        tool_call_id=call.id,
                    )

        if ws_bare is not None:
            return await self._dispatch_workspace(call, bare_name=ws_bare)

        return await self._dispatch_toolset(
            call,
            toolset_id=toolset_id,
            bare_name=bare_name,
            principal=principal,
        )

    # ---- Internals -------------------------------------------------------

    async def _dispatch_toolset(
        self,
        call: ToolCallPart,
        *,
        toolset_id: str,
        bare_name: str,
        principal: str | None,
    ) -> ToolResultPart:
        provider = self._toolsets[toolset_id]
        try:
            result = await provider.call(
                tool_name=bare_name,
                arguments=call.arguments,
                principal=principal,
            )
        except AuthRequiredError:
            raise
        except PrimerError as exc:
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

    async def _dispatch_workspace(
        self,
        call: ToolCallPart,
        *,
        bare_name: str,
    ) -> ToolResultPart:
        from primer.workspace.tool import ToolCallContext

        tool = self._workspace_tools[bare_name]
        sess = self._workspace_session
        assert sess is not None  # invariant from constructor

        try:
            args_model = tool.parameters().model_validate(call.arguments)
        except ValidationError as exc:
            # Genuine bad-args from the LLM: surface as a tool error so
            # the model can correct itself.
            logger.warning(
                "ToolExecutionManager: workspace-tool args validation failed",
                extra={"tool": call.name, "error": str(exc)},
            )
            return ToolResultPart(
                id=call.id,
                output=f"invalid arguments for {call.name}: {exc}",
                error=True,
            )
        except Exception:
            # Programming bug in tool.parameters() (e.g. NameError) -- log
            # at ERROR with traceback so the operator sees it; still
            # return a tool error so the agent doesn't loop forever.
            logger.exception(
                "ToolExecutionManager: tool.parameters() raised unexpectedly",
                extra={"tool": call.name},
            )
            return ToolResultPart(
                id=call.id,
                output=(
                    f"internal error preparing arguments for {call.name}; "
                    "see server logs"
                ),
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
        except PrimerError as exc:
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


def _workspace_tool_descriptor(
    ws_tool: "WorkspaceTool",
    *,
    scoped_id: str,
) -> Tool:
    """Convert a WorkspaceTool's ClassVars + parameters() into a Tool.

    The emitted ``Tool.id`` is the scoped form (``workspace__bare_name``)
    so the LLM sees a globally-unique id that won't collide with tools
    from other toolsets.
    """
    return Tool(
        id=scoped_id,
        description=ws_tool.description,
        toolset_id=WORKSPACE_TOOLSET_ID,
        args_schema=ws_tool.parameters().model_json_schema(),
    )


async def invoke_one(
    *,
    provider: ToolsetProvider,
    tool_name: str,
    arguments: dict,
    principal: str | None,
) -> ToolCallResult:
    """Invoke a single tool against ``provider`` — no approval, no workspace branch.

    Used by the MCP server endpoint to call tools directly without an
    agent context. Wraps the call in the same OTel span + Prometheus
    counters as :meth:`ToolExecutionManager.execute` so traces and
    metrics stay unified across agent-driven and MCP-driven invocations.

    Parameters
    ----------
    provider
        The :class:`ToolsetProvider` that owns ``tool_name``.
    tool_name
        Bare tool name (e.g. ``"uuid_v4"``), not a scoped id
        (``"misc__uuid_v4"``). Resolution from scoped id back to
        (provider, bare_name) is the caller's responsibility.
    arguments
        Pre-parsed argument dict forwarded to ``provider.call``.
    principal
        Caller identity, propagated to the provider for upstream
        auth-binding (MCP toolsets) and audit. ``None`` for unauthenticated
        callers; providers tolerate this where their backend permits.

    Caller is responsible for:

    * **Allowlist filtering** — e.g. the MCP exposure config's
      ``allowed_tools`` set. This helper does not consult any allowlist.
    * **Hard-deny enforcement** — e.g.
      :func:`primer.mcp.safety.is_exposable`. This helper does not check
      hard-deny lists.
    * **Approval-policy filtering** — MCP excludes approval-gated tools
      from its allowlist; this function does not invoke
      :class:`ApprovalResolver` or raise :class:`YieldToWorker`.

    Errors raised by ``provider.call`` propagate unchanged. The caller
    decides how to translate them into a protocol-level error response.
    """
    import time as _time

    _tracer = _tracing.get_tracer("primer.tool")
    _t0 = _time.monotonic()
    with _tracer.start_as_current_span("tool.exec") as _span:
        _span.set_attribute("tool.name", tool_name)
        _span.set_attribute("tool.via", "mcp")
        try:
            result = await provider.call(
                tool_name=tool_name,
                arguments=arguments,
                principal=principal,
                ctx=None,
            )
            _metrics.tool_calls_total.labels(tool_name, "ok").inc()
            return result
        except Exception as _exc:
            _span.record_exception(_exc)
            _metrics.tool_calls_total.labels(tool_name, "fail").inc()
            raise
        finally:
            _metrics.tool_duration_seconds.labels(tool_name).observe(
                _time.monotonic() - _t0
            )


__all__ = ["ToolExecutionManager", "WORKSPACE_TOOLSET_ID", "invoke_one"]
