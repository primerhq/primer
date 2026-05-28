"""MCP-protocol :class:`ToolsetProvider` implementation.

Connects to an MCP server over stdio (subprocess; long-lived session
held for the provider's lifetime) or HTTP (streamable-http transport;
one short-lived session per call). Both transports share the
request-translation logic in this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from primer.common.mcp_errors import classify_mcp_exception
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import AuthRequiredError, ConfigError
from primer.model.provider import (
    HttpConfig,
    McpConfig,
    StdioConfig,
    TransportType,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.oauth.handler import MatrixOAuthHandler


# Canonical tool_name for MCP task parks in the resume registry.
# Each individual MCP task tool keeps its own user-facing name but
# yields under this synthetic name so a single resume hook can
# service all of them — the per-task metadata in resume_metadata
# carries the specifics (task_id, toolset_id, original tool name).
MCP_TASK_PARK_NAME = "__mcp_task__"


def is_mcp_task_tool(tool: mcp_types.Tool) -> bool:
    """Whether ``tool`` advertises task-style execution.

    Per the MCP 2025-11-25 ``tools/tasks`` extension, a tool's
    ``execution.taskSupport`` may be ``forbidden`` / ``optional`` /
    ``required``. The first two short-circuit to synchronous calls
    when the caller doesn't ask for task mode; ``required`` means
    the server only supports task-style invocation.
    """
    execution = getattr(tool, "execution", None)
    if execution is None:
        return False
    return execution.taskSupport in ("optional", "required")


logger = logging.getLogger(__name__)


class McpToolsetProvider(ToolsetProvider):
    """MCP-protocol tool source.

    Stdio servers run as a long-lived subprocess (lazy-started on first
    call, kept alive for the provider's lifetime, terminated by
    :meth:`aclose`). HTTP servers open a short-lived
    ``streamable_http`` session per call (cheap; the SDK has no
    long-lived equivalent for stateless HTTP MCP).

    The ``oauth`` constructor argument and :meth:`complete_oauth` method
    are accepted unconditionally so sub-project #10 can wire OAuth in
    without breaking callers built against this version. In this
    sub-project ``oauth`` MUST be ``None``; passing anything else is
    accepted (the field is stored) but unused.
    """

    def __init__(
        self,
        toolset_id: str,
        config: McpConfig,
        *,
        oauth: MatrixOAuthHandler | None = None,
        client_name: str = "primer",
        client_version: str = "0.0.1",
        allowed_stdio_commands: frozenset[str] | None = None,
    ) -> None:
        """``allowed_stdio_commands``: when set, ``stdio_cfg.command[0]``
        must match one of these strings exactly or :meth:`_ensure_stdio_session`
        raises :class:`ConfigError`. ``None`` (the default) means no
        allowlist is enforced -- the caller has decided either that all
        Toolset rows are operator-trusted or that the auth layer
        upstream gates who can create stdio toolsets at all. Operators
        running multi-tenant deployments SHOULD set an allowlist.
        """
        self._toolset_id = toolset_id
        self._config = config
        self._oauth = oauth
        self._client_name = client_name
        self._client_version = client_version
        self._allowed_stdio_commands = allowed_stdio_commands

        # Stdio long-lived state. Populated lazily on first use.
        self._stdio_lock = asyncio.Lock()
        self._stdio_session: ClientSession | None = None
        self._stdio_exit_stack: AsyncExitStack | None = None

        # Cache of MCP tool names that advertise task-style execution.
        # Populated as a side-effect of ``list_tools`` so subsequent
        # ``call`` invocations can route task tools to the yielding
        # path without re-listing.
        self._task_tools: set[str] = set()

    # ---------- public API ------------------------------------------------

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        async with self._open_session(principal=principal) as session:
            try:
                result = await session.list_tools()
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

        # Refresh the task-tools cache off the latest list. Server may
        # have added / removed task-style annotations between calls;
        # refreshing here keeps the call-time decision fresh without
        # an extra round trip.
        self._task_tools = {
            t.name for t in result.tools if is_mcp_task_tool(t)
        }
        for mcp_tool in result.tools:
            yield self._mcp_tool_to_matrix(mcp_tool)

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        # Task-style dispatch: when the tool advertises task support
        # AND the worker passed a ToolContext (so we can form a unique
        # event_key), invoke task mode and return Yielded. The provider
        # base class catches the Yielded sentinel and raises
        # YieldToWorker. Without ctx (legacy callers), fall through to
        # the synchronous path — a task-required tool will then fail
        # at the server, which is the right behaviour for the caller.
        if ctx is not None and tool_name in self._task_tools:
            from primer.model.yield_ import YieldToWorker  # local: cycle

            yielded = await self._call_task_mode(
                session_id=ctx.session_id,
                tool_call_id=ctx.tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                principal=principal,
            )
            raise YieldToWorker(yielded, tool_call_id=ctx.tool_call_id)

        async with self._open_session(principal=principal) as session:
            try:
                result = await session.call_tool(tool_name, arguments=arguments)
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

            return self._mcp_call_result_to_matrix(result)

    async def _call_task_mode(
        self,
        *,
        session_id: str | None,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None,
    ) -> Yielded:
        """Issue a task-style tools/call and return a Yielded sentinel.

        The MCP server replies with a Task reference (carried in
        ``CallToolResult.meta['task']``). We build the event_key off
        ``(toolset_id, task_id)`` so the bridge can match parks
        cross-process and so each MCP server has its own task
        namespace.
        """
        async with self._open_session(principal=principal) as session:
            try:
                result = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    meta={"task": {}},  # request task-style execution
                )
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

        # Extract the task id. Tolerate slight variations in where
        # the server returns it: result.meta or result._meta, with a
        # nested `task` object.
        meta = getattr(result, "_meta", None) or getattr(result, "meta", None) or {}
        task_obj = meta.get("task") if isinstance(meta, dict) else None
        if not task_obj or not isinstance(task_obj, dict):
            raise ConfigError(
                f"MCP toolset {self._toolset_id!r}: task-style call to "
                f"{tool_name!r} returned no task reference; the server "
                "may not implement the tasks extension correctly."
            )
        task_id = task_obj.get("taskId") or task_obj.get("task_id")
        if not task_id:
            raise ConfigError(
                f"MCP toolset {self._toolset_id!r}: task reference missing "
                f"taskId for {tool_name!r}"
            )

        # Pluck the server-suggested poll interval if present — the
        # bridge uses it to back off cheaper than its default.
        poll_interval = task_obj.get("pollInterval")

        return Yielded(
            tool_name=MCP_TASK_PARK_NAME,
            event_key=f"mcp_task:{self._toolset_id}:{task_id}",
            timeout=None,  # honour the global yield cap
            resume_metadata={
                "task_id": task_id,
                "toolset_id": self._toolset_id,
                "tool_name": tool_name,
                "poll_interval_ms": poll_interval,
                "tool_call_id": tool_call_id,
                "session_id": session_id,
            },
        )

    async def poll_task_status(
        self,
        task_id: str,
        *,
        principal: str | None = None,
    ) -> mcp_types.GetTaskResult:
        """Send a ``tasks/get`` request for ``task_id``. Used by the bridge."""
        request = mcp_types.ClientRequest(
            mcp_types.GetTaskRequest(
                params=mcp_types.GetTaskRequestParams(taskId=task_id),
            )
        )
        async with self._open_session(principal=principal) as session:
            try:
                return await session.send_request(
                    request, mcp_types.GetTaskResult,
                )
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

    async def fetch_task_result(
        self,
        task_id: str,
        *,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """Send a ``tasks/result`` request once the task is terminal."""
        request = mcp_types.ClientRequest(
            mcp_types.GetTaskPayloadRequest(
                params=mcp_types.GetTaskPayloadRequestParams(taskId=task_id),
            )
        )
        async with self._open_session(principal=principal) as session:
            try:
                result = await session.send_request(
                    request, mcp_types.GetTaskPayloadResult,
                )
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc
        # GetTaskPayloadResult is "additionalProperties: true" — the
        # body of the original CallToolResult lives alongside _meta.
        # model_dump preserves the extra fields the server attached.
        return result.model_dump(mode="json", exclude_none=False)

    async def cancel_task(
        self,
        task_id: str,
        *,
        principal: str | None = None,
    ) -> None:
        """Send a ``tasks/cancel`` request. Best-effort — caller swallows."""
        request = mcp_types.ClientRequest(
            mcp_types.CancelTaskRequest(
                params=mcp_types.CancelTaskRequestParams(taskId=task_id),
            )
        )
        async with self._open_session(principal=principal) as session:
            try:
                await session.send_request(
                    request, mcp_types.CancelTaskResult,
                )
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

    async def complete_oauth(self, *, code: str, state: str) -> None:
        """Finish an OAuth flow started by an earlier AuthRequiredError."""
        if self._oauth is None:
            raise ConfigError("OAuth not configured for this provider")
        await self._oauth.complete_oauth(code=code, state_id=state)

    async def aclose(self) -> None:
        """Tear down any long-lived stdio subprocess. No-op for HTTP."""
        async with self._stdio_lock:
            if self._stdio_exit_stack is not None:
                await self._stdio_exit_stack.aclose()
                self._stdio_exit_stack = None
                self._stdio_session = None

    # ---------- session management ---------------------------------------

    @asynccontextmanager
    async def _open_session(self, *, principal: str | None = None):
        """Yield a ready :class:`mcp.ClientSession` for one operation.

        Stdio: returns the single long-lived session (starting it on
        first call). HTTP: opens a fresh session per call. ``principal``
        is forwarded into the OAuth flow for HTTP transports; passing it
        as a parameter (rather than via an instance field) closes the
        race where two concurrent requests would clobber each other's
        principal.

        Subclasses may override this entirely (used in tests to inject a
        pre-built session over in-memory streams).
        """
        if self._config.transport == TransportType.STDIO:
            session = await self._ensure_stdio_session()
            yield session
            return

        if self._config.transport == TransportType.HTTP:
            assert isinstance(self._config.config, HttpConfig)
            http_cfg: HttpConfig = self._config.config

            from mcp.client.streamable_http import streamablehttp_client

            base_headers: dict[str, str] = (
                dict(http_cfg.headers) if http_cfg.headers else {}
            )
            if self._oauth is not None:
                # May raise AuthRequiredError -- intended bubble-out.
                auth_headers = await self._oauth.authorize(principal=principal)
                base_headers.update(auth_headers)

            stack = AsyncExitStack()
            try:
                streams = await stack.enter_async_context(
                    streamablehttp_client(
                        url=http_cfg.url,
                        headers=base_headers if base_headers else None,
                    )
                )
                # mcp >= 1.16 yields (read, write, get_session_id);
                # older releases yield (read, write).
                if len(streams) >= 2:
                    read, write = streams[0], streams[1]
                else:  # pragma: no cover - defensive for older mcp
                    raise ConfigError(
                        "streamablehttp_client returned an unexpected stream tuple"
                    )

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except (ConfigError, AuthRequiredError):
                await stack.aclose()
                raise
            except Exception as exc:
                await stack.aclose()
                raise classify_mcp_exception(exc) from exc

            try:
                yield session
            finally:
                await stack.aclose()
            return

        raise ConfigError(f"unknown transport {self._config.transport!r}")

    async def _ensure_stdio_session(self) -> ClientSession:
        async with self._stdio_lock:
            if self._stdio_session is not None:
                return self._stdio_session

            assert isinstance(self._config.config, StdioConfig)
            stdio_cfg: StdioConfig = self._config.config
            # Allowlist enforcement: when an operator-supplied allowlist
            # is in effect, refuse to launch any binary not on it.
            if self._allowed_stdio_commands is not None:
                if stdio_cfg.command[0] not in self._allowed_stdio_commands:
                    raise ConfigError(
                        f"toolset {self._toolset_id!r}: stdio command "
                        f"{stdio_cfg.command[0]!r} is not in the allowlist; "
                        "set `allowed_stdio_commands` on the registry / "
                        "AppConfig to permit it."
                    )
            params = StdioServerParameters(
                command=stdio_cfg.command[0],
                args=list(stdio_cfg.command[1:]),
                env=dict(stdio_cfg.env) if stdio_cfg.env else None,
            )

            stack = AsyncExitStack()
            try:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except FileNotFoundError as exc:
                await stack.aclose()
                raise ConfigError(
                    f"toolset {self._toolset_id!r}: stdio command "
                    f"{stdio_cfg.command[0]!r} could not be launched "
                    f"(executable not found on PATH)"
                ) from exc
            except PermissionError as exc:
                await stack.aclose()
                raise ConfigError(
                    f"toolset {self._toolset_id!r}: stdio command "
                    f"{stdio_cfg.command[0]!r} could not be launched "
                    f"(permission denied)"
                ) from exc
            except Exception:
                await stack.aclose()
                raise

            self._stdio_exit_stack = stack
            self._stdio_session = session
            logger.info(
                "Started stdio MCP subprocess for toolset %r", self._toolset_id
            )
            return session

    # ---------- translation ----------------------------------------------

    def _mcp_tool_to_matrix(self, t: mcp_types.Tool) -> Tool:
        return Tool(
            id=t.name,
            description=t.description or "",
            toolset_id=self._toolset_id,
            args_schema=t.inputSchema or {"type": "object", "properties": {}},
        )

    def _mcp_call_result_to_matrix(
        self,
        r: mcp_types.CallToolResult,
    ) -> ToolCallResult:
        text_chunks: list[str] = []
        non_text: list[dict[str, Any]] = []
        for item in r.content:
            if isinstance(item, mcp_types.TextContent):
                text_chunks.append(item.text)
                continue
            non_text.append(item.model_dump(mode="json"))

        if non_text:
            text_chunks.append(
                "```json\n" + json.dumps(non_text, indent=2) + "\n```"
            )

        extended: dict[str, Any] | None = None
        if non_text:
            extended = {
                "content": [item.model_dump(mode="json") for item in r.content],
            }

        return ToolCallResult(
            output="\n".join(text_chunks),
            is_error=bool(r.isError),
            extended=extended,
        )


# ===========================================================================
# MCP task resume hook
# ===========================================================================


def mcp_task_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
) -> ToolCallResult:
    """Resume hook for MCP-task yields.

    The bridge publishes ``{"result": <CallToolResult-shaped dict>}``
    on the bus when a task transitions to a terminal state. This hook
    translates that payload into a Primer :class:`ToolCallResult`. On
    timeout / cancel, the upstream MCP task is cancelled by the
    bridge / pre_cancel hook (best-effort) and the agent sees a
    structured marker so it can decide what to do next.
    """
    from primer.model.yield_ import YieldCancelled, YieldTimeout  # local: cycle

    task_id = yield_metadata.get("task_id", "")
    if isinstance(event_payload, YieldTimeout):
        return ToolCallResult(
            output=json.dumps(
                {
                    "timed_out": True,
                    "task_id": task_id,
                    "elapsed_seconds": event_payload.elapsed_seconds,
                }
            ),
            is_error=False,
        )
    if isinstance(event_payload, YieldCancelled):
        return ToolCallResult(
            output=json.dumps(
                {
                    "cancelled": True,
                    "task_id": task_id,
                    "reason": event_payload.reason,
                    "elapsed_seconds": event_payload.elapsed_seconds,
                }
            ),
            is_error=False,
        )
    # Real completion. The bridge wraps the CallToolResult payload
    # under a `result` key.
    result_blob = (
        event_payload.get("result")
        if isinstance(event_payload, dict)
        else None
    )
    if not isinstance(result_blob, dict):
        return ToolCallResult(
            output=json.dumps({"task_id": task_id, "result": result_blob}),
            is_error=False,
        )
    # If the upstream tool errored, propagate is_error so the LLM
    # surfaces it as a tool-error rather than success.
    is_error = bool(result_blob.get("isError", False))
    # Best-effort to extract text content for the LLM. The bridge may
    # send back a full CallToolResult-shaped dict (content array) or a
    # custom payload — handle both.
    content = result_blob.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                chunks.append(json.dumps(item))
        output = "\n".join(chunks) if chunks else json.dumps(result_blob)
    else:
        output = json.dumps(result_blob)
    return ToolCallResult(output=output, is_error=is_error)


# Register the resume hook at module import. The hook is keyed under
# MCP_TASK_PARK_NAME — the synthetic tool_name we stamp into Yielded
# for task parks. Per-MCP-server / per-tool routing happens via
# resume_metadata.toolset_id, not via the tool_name.
from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

register_resume_hook(MCP_TASK_PARK_NAME, mcp_task_resume)


__all__ = [
    "MCP_TASK_PARK_NAME",
    "McpToolsetProvider",
    "is_mcp_task_tool",
    "mcp_task_resume",
]
