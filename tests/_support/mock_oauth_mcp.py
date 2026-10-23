"""A hermetic OAuth authorization server and MCP resource server in one ASGI app, for tests (plan 07, Task 2).

One origin (``base_url``) serves both roles, like a hosted MCP product does:

* the protected MCP endpoint ``/mcp`` (the ``echo`` and ``bump`` tools of ``tests/_support/mcp/_tools.py``) behind a bearer check that answers a
  missing, unknown or expired token with ``401`` and ``WWW-Authenticate: Bearer resource_metadata="...", scope="mcp"``;
* the RFC 9728 protected-resource metadata and the RFC 8414 authorization-server metadata, both PATH-AWARE (the root documents answer 404, so a
  client that skips the path-aware lookup finds nothing);
* the authorization server under ``/as``: RFC 7591 registration, the authorization endpoint (auto-consent), the token endpoint (authorization
  code with PKCE S256, rotating refresh tokens, client credentials) and RFC 7009 revocation.

It speaks the specs strictly on purpose. Anything a real server would refuse (a plain PKCE method, a missing ``state``, a ``resource`` that is not the
MCP URL, a second use of a code or of a rotated-away refresh token, a body that is not a form) is refused here with a precise ``error_description``, so
a client bug fails at its cause instead of passing against a forgiving server.

The test process owns the :class:`MockOAuthState` and the server runs in a thread (see ``mock_oauth_mcp_fixtures.py``), so the state is guarded by a
lock; a test changes a knob or reads a record from its own thread at any time.

Never use this in product code. It issues readable tokens (``mock-at-...``, ``mock-rt-...``) and keeps them for the life of the process.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from tests._support.mcp._tools import register

__all__ = ["MockOAuthServer", "MockOAuthState", "build_mock_oauth_mcp_app"]

_SCOPE = "mcp"
_REALM = 'Basic realm="mock-oauth"'
_PKCE_TEXT = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_NEVER = float("-inf")


# ---- the state a test sees ------------------------------------------------------------------------------------------------------------------------------------------------


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str
    used: bool = False


@dataclass
class _Access:
    client_id: str
    scope: str
    grant_id: str
    expires_at: float          # time.monotonic()


@dataclass
class _Refresh:
    client_id: str
    scope: str
    grant_id: str
    used: bool = False


class _Books:
    """What the server has issued. Every access goes through ``lock``: the server thread and the test thread both touch it."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.codes: dict[str, _Code] = {}
        self.access: dict[str, _Access] = {}
        self.refresh: dict[str, _Refresh] = {}
        self.clients: dict[str, list[str]] = {}          # registered (public) clients: client_id -> redirect URIs


@dataclass
class MockOAuthState:
    """The knobs a test turns and the records it reads.

    Knobs
        ``access_token_ttl_s``: lifetime of the access tokens issued from now on, and the ``expires_in`` they carry.
        ``rotate_refresh_tokens``: a refresh grant answers a NEW refresh token and spends the one it was given. When False the answer carries none and the
        old one stays valid, like a server that does not rotate.
        ``issue_refresh_tokens``: when False no grant answers a refresh token and no refresh grant spends the one it was given (the old one is kept).
        ``token_status_override``: every token request is answered with this status and a matching error, touching nothing (503 is "the server is down").
        ``token_delay_s``: seconds the token endpoint waits before it answers (the request is recorded when it arrives).
        ``deny_next_authorize``: the next valid authorization request is declined with ``access_denied``; the flag is then reset.
        ``reject_bearer_tokens``: ``/mcp`` answers every bearer token with the 401 challenge, a live one too (a resource server that will not take
        what its authorization server issues: another audience, a consent revoked on the resource side). The token endpoint is untouched.
        ``static_clients``: pre-registered clients, id to secret. An empty secret makes the client public.

    Records
        ``token_requests``: the form fields of each token request, in arrival order, whatever the answer was.
        ``registrations``: the JSON body of each accepted registration.
        ``revocations``: the token of each accepted revocation request, in arrival order.
        ``seen_tokens``: every bearer token presented to ``/mcp``, in arrival order, valid or not.
    """

    access_token_ttl_s: int = 3600
    rotate_refresh_tokens: bool = True
    issue_refresh_tokens: bool = True
    token_status_override: int | None = None
    token_delay_s: float = 0.0
    deny_next_authorize: bool = False
    reject_bearer_tokens: bool = False
    static_clients: dict[str, str] = field(default_factory=lambda: {"cc-client": "cc-secret", "pre-client": "pre-secret"})

    token_requests: list[dict[str, str]] = field(default_factory=list)
    registrations: list[dict] = field(default_factory=list)
    revocations: list[str] = field(default_factory=list)
    seen_tokens: list[str] = field(default_factory=list)

    _books: _Books = field(default_factory=_Books, init=False, repr=False, compare=False)

    def expire_access_tokens(self) -> None:
        """End every access token issued so far, as if its lifetime had run out. Refresh tokens are untouched."""
        with self._books.lock:
            for record in self._books.access.values():
                record.expires_at = _NEVER

    def revoke_all(self) -> None:
        """Drop every access token and every refresh token, as if the user had revoked the app at the provider."""
        with self._books.lock:
            self._books.access.clear()
            self._books.refresh.clear()


