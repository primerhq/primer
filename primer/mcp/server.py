"""Build the lowlevel MCP :class:`Server` with primer's handlers wired up.

Spec §3. Exposes :func:`build_mcp_server` — the factory used by the
ASGI mount (Phase 5) to construct one ``Server`` instance per process.
Handler bodies are intentionally thin: argument validation lives
inside the SDK's request-params model (``CallToolRequestParams``),
authorisation lives in :mod:`primer.mcp.dispatch`, and observability
lives in :func:`primer.agent.tool_manager.invoke_one`. This module's
job is to glue those three together and surface MCP-protocol-shaped
responses.

mcp>=2.0 dropped the ``@server.list_tools()`` / ``@server.call_tool()``
decorator API in favour of constructor-passed ``on_list_tools`` /
``on_call_tool`` callables taking ``(ctx: ServerRequestContext, params)``
and returning the SDK's own ``*Result`` model (see
``mcp.server.lowlevel.server``'s module docstring). ``ctx`` is unused
here — primer's own per-request identity threads through the
``current_*`` ContextVars below, set by the auth gate
(:func:`primer.api._app_mcp._make_mcp_auth_gate`) before the SDK's
session manager invokes these handlers, not through the SDK's request
context object.

ContextVars are populated by the auth gate before the SDK's request
runner enters the handler. They default to ``None`` for tests / dev
loops that exercise the handlers without an authenticated request.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from secrets import token_hex
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

import jsonschema
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    METHOD_NOT_FOUND,
    PaginatedRequestParams,
    TextContent,
    Tool as McpTool,
)

from primer.mcp.audit import log_invoke
from primer.mcp.exposure import ExposureDeps
from primer.mcp.meta_tools import (
    META_TOOL_DESCRIPTIONS,
    META_TOOL_SCHEMAS,
    call_tool_rejection,
    invoke_meta_tool,
    result_text,
)

if TYPE_CHECKING:
    from primer.model.principal import Principal


logger = logging.getLogger(__name__)


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
current_mcp_invocation: ContextVar[tuple[str, datetime, str] | None] = ContextVar(
    "primer_mcp_invocation", default=None,
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


async def _input_validation_error(
    deps: ExposureDeps, name: str, arguments: dict,
) -> str | None:
    """Check ``arguments`` against the exposed tool's ``inputSchema``.

    mcp 1.x validated tool arguments against ``inputSchema`` inside the
    SDK before dispatch; 2.x dropped that. Primer's pydantic layer still
    rejects malformed arguments further down (no correctness/security
    gap -- confirmed during the #226 migration review by both the lead
    and Dev-Backend), but the error SHAPE regressed: a caller got a deep
    tool-execution error instead of a protocol-level one, without the
    tool ever running. This restores that shape as an optional DX
    polish, re-checked here rather than cached: the schema is whatever
    ``_list`` would currently advertise for this tool, so a caller who
    just re-fetched ``tools/list`` sees a call rejected against the
    exact same shape.

    Returns a fixed marker string when validation fails, or ``None`` when
    the arguments are valid, the tool declares no schema, or the tool is
    not found here at all -- an unresolvable name is ``_call``'s job to
    reject as not-exposed (JSON-RPC method-not-found), not this
    function's, so a lookup miss is silently treated as "nothing to
    validate against" rather than surfaced as a validation failure.

    The marker is fixed on purpose: a jsonschema message would echo
    caller-supplied values back to the client, and the three meta
    schemas are fixed and fetchable from ``tools/list``.
    """
    del deps
    schema = META_TOOL_SCHEMAS.get(name)
    if schema is None:
        return None
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.exceptions.ValidationError:
        return "arguments"
    except jsonschema.exceptions.SchemaError:
        return None
    return None


def build_mcp_server(deps_factory: Callable[[], ExposureDeps]) -> Server:
    """Construct a :class:`Server` instance with primer's handlers attached.

    The ``deps_factory`` callable is invoked per-request so the handler
    body always sees a fresh :class:`ExposureDeps` (the storage +
    registry references it bundles are themselves long-lived, but
    binding them through a factory keeps the seam testable and avoids
    capturing the wrong references when the factory is closured over
    fields that change after process start).
    """

    async def _list(ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
        """Map primer's exposed tools onto the MCP ``Tool`` schema.

        No pagination: every exposed tool is returned in one page, same
        as before this handler took a ``params.cursor`` it doesn't read.
        """
        del ctx, params
        deps = deps_factory()
        out = [
            McpTool(
                name=name,
                description=META_TOOL_DESCRIPTIONS[name],
                inputSchema=META_TOOL_SCHEMAS[name],
            )
            for name in ("list_tools", "find_tools", "call_tool")
        ]
        return ListToolsResult(tools=out)

    async def _call(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        """Dispatch a single ``tools/call`` request for one of the three fixed meta-tools.

        Error mapping (M2 wire contract):

        * A name that is not one of ``list_tools`` / ``find_tools`` /
          ``call_tool`` → JSON-RPC ``method-not-found`` with the fixed
          message ``Tool is unavailable.`` -- the requested name is
          never echoed back.
        * Arguments that fail the meta schema → ``isError=True`` with
          the fixed marker ``Input validation error: arguments`` before
          dispatch (see :func:`_input_validation_error` below); a
          jsonschema message would echo caller-supplied values.
        * ``call_tool`` answers target refusals in-band through the
          meta envelope (``tool_unavailable`` / ``access_denied`` /
          ``invalid_arguments`` / ``tool_failed``), mapped to
          ``isError=True`` below; the denial reason is audit-only via
          :data:`primer.mcp.meta_tools.call_tool_rejection` and never
          reaches the wire.
        * Any other exception from the meta-tool dispatch → returned as
          an MCP-level ``isError=True`` result carrying the fixed text
          ``Tool dispatch failed.``; the exception detail is logged
          server-side (``logger.exception``) so the operator keeps it
          while the client sees one bounded message.
        * Success answers the meta payload's ``ok`` shape, serialized
          by :func:`primer.mcp.meta_tools.result_text`.
        """
        name = params.name
        arguments = params.arguments or {}
        # call_tool's audit/event identity is the inner requested
        # scoped_id, not the meta name, so audit and events keep the
        # per-target resolution the raw wire used to have.
        _inner_id = arguments.get("scoped_id")
        audit_target = (
            _inner_id
            if name == "call_tool" and isinstance(_inner_id, str)
            else None
        )
        audit_id = audit_target or name
        deps = deps_factory()
        principal = current_principal.get()
        api_token_id = current_api_token_id.get()
        actor = current_actor.get()
        api_token_scopes = current_api_token_scopes.get()
        t0 = time.monotonic()
        error_code: str | None = None
        ok = False
        request_id = getattr(ctx, "request_id", None)
        request = getattr(ctx, "request", None)
        session_id = None
        if request is not None:
            session_id = request.headers.get("mcp-session-id")
        invocation_token = current_mcp_invocation.set(
            (
                f"{session_id}\0{type(request_id).__name__}\0{request_id}",
                datetime.now(UTC),
                token_hex(16),
            )
            if session_id and request_id is not None
            else None
        )
        try:
            try:
                if name not in META_TOOL_SCHEMAS:
                    error_code = "not_exposed"
                    # Fixed on purpose: echoing the requested name would
                    # put unbounded client input back on the wire.
                    raise MCPError(
                        code=METHOD_NOT_FOUND,
                        message="Tool is unavailable.",
                    )
                bad_input = await _input_validation_error(deps, name, arguments)
                if bad_input is not None:
                    error_code = "invalid_arguments"
                    return CallToolResult(
                        isError=True,
                        content=[TextContent(
                            type="text",
                            text=(f"Input validation error: {bad_input}")[:1000],
                        )],
                    )
                payload = await invoke_meta_tool(name, arguments, deps)
            except MCPError:
                # Already protocol-shaped; let the SDK propagate. Keep a
                # more specific code assigned at the raise site (the
                # unknown-name refusal is not_exposed, not mcp_error).
                if error_code is None:
                    error_code = "mcp_error"
                raise
            except Exception:  # noqa: BLE001 -- surface as isError
                error_code = "dispatch_failed"
                # Fixed text on the wire; the exception detail stays
                # server-side where the operator can read it.
                logger.exception("mcp: dispatch raised")
                return CallToolResult(
                    isError=True,
                    content=[TextContent(
                        type="text",
                        text="Tool dispatch failed.",
                    )],
                )
            ok = bool(payload.get("ok", True))
            if not ok:
                error_code = str(payload.get("error", {}).get("code") or "tool_failed")
            return CallToolResult(
                isError=not ok,
                content=[
                    TextContent(type="text", text=result_text(payload)),
                ],
            )
        finally:
            current_mcp_invocation.reset(invocation_token)
            # The denial reason is audit-only: the wire keeps its fixed
            # indistinguishable error text, the operators get the cause.
            reason = call_tool_rejection.get() if audit_target else None
            log_invoke(
                principal=principal,
                api_token_id=api_token_id,
                scoped_id=audit_id,
                ok=ok,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                error_code=error_code,
                reason=reason,
            )
            from primer.events.recorder import actor_of, recorder_for

            await recorder_for(deps.storage_provider).emit(
                "mcp.tool_called",
                actor=actor_of(actor),
                payload={
                    "tool": name,
                    "target": audit_target,
                    "ok": ok,
                    "error_code": error_code,
                    "reason": reason,
                },
            )

    return Server("primer", on_list_tools=_list, on_call_tool=_call)


__all__ = [
    "build_mcp_server",
    "current_principal",
    "current_api_token_id",
    "current_actor",
    "current_mcp_invocation",
    "current_api_token_scopes",
]
