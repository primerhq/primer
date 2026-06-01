"""Audit logging for MCP-driven tool invocations.

Spec §12. One log line per ``tools/call`` request, regardless of
outcome. Operators feed the ``primer.mcp.audit`` logger to wherever
their compliance pipeline expects MCP usage records.

Why a dedicated logger?

The agent-driven path already emits OTel spans + Prometheus counters
under :mod:`primer.agent.tool_manager`; the MCP path inherits those
through :func:`primer.agent.tool_manager.invoke_one`. The extra log
line here is the operator-visible audit trail — who called what, was
it allowed, how long did it take — and lives on its own logger so
operators can route / level it independently of the rest of
``primer.mcp``.
"""

from __future__ import annotations

import logging


logger = logging.getLogger("primer.mcp.audit")


def log_invoke(
    *,
    principal: str | None,
    api_token_id: str | None,
    scoped_id: str,
    ok: bool,
    duration_ms: float,
    error_code: str | None = None,
) -> None:
    """Emit one audit record for an MCP ``tools/call`` request.

    Parameters
    ----------
    principal
        Caller identity surfaced by the auth gate (cookie subject or
        bearer-token owner). ``None`` for unauthenticated paths —
        these only arise during tests; the auth gate (Phase 5) refuses
        anonymous requests in production.
    api_token_id
        Identifier of the ``ApiToken`` row that authorised the call,
        ``None`` when the caller used a cookie session.
    scoped_id
        The wire-level ``toolset_id__tool_id`` the client asked for.
        Recorded as-given so operators can audit attempts at unknown
        or denied tool ids.
    ok
        ``True`` iff the provider returned a non-error ``ToolCallResult``.
        Failures from the dispatcher (``NotExposed`` etc.) emit
        ``ok=False`` with ``error_code`` set.
    duration_ms
        Wall-clock time the handler spent, measured by the caller.
    error_code
        Stable code from Spec §13 (``"not_exposed"`` /
        ``"dispatch_failed"``) when ``ok`` is ``False``, ``None``
        otherwise.
    """
    logger.info(
        "mcp.invoke",
        extra={
            "principal": principal,
            "api_token_id": api_token_id,
            "scoped_id": scoped_id,
            "ok": ok,
            "duration_ms": round(duration_ms, 2),
            "error_code": error_code,
        },
    )


__all__ = ["log_invoke"]