@dataclass(frozen=True)
class MockOAuthServer:
    """Where a running mock is: what the fixtures yield."""

    state: MockOAuthState
    base_url: str
    mcp_url: str
    issuer: str

    @classmethod
    def at(cls, state: MockOAuthState, base_url: str) -> MockOAuthServer:
        base = base_url.rstrip("/")
        return cls(state=state, base_url=base, mcp_url=base + "/mcp", issuer=base + "/as")


# ---- small helpers --------------------------------------------------------------------------------------------------------------------------------------------------------


def _error(status: int, error: str, description: str, *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status, headers=headers)


def _invalid_client(description: str) -> JSONResponse:
    return _error(401, "invalid_client", description, headers={"WWW-Authenticate": _REALM, "Cache-Control": "no-store"})


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "replace")).digest()).rstrip(b"=").decode("ascii")


def _same(expected: str, supplied: str) -> bool:
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def _redirect(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return RedirectResponse(urlunsplit(parts._replace(query=urlencode(query))), status_code=302)


async def _form(request: Request) -> dict[str, str] | JSONResponse:
    """The urlencoded body of an OAuth request, or the 400 it earns: another content type, a body that is not UTF-8, or a repeated parameter."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return _error(400, "invalid_request", "the body must be application/x-www-form-urlencoded")
    try:
        pairs = parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return _error(400, "invalid_request", "the body must be UTF-8")
    form: dict[str, str] = {}
    for key, value in pairs:
        if key in form:
            return _error(400, "invalid_request", f"the parameter {key} is repeated")
        form[key] = value
    return form


@dataclass(frozen=True)
class _Client:
    client_id: str
    static: bool


# ---- the app --------------------------------------------------------------------------------------------------------------------------------------------------------------


def build_mock_oauth_mcp_app(state: MockOAuthState, *, base_url: str) -> Starlette:
    """The combined ASGI app. ``base_url`` is the origin clients are told to use (it may differ from the address the server is bound to)."""
    base = base_url.rstrip("/")
    mcp_url = base + "/mcp"
    issuer = base + "/as"
    challenge = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="{_SCOPE}"'
    books = state._books

    # ---- metadata ---------------------------------------------------------------------------------------------------------------------------------------------------------

    async def protected_resource(_request: Request) -> Response:
        return JSONResponse({"resource": mcp_url, "authorization_servers": [issuer], "scopes_supported": [_SCOPE]})

    async def authorization_server(_request: Request) -> Response:
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token",
            "registration_endpoint": issuer + "/register",
            "revocation_endpoint": issuer + "/revoke",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
        })

    async def not_found(_request: Request) -> Response:
        return JSONResponse({"error": "not_found"}, status_code=404)

    # ---- client registration ----------------------------------------------------------------------------------------------------------------------------------------------

    async def register_client(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "invalid_client_metadata", "the body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "invalid_client_metadata", "the body must be a JSON object")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
            return _error(400, "invalid_redirect_uri", "redirect_uris must be a non-empty list of URLs")
        if body.get("token_endpoint_auth_method", "none") != "none":
            return _error(400, "invalid_client_metadata", "this server registers public clients only (token_endpoint_auth_method none)")
        client_id = "dcr-" + secrets.token_hex(8)
        with books.lock:
            books.clients[client_id] = list(uris)
            state.registrations.append(body)
        return JSONResponse(
            {**body, "client_id": client_id, "client_id_issued_at": int(time.time()), "token_endpoint_auth_method": "none"},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    # ---- authorization endpoint -------------------------------------------------------------------------------------------------------------------------------------------

    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")
        with books.lock:
            registered = books.clients.get(client_id)
        if client_id not in state.static_clients and registered is None:
            return _error(400, "invalid_request", "unknown client_id")
        if not redirect_uri:
            return _error(400, "invalid_request", "redirect_uri is required")
        parts = urlsplit(redirect_uri)
        if not parts.scheme or parts.fragment:
            return _error(400, "invalid_request", "redirect_uri must be an absolute URL without a fragment")
        if registered is not None and redirect_uri not in registered:
            return _error(400, "invalid_request", "redirect_uri is not registered for this client")
        if q.get("response_type") != "code":
            return _error(400, "invalid_request", "response_type must be code")
        if q.get("code_challenge_method") != "S256":
            return _error(400, "invalid_request", "code_challenge_method must be S256")
        code_challenge = q.get("code_challenge", "")
        if not _PKCE_TEXT.match(code_challenge):
            return _error(400, "invalid_request", "code_challenge is missing or is not 43 to 128 base64url characters")
        client_state = q.get("state", "")
        if not client_state:
            return _error(400, "invalid_request", "state is required")
        if q.get("resource") != mcp_url:
            return _error(400, "invalid_request", f"resource must be {mcp_url}")
        with books.lock:                                # check and reset together: "the next" request is one request, even when two arrive at once
            deny = state.deny_next_authorize
            if deny:
                state.deny_next_authorize = False
        if deny:
            return _redirect(redirect_uri, {"error": "access_denied", "state": client_state})
        code = "mock-code-" + secrets.token_hex(16)
        with books.lock:
            books.codes[code] = _Code(client_id, redirect_uri, code_challenge, q.get("scope") or _SCOPE)
        return _redirect(redirect_uri, {"code": code, "state": client_state})

    # ---- client authentication at the token and revocation endpoints ------------------------------------------------------------------------------------------------------

    def authenticate(request: Request, form: dict[str, str]) -> _Client | JSONResponse:
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        client_id: str | None = form.get("client_id")
        supplied: list[str] = [form["client_secret"]] if form.get("client_secret") else []       # the spellings of the secret the client may have meant
        if scheme.lower() == "basic":
            if supplied:
                return _error(400, "invalid_request", "use one client authentication method")
            try:
                decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
            except ValueError:
                return _invalid_client("the Basic credentials are not base64 of id:secret")
            basic_id, separator, basic_secret = decoded.partition(":")
            if not separator:
                return _invalid_client("the Basic credentials are not id:secret")
            if client_id is not None and client_id not in (basic_id, unquote_plus(basic_id)):
                return _error(400, "invalid_request", "client_id differs from the Basic credentials")
            # RFC 6749 2.3.1 form-encodes the id and the secret before the base64, and clients differ on it: take the plain spelling and the decoded one.
            client_id = basic_id if basic_id in state.static_clients or basic_id in books.clients else unquote_plus(basic_id)
            supplied = [basic_secret, unquote_plus(basic_secret)] if basic_secret else []
        if not client_id:
            return _invalid_client("no client_id")
        expected = state.static_clients.get(client_id)
        if expected is not None:
            if expected and not any(_same(expected, candidate) for candidate in supplied):
                return _invalid_client("the client secret is missing or wrong")
            return _Client(client_id, static=True)
        with books.lock:
            known = client_id in books.clients
        if known and not supplied:
            return _Client(client_id, static=False)
        return _invalid_client("unknown client, or a public client that sent a secret")

    # ---- token endpoint ---------------------------------------------------------------------------------------------------------------------------------------------------

    def issue(client_id: str, scope: str, grant_id: str, *, refresh: bool) -> dict[str, Any]:
        """Mint an access token (and a refresh token when asked). The caller holds ``books.lock``."""
        access = "mock-at-" + secrets.token_hex(16)
        books.access[access] = _Access(client_id, scope, grant_id, time.monotonic() + state.access_token_ttl_s)
        body: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "expires_in": state.access_token_ttl_s, "scope": scope}
        if refresh:
            token = "mock-rt-" + secrets.token_hex(16)
            books.refresh[token] = _Refresh(client_id, scope, grant_id)
            body["refresh_token"] = token
        return body

    def token_answer(body: dict[str, Any]) -> Response:
        return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def token(request: Request) -> Response:
        form = await _form(request)
        if isinstance(form, JSONResponse):
            return form
        state.token_requests.append(dict(form))
        if state.token_delay_s > 0:
            await asyncio.sleep(state.token_delay_s)
        forced = state.token_status_override
        if forced is not None:
            kind = "server_error" if forced >= 500 else "invalid_request"
            return _error(forced, kind, f"the status {forced} was forced by the test")
        client = authenticate(request, form)
        if isinstance(client, JSONResponse):
            return client
        resource = form.get("resource")
        if resource is not None and resource != mcp_url:
            return _error(400, "invalid_target", f"resource must be {mcp_url}")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return authorization_code_grant(client, form)
        if grant == "refresh_token":
            return refresh_token_grant(client, form)
        if grant == "client_credentials":
            return client_credentials_grant(client, form)
        if not grant:
            return _error(400, "invalid_request", "grant_type is required")
        return _error(400, "unsupported_grant_type", f"grant_type {grant} is not supported")

    def authorization_code_grant(client: _Client, form: dict[str, str]) -> Response:
        code_value, verifier, redirect_uri = form.get("code"), form.get("code_verifier"), form.get("redirect_uri")
        if not code_value or not redirect_uri:
            return _error(400, "invalid_request", "code and redirect_uri are required")
        if not verifier or not _PKCE_TEXT.match(verifier):
            return _error(400, "invalid_request", "code_verifier is missing or is not 43 to 128 unreserved characters")
        with books.lock:
            record = books.codes.get(code_value)
            if record is None or record.used:
                return _error(400, "invalid_grant", "the code is unknown or was already used")
            record.used = True          # spent by the first attempt that reaches it, whatever the outcome
            if record.client_id != client.client_id or record.redirect_uri != redirect_uri:
                return _error(400, "invalid_grant", "the code was issued to another client or redirect_uri")
            if not _same(record.code_challenge, _pkce_challenge(verifier)):
                return _error(400, "invalid_grant", "the code_verifier does not match the code_challenge")
            return token_answer(issue(client.client_id, record.scope, secrets.token_hex(6), refresh=state.issue_refresh_tokens))

    def refresh_token_grant(client: _Client, form: dict[str, str]) -> Response:
        value = form.get("refresh_token")
        if not value:
            return _error(400, "invalid_request", "refresh_token is required")
        with books.lock:
            record = books.refresh.get(value)
            if record is None or record.used or record.client_id != client.client_id:
                return _error(400, "invalid_grant", "the refresh token is unknown, was already used or belongs to another client")
            rotate = state.issue_refresh_tokens and state.rotate_refresh_tokens
            answer = issue(record.client_id, record.scope, record.grant_id, refresh=rotate)
            if rotate:
                record.used = True
            return token_answer(answer)

    def client_credentials_grant(client: _Client, form: dict[str, str]) -> Response:
        if not client.static:
            return _error(400, "unauthorized_client", "only the pre-registered clients may use client_credentials")
        with books.lock:
            return token_answer(issue(client.client_id, form.get("scope") or _SCOPE, secrets.token_hex(6), refresh=False))

    # ---- revocation -------------------------------------------------------------------------------------------------------------------------------------------------------

    async def revoke(request: Request) -> Response:
        form = await _form(request)
        if isinstance(form, JSONResponse):
            return form
        client = authenticate(request, form)
        if isinstance(client, JSONResponse):
            return client
        value = form.get("token")
        if not value:
            return _error(400, "invalid_request", "token is required")
        with books.lock:
            state.revocations.append(value)
            refresh = books.refresh.pop(value, None)
            if refresh is not None:
                for token in [t for t, a in books.access.items() if a.grant_id == refresh.grant_id]:
                    del books.access[token]
            books.access.pop(value, None)
        return Response(status_code=200)

    # ---- the protected MCP endpoint ---------------------------------------------------------------------------------------------------------------------------------------

    server = MCPServer("mock-oauth-mcp")
    register(server)
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    class BearerGate:
        """Lets a request through to the MCP app only with a live access token; records every bearer token it is shown."""

        def __init__(self, app: ASGIApp) -> None:
            self._app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self._app(scope, receive, send)
                return
            scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
            token = token.strip() if scheme.lower() == "bearer" else ""
            if token:
                state.seen_tokens.append(token)
            if not token or state.reject_bearer_tokens or not self._live(token):
                refusal = JSONResponse(
                    {"error": "invalid_token", "error_description": "a valid access token is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": challenge},
                )
                await refusal(scope, receive, send)
                return
            await self._app(scope, receive, send)

        @staticmethod
        def _live(token: str) -> bool:
            with books.lock:
                record = books.access.get(token)
                return record is not None and record.expires_at > time.monotonic()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # The MCP app's own lifespan runs the session manager its transport needs; a sub-app's lifespan is not run by the parent.
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    return Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
            Route("/.well-known/oauth-authorization-server/as", authorization_server),
            Route("/.well-known/oauth-protected-resource", not_found),
            Route("/.well-known/oauth-authorization-server", not_found),
            Route("/.well-known/openid-configuration", not_found),
            Route("/as/register", register_client, methods=["POST"]),
            Route("/as/authorize", authorize, methods=["GET"]),
            Route("/as/token", token, methods=["POST"]),
            Route("/as/revoke", revoke, methods=["POST"]),
            Route("/mcp", BearerGate(mcp_app)),
        ],
        lifespan=lifespan,
    )
