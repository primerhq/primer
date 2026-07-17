"""Build the lowlevel MCP :class:`Server` with primer's handlers wired up.

Spec §3. Exposes :func:`build_mcp_server` — the factory used by the
ASGI mount (Phase 5) to construct one ``Server`` instance per process.
Handler bodies are intentionally thin: argument validation lives
inside the SDK's ``call_tool`` decorator, authorisation lives in
:mod:`primer.mcp.dispatch`, and observability lives in
:func:`primer.agent.tool_manager.invoke_one`. This module's job is to
glue those three together and surface MCP-protocol-shaped responses.

ContextVars are populated by the auth gate before the SDK's request
runner enters the handler. They default to ``None`` for tests / dev
loops that exercise the handlers without an authenticated request.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolResult,
    ErrorData,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    TextContent,
    Tool as McpTool,
)

from primer.mcp.audit import log_invoke
from primer.mcp.dispatch import NotExposed, invoke_exposed, list_exposed_tools
from primer.mcp.exposure import ExposureDeps
from primer.mcp.safety import tool_scoped_id

if TYPE_CHECKING:
    from primer.model.principal import Principal


# ---- Per-request context ---------------------------------------------------
#
# The auth gate (Phase 5) sets these before the StreamableHTTP session
# manager dispatches into the SDK; the handlers read them back here so
# the audit + provider-call chain knows who the caller is. The defaults
# are ``None`` so unit tests and the dev REPL can call the handlers
# directly without a fully-built auth context.
current_principal: ContextVar[str | None] = ContextVar(
    "primer_mcp_principal", default=None,
)
current_api_token_id: ContextVar[str | None] = ContextVar(
    "primer_mcp_api_token_id", default=None,
)
# The auth gate (Phase 5) also stashes the typed ``Principal`` (carrying
# ``.role``) here so the dispatch layer can enforce role gates without
# re-resolving identity. ``current_principal`` stays the bare-username
# string consumed by the provider-call + audit chain.
current_actor: ContextVar["Principal | None"] = ContextVar(
    "primer_mcp_actor", default=None,
)
# The auth gate also stashes the authenticated bearer token's scopes
# here so the dispatch layer can enforce the ``mcp`` scope when a tool
# call is dispatched (the scope floor moved from connect-time into
# dispatch so any authenticated caller may connect). The value is
# captured for the credential that opened the session. ``None`` is the
# sentinel for a cookie session (``api_token is None``) -- full user
# authority, no scope check. A concrete (possibly empty) list means a
# bearer token.
current_api_token_scopes: ContextVar["list[str] | None"] = ContextVar(
    "primer_mcp_api_token_scopes", default=None,
)


def build_mcp_server(deps_factory: Callable[[], ExposureDeps]) -> Server:
    """Construct a :class:`Server` instance with primer's handlers attached.

    The ``deps_factory`` callable is invoked per-request so the handler
    body always sees a fresh :class:`ExposureDeps` (the storage +
    registry references it bundles are themselves long-lived, but
    binding them through a factory keeps the seam testable and avoids
    capturing the wrong references when the factory is closured over
    fields that change after process start).
    """

    server: Server = Server("primer")

    @server.list_tools()
    async def _list() -> list[McpTool]:
        """Map primer's exposed tools onto the MCP ``Tool`` schema."""
        deps = deps_factory()
        exposed = await list_exposed_tools(deps)
        out: list[McpTool] = []
        for tool, _provider in exposed:
            out.append(
                McpTool(
                    name=tool_scoped_id(tool),
                    description=tool.description or "",
                    inputSchema=(
                        tool.args_schema
                        or {"type": "object", "properties": {}}
                    ),
                )
            )
        return out

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Dispatch a single ``tools/call`` request.

        Error mapping:

        * :class:`NotExposed` → JSON-RPC ``method-not-found`` so the
          client treats the tool name as unknown. Operators see the
          rich ``reason`` only in the audit log.
        * Any other exception from the dispatcher / provider → returned
          as an MCP-level ``isError=True`` result, which is the SDK's
          convention for tool-execution failures the client should
          surface to the user without aborting the session.
        """
        deps = deps_factory()
        principal = current_principal.get()
        api_token_id = current_api_token_id.get()
        actor = current_actor.get()
        api_token_scopes = current_api_token_scopes.get()
        t0 = time.monotonic()
        error_code: str | None = None
        ok = False
        try:
            try:
                result = await invoke_exposed(
                    scoped_id=name,
                    arguments=arguments or {},
                    principal=principal,
                    actor=actor,
                    api_token_scopes=api_token_scopes,
                    deps=deps,
                )
            except NotExposed as exc:
                error_code = "not_exposed"
                # Spec §13: surfaces as method-not-found to the client.
                # The exc.reason is included for ops-only triage via the
                # audit log; the wire-level message stays generic so we
                # do not leak which tools exist behind the allowlist.
                raise McpError(
                    ErrorData(
                        code=METHOD_NOT_FOUND,
                        message=f"tool {name!r} not exposed",
                    )
                ) from exc
            except McpError:
                # Already protocol-shaped; let the SDK propagate.
                error_code = "mcp_error"
                raise
            except Exception as exc:  # noqa: BLE001 -- surface as isError
                error_code = "dispatch_failed"
                return CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text=str(exc))],
                )
            ok = not result.is_error
            return CallToolResult(
                isError=result.is_error,
                content=[
                    TextContent(type="text", text=result.output or ""),
                ],
            )
        finally:
            log_invoke(
                principal=principal,
                api_token_id=api_token_id,
                scoped_id=name,
                ok=ok,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                error_code=error_code,
            )

    # Silence unused-import warnings on INVALID_PARAMS — kept in scope
    # so future handlers (e.g. session-init validation) can reuse the
    # same import block without re-touching this file.
    _ = INVALID_PARAMS

    return server


__all__ = [
    "build_mcp_server",
    "current_principal",
    "current_api_token_id",
    "current_actor",
    "current_api_token_scopes",
]
