"""MCP (/v1/mcp) mount + auth-gate wiring.

Extracted verbatim from :mod:`primer.api.app` as part of the app.py
decomposition. ``_start_mcp_mount`` builds the StreamableHTTP session
manager, mounts the auth-gated ASGI app at ``/v1/mcp``, and returns a
teardown coroutine. ``_make_mcp_auth_gate`` / ``_mcp_send_simple_response``
are its helpers. All are re-exported from ``primer.api.app``.
"""

from __future__ import annotations

from fastapi import FastAPI


async def _mcp_send_simple_response(send, status, body, extra_headers=None):
    """Emit a minimal JSON response from the MCP auth gate.

    The gate runs before the SDK's session manager touches the scope,
    so we cannot lean on FastAPI's exception machinery to render
    errors. A hand-rolled ASGI start+body pair keeps the surface
    tight and avoids accidentally inheriting any of the SDK's own
    response shaping.
    """
    import json
    body_bytes = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body_bytes)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body_bytes})


def _make_mcp_auth_gate(app: FastAPI):
    """Build the ASGI gate that fronts ``StreamableHTTPSessionManager``.

    Reads scope state populated by :class:`AuthMiddleware`
    (``state.user`` / ``state.principal`` / ``state.api_token``),
    rejects anonymous callers with 401 + ``WWW-Authenticate``, and
    enforces the ``mcp`` scope on bearer tokens with 403. Cookie
    sessions carry full user authority (``api_token is None``) and
    pass through without a scope check.

    On success the principal + api_token id are stashed in the
    module-level :class:`ContextVar`s :data:`current_principal` and
    :data:`current_api_token_id` (from :mod:`primer.mcp.server`) so
    the MCP request handlers see the authenticated caller. The
    ContextVars are reset in a ``finally`` so concurrent requests on
    the same worker do not leak identities.
    """
    from primer.mcp.server import (
        current_api_token_id as _current_api_token_id,
        current_principal as _current_principal,
    )
    from starlette.datastructures import State

    async def _mcp_auth_gate(scope, receive, send):
        if scope["type"] != "http":
            # WebSocket / lifespan scopes are not part of the MCP
            # surface; reject quietly so a stray probe doesn't crash.
            await _mcp_send_simple_response(
                send, 400, {"detail": {"code": "unsupported_scope"}},
            )
            return

        state = scope.get("state")
        # AuthMiddleware sets ``state`` to a Starlette ``State`` object;
        # support both that and a plain dict for defensive callers.
        if isinstance(state, State):
            user = getattr(state, "user", None)
            principal = getattr(state, "principal", None)
            api_token = getattr(state, "api_token", None)
        elif isinstance(state, dict):
            user = state.get("user")
            principal = state.get("principal")
            api_token = state.get("api_token")
        else:
            user = principal = api_token = None

        if user is None:
            await _mcp_send_simple_response(
                send, 401,
                {"detail": {"code": "auth_required"}},
                extra_headers=[
                    (b"www-authenticate", b'Bearer realm="primer"'),
                ],
            )
            return

        if api_token is not None and "mcp" not in api_token.scopes:
            await _mcp_send_simple_response(
                send, 403,
                {"detail": {"code": "scope_required", "scope": "mcp"}},
            )
            return

        session_manager = getattr(app.state, "mcp_session_manager", None)
        if session_manager is None:
            # Should never happen in a well-configured app — surface a
            # 503 rather than crash, so the failure is visible to ops.
            await _mcp_send_simple_response(
                send, 503,
                {"detail": {"code": "mcp_unavailable"}},
            )
            return

        principal_tok = _current_principal.set(principal)
        api_token_id_tok = _current_api_token_id.set(
            api_token.id if api_token is not None else None
        )
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _current_principal.reset(principal_tok)
            _current_api_token_id.reset(api_token_id_tok)

    return _mcp_auth_gate


async def _start_mcp_mount(
    app: FastAPI,
    *,
    storage_provider,
    provider_registry,
    approval_resolver=None,
):
    """Build the MCP session manager, mount /v1/mcp, return a teardown.

    The session manager's ``run()`` is an async context manager that
    spins an anyio task group; entered here, exited by the returned
    coroutine. Callers (the production lifespan + the test factory)
    are responsible for invoking the teardown during shutdown so the
    task group can drain.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from primer.mcp.exposure import ExposureDeps
    from primer.mcp.server import build_mcp_server

    def _deps_factory():
        return ExposureDeps(
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
        )

    mcp_server = build_mcp_server(_deps_factory)
    mcp_session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=False,
        stateless=False,
    )
    _ctx = mcp_session_manager.run()
    await _ctx.__aenter__()
    app.state.mcp_session_manager = mcp_session_manager
    # Mount once. The gate closure captures ``app`` so it can read
    # the session manager off ``app.state`` at request time; this
    # also keeps the mount survivable across hot-reloads in tests
    # that rebuild the manager without re-mounting.
    app.mount("/v1/mcp", _make_mcp_auth_gate(app))

    async def _teardown() -> None:
        try:
            await _ctx.__aexit__(None, None, None)
        finally:
            app.state.mcp_session_manager = None

    return _teardown
