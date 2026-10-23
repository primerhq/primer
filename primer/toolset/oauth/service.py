"""One shared OAuth sign-in per MCP toolset (plan 07, Tasks 7 and 8; design 12.3 to 12.6 and 20 Q2).

A toolset that signs in with OAuth has ONE sign-in, owned by the toolset and used by every agent and caller of it. :class:`ToolsetOAuthService` (one
per server process) is the only code that starts, finishes, uses or ends one:

* **Browser sign-in** (``authorization_code``). :meth:`~ToolsetOAuthService.start_sign_in` discovers the authorization server
  (:mod:`primer.toolset.oauth.protocol`), refuses one without PKCE S256, picks the client (the toolset's static client, else its earlier registration
  when the issuer and redirect URI still match, else a new dynamic registration), stores a pending sign-in (the PKCE verifier sealed, single use, ten
  minutes) and returns the authorize URL. The pending sign-in is bound to the browser that started it: it keeps the digest of a nonce that browser holds
  as an HttpOnly cookie, and :meth:`~ToolsetOAuthService.complete_sign_in` takes it back by its ``state`` only with that nonce, then exchanges the code.
  A callback whose state is unknown, used, expired or another toolset's, or that comes from another browser, ends as ``invalid_state``; one the provider
  declined as ``access_denied``; one for a toolset whose URL or OAuth settings changed since the start as ``settings_changed``; a refused or failed code
  exchange as ``exchange_failed``. None of them writes a token.
* **Client credentials**. :meth:`~ToolsetOAuthService.connect_client_credentials` exchanges the toolset's client id and secret at once, at the
  configured token endpoint or the discovered one.
* **Disconnect** ends the toolset's pending sign-ins, revokes the refresh token (else the access token) where the server advertises RFC 7009
  revocation, best effort, and deletes the tokens whatever the answer. :meth:`~ToolsetOAuthService.forget` removes everything stored for a toolset (its
  deletion, a change of its settings).
* **Status** (:meth:`~ToolsetOAuthService.status`) says ``connected``, ``needs_sign_in`` or ``error`` with the reason and expiry, or ``not_applicable``
  for a toolset without OAuth. It never carries a token.
* **Using the token** (:meth:`~ToolsetOAuthService.access_token`, :meth:`~ToolsetOAuthService.refresh_after_401`). A token that expires within
  :data:`REFRESH_SKEW`, or that the MCP server rejected, is refreshed under a LEASE: the process that wins a compare-and-set on the token row's version
  and lease exchanges the refresh token, and the others wait for its answer (reading the row every :data:`LEASE_POLL_S`, for at most
  :data:`LEASE_WAIT_S`), so a rotating refresh token is spent once and every process ends with the new one. Every write of the lease holder is fenced
  by the version it leased and its own lease token, so a reconnect, a disconnect or the takeover of a dead lease is never overwritten. A refused refresh
  signs the toolset out; a token endpoint that cannot be reached keeps the tokens and marks the state ``error``. The exchange runs to its end even when
  its caller is cancelled, so a Stop never loses a refresh token the server already rotated. A process whose encryption key cannot open the stored
  sign-in fails its own call (:data:`KEY_CHANGED`) with an ERROR line and changes nothing: the other processes keep the shared tokens (lead ruling on
  audit G4-10).

The tokens are bound to the settings they were obtained for: :func:`config_fingerprint` hashes the URL and the OAuth settings, and a toolset whose
settings no longer hash to the stored value has no usable sign-in, so a token is never sent to a URL it was not issued for.

Nothing here logs a token, a code, a PKCE verifier, a ``state`` or a client secret: log lines name the toolset, HTTP statuses and RFC 6749 error codes.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, ParamSpec, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from primer.common.error_codes import safe_code
from primer.common.origin import origin_of
from primer.common.secret_box import SecretBox, SecretBoxError
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import BadRequestError, ConfigError, PrimerError, ProviderError
from primer.model.mcp_oauth import ClientAuthMethod, ClientSource, McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet, McpSignInRequiredError
from primer.model.provider import HttpConfig, McpConfig, OAuthConfig, Toolset, ToolsetProviderType, TransportType
from primer.storage import raw_generation
from primer.toolset.oauth import egress, protocol
from primer.toolset.oauth.protocol import ClientAuth, Discovery
from primer.toolset.oauth.store import McpOAuthStore, binding_digest

__all__ = [
    "EXPIRED_NO_REFRESH",
    "HTTP_TIMEOUT_S",
    "KEY_CHANGED",
    "LEASE_POLL_S",
    "LEASE_TTL",
    "LEASE_WAIT_S",
    "NOT_CONNECTED",
    "PENDING_TTL",
    "REFRESH_SKEW",
    "SETTINGS_CHANGED",
    "TOKEN_CALL_TIMEOUT_S",
    "TOKEN_REJECTED",
    "AuthMode",
    "AuthState",
    "OAuthTarget",
    "SignInError",
    "SignInErrorCode",
    "SignInStart",
    "ToolsetAuthStatus",
    "ToolsetOAuthService",
    "config_fingerprint",
]

logger = logging.getLogger(__name__)

AuthMode = Literal["none", "header", "oauth_browser", "oauth_client_credentials"]
AuthState = Literal["not_applicable", "connected", "needs_sign_in", "error"]
SignInErrorCode = Literal["invalid_state", "access_denied", "exchange_failed", "settings_changed"]

#: How long a started browser sign-in can be finished.
PENDING_TTL = timedelta(minutes=10)
#: The timeout of every OAuth HTTP call the service makes (one ``httpx.AsyncClient`` per service, redirects not followed).
HTTP_TIMEOUT_S = 10.0

#: A token that expires within this is refreshed before it is used.
REFRESH_SKEW = timedelta(seconds=60)
#: The bound on one whole token call made under the refresh lease (the HTTP client's timeout applies to each phase of a call, not to the whole).
TOKEN_CALL_TIMEOUT_S = HTTP_TIMEOUT_S
#: How long a refresh lease holds: three whole token calls, so a holder that is alive never loses its lease in the middle of one.
LEASE_TTL = timedelta(seconds=30)
#: How often a process that waits for another's refresh reads the row again ...
LEASE_POLL_S = 0.25
#: ... and how long it waits at most before it fails the call with a temporary error.
LEASE_WAIT_S = 35.0

NOT_CONNECTED = "Not connected."
SETTINGS_CHANGED = "The URL or OAuth settings changed since this toolset was connected."
KEY_CHANGED = "The stored sign-in cannot be read with the current encryption key."
EXPIRED_NO_REFRESH = "The access token expired and the server issued no refresh token."
TOKEN_REJECTED = "The MCP server rejected the access token."


def _utc_now() -> datetime:
    return datetime.now(UTC)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _calls_for(target: OAuthTarget) -> AbstractContextManager[None]:
    """The OAuth calls made for ``target``: its MCP host, and a token endpoint it configures, are reached directly; any other host the server
    names passes the private-destination guard (:mod:`primer.toolset.oauth.egress`, audit G4-08)."""
    oauth = target.oauth
    return egress.calls_for(target.server_url, oauth.token_endpoint if oauth.grant == "client_credentials" else None)


def _for_its_target(method: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
    """Run a service method whose first argument after ``self`` is an :class:`OAuthTarget` inside :func:`_calls_for` that target."""
    @functools.wraps(method)
    async def run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        target = args[1]
        assert isinstance(target, OAuthTarget)
        with _calls_for(target):
            return await method(*args, **kwargs)
    return run


# ---- what a toolset says about its sign-in -------------------------------------------------------------------------------------------------------------------------------


def _http_config(toolset: Toolset | None) -> HttpConfig | None:
    """The http config of an http or sse MCP toolset, else ``None``."""
    if toolset is None or toolset.provider != ToolsetProviderType.MCP:
        return None
    config = toolset.config
    if not isinstance(config, McpConfig) or config.transport not in (TransportType.HTTP, TransportType.SSE):
        return None
    return config.config if isinstance(config.config, HttpConfig) else None


@dataclass(frozen=True, repr=False)
class OAuthTarget:
    """What the service needs of a toolset that signs in with OAuth: its id, server URL, OAuth settings and extra headers.

    ``headers`` holds the toolset's header VALUES in plaintext (sent with the discovery probe), with any ``Authorization`` left out, so the ``repr`` shows
    the toolset id only.
    """

    toolset_id: str
    server_url: str
    oauth: OAuthConfig
    headers: dict[str, str]

    @classmethod
    def from_toolset(cls, toolset: Toolset) -> OAuthTarget | None:
        """The target of an http or sse MCP toolset with an ``oauth`` block; ``None`` for any other toolset."""
        http_config = _http_config(toolset)
        if http_config is None or http_config.oauth is None or not toolset.id:
            return None
        headers = {name: value.get_secret_value() for name, value in http_config.headers.items() if name.lower() != "authorization"}
        return cls(toolset_id=toolset.id, server_url=str(http_config.url), oauth=http_config.oauth, headers=headers)

    def __repr__(self) -> str:
        return f"OAuthTarget(toolset_id={self.toolset_id!r})"


def config_fingerprint(target: OAuthTarget) -> str:
    """The SHA-256 hex of what the tokens are obtained for: the URL, the grant, the scopes (sorted), the static client id, the resource, the token endpoint
    and the client name. The client secret is not part of it: a new secret for the same client keeps the sign-in."""
    oauth = target.oauth
    facts = {
        "url": target.server_url,
        "grant": oauth.grant,
        "scopes": sorted(oauth.scopes),
        "static_client_id": oauth.static_client.client_id if oauth.static_client is not None else None,
        "resource_uri": oauth.resource_uri,
        "token_endpoint": oauth.token_endpoint,
        "client_name": oauth.client_name,
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _mode_of(target: OAuthTarget) -> AuthMode:
    return "oauth_client_credentials" if target.oauth.grant == "client_credentials" else "oauth_browser"


def _static_secret(target: OAuthTarget | None, client_id: str) -> str | None:
    """The toolset's configured client secret, when its static client is ``client_id``."""
    static = target.oauth.static_client if target is not None else None
    if static is None or static.client_id != client_id or static.client_secret is None:
        return None
    return static.client_secret.get_secret_value() or None


def _client_auth(client_id: str, secret: str | None, method: ClientAuthMethod | None) -> ClientAuth:
    """The client authentication a stored sign-in uses: none without a secret, else the remembered method (Basic for a row that remembers none)."""
    if not secret:
        return ClientAuth(client_id=client_id, client_secret=None, method="none")
    if method in ("client_secret_basic", "client_secret_post"):
        return ClientAuth(client_id=client_id, client_secret=secret, method=method)
    return ClientAuth(client_id=client_id, client_secret=secret, method="client_secret_basic")


def _fresh(row: McpOAuthTokenSet, now: datetime) -> bool:
    """True when the row's access token does not expire within :data:`REFRESH_SKEW` (a token without an expiry is fresh until the server rejects it)."""
    return row.expires_at is None or row.expires_at - now > REFRESH_SKEW


def _client_credentials_scopes(target: OAuthTarget, row: McpOAuthTokenSet) -> list[str]:
    """What a new client-credentials token asks for: the configured scopes, else the scope the server granted last time, else none."""
    if target.oauth.scopes:
        return list(target.oauth.scopes)
    return row.scope.split() if row.scope else []


# ---- what the service answers --------------------------------------------------------------------------------------------------------------------------------------------


class ToolsetAuthStatus(BaseModel):
    """A toolset's sign-in, as the API and the agents see it. Never holds a token or a secret."""

    toolset_id: str = Field(..., description="The toolset.")
    mode: AuthMode = Field(..., description="How the toolset authenticates: none, a configured header, OAuth browser sign-in or OAuth client credentials.")
    state: AuthState = Field(
        ...,
        description=(
            "'connected': it has a usable sign-in. 'needs_sign_in': an admin must connect it (again). 'error': the last refresh failed for a reason "
            "that may pass (the tokens are kept). 'not_applicable': the toolset does not sign in with OAuth."
        ),
    )
    reason: str | None = Field(default=None, description="Why the state is not 'connected', in words an operator can act on.")
    expires_at: datetime | None = Field(default=None, description="When the access token expires; None for no known expiry.")
    connected_at: datetime | None = Field(default=None, description="When an admin connected it; None when it was never connected.")
    refreshed_at: datetime | None = Field(default=None, description="When its access token was last refreshed.")
    scope: str | None = Field(default=None, description="The scope the authorization server granted.")
    redirect_uri: str | None = Field(default=None, description="The redirect URI a browser sign-in of this toolset uses, for registering a client by hand.")


class SignInStart(BaseModel):
    """What starting a sign-in answers: the URL to open (``None`` when the toolset connected at once), the redirect URI and when the sign-in expires."""

    authorize_url: str | None = Field(..., description="The authorization server's sign-in page, carrying the single-use state.")
    redirect_uri: str | None = Field(..., description="Where the authorization server sends the browser back.")
    expires_at: datetime | None = Field(..., description="When the started sign-in stops being valid.")
    status: ToolsetAuthStatus = Field(..., description="The toolset's sign-in status now.")


class SignInError(Exception):
    """A browser sign-in did not complete. ``code`` says why: ``invalid_state`` (unknown, used, expired or another toolset's state, or a browser that did
    not start the sign-in), ``access_denied`` (declined at the provider), ``exchange_failed`` (the provider refused or could not exchange the code) or
    ``settings_changed`` (the toolset was edited, removed or lost its OAuth settings during the sign-in). No token was written."""

    def __init__(self, code: SignInErrorCode, toolset_id: str) -> None:
        super().__init__(f"the sign-in of the MCP toolset {toolset_id} did not complete: {code}")
        self.code: SignInErrorCode = code
        self.toolset_id = toolset_id


# ---- the service ---------------------------------------------------------------------------------------------------------------------------------------------------------


class ToolsetOAuthService:
    """Starts, finishes, uses and ends the shared sign-ins of MCP toolsets. One per server process; safe to share between tasks."""

    def __init__(
        self,
        *,
        storage_provider: StorageProvider,
        secret_box: SecretBox,
        public_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = _utc_now,
        owner_id: str | None = None,
    ) -> None:
        self._store = McpOAuthStore(storage_provider, secret_box)
        self._public_url = public_url.rstrip("/") if public_url else None
        self._owns_http = http is None
        # The service's own client routes each request by its host (audit G4-08): one handed in (a test's) is used as it is.
        self._http = http if http is not None else egress.oauth_http_client(timeout=HTTP_TIMEOUT_S)
        self._clock = clock
        self._owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
        #: One lock per toolset: the callers of ONE process take turns at the lease, so a process makes at most one exchange at a time per toolset.
        self._locks: dict[str, asyncio.Lock] = {}
        #: Lease-and-exchange tasks still running, kept referenced until they end (a cancelled caller does not cancel them).
        self._inflight: set[asyncio.Task[Any]] = set()

    @property
    def store(self) -> McpOAuthStore:
        """The store the service reads and writes (sealing with the service's key)."""
        return self._store

    async def aclose(self) -> None:
        """Let the refreshes in flight store their answers (bounded), then close the HTTP client the service made (one handed in is the caller's)."""
        if self._inflight:
            await asyncio.wait(set(self._inflight), timeout=TOKEN_CALL_TIMEOUT_S + 5.0)
        if self._owns_http:
            await self._http.aclose()

    # ---- the redirect URI -----------------------------------------------------------------------------------------------------------------------------------------------

    def redirect_uri(self, toolset_id: str, *, request_base_url: str | None) -> str:
        """``{public URL}/v1/toolsets/{toolset id}/oauth/callback``: the deployment's public URL when it is set, else the base URL of the request that
        starts the sign-in. Raises :class:`~primer.model.except_.ConfigError` when neither is known."""
        base = self._public_url or (request_base_url or "").rstrip("/")
        if not base:
            raise ConfigError("the deployment's public URL is unknown, so no OAuth redirect URI can be built: set PRIMER_PUBLIC_URL")
        return f"{base}/v1/toolsets/{quote(toolset_id, safe='')}/oauth/callback"

    # ---- browser sign-in ------------------------------------------------------------------------------------------------------------------------------------------------

    @_for_its_target
    async def start_sign_in(self, target: OAuthTarget, *, redirect_uri: str, started_by: str | None, browser_binding: str) -> SignInStart:
        """Start a browser sign-in of ``target``: the authorize URL to open, valid for :data:`PENDING_TTL`.

        ``browser_binding`` is a nonce only the browser that starts the sign-in holds (the route sets it as an HttpOnly cookie on the start answer);
        the pending sign-in keeps its digest, and :meth:`complete_sign_in` takes it back only with the same nonce (audit G4-01). The ``state`` in the
        authorize URL is therefore not enough to finish the sign-in: an authorize URL that leaks (a browser history, a shared link, a provider-side
        log) and is completed in another browser, with another account at the provider, connects nothing.

        Raises :class:`~primer.model.except_.BadRequestError` when the toolset uses client credentials, the authorization server cannot be discovered,
        does not advertise PKCE S256, has no authorization endpoint, or (without a configured client) refuses a registration; and
        :class:`~primer.model.except_.NetworkError` when it cannot be reached.
        """
        if target.oauth.grant != "authorization_code":
            raise BadRequestError(f"the MCP toolset {target.toolset_id} signs in with client credentials, so it connects without a browser sign-in")
        if not browser_binding:
            raise ValueError("a browser sign-in is bound to the browser that starts it: browser_binding is required")
        self._require_resource_on_the_server(target)
        now = self._clock()
        await self._store.purge_pending(target.toolset_id, now=now)
        found = await self._discover(target)
        metadata = found.metadata
        protocol.require_s256(metadata)
        if not metadata.authorization_endpoint:
            raise BadRequestError(f"the authorization server {metadata.issuer} has no authorization endpoint, so a browser sign-in cannot start")
        client, source = await self._sign_in_client(target, found, redirect_uri=redirect_uri, now=now)
        resource = self._resource(target, found)
        verifier, challenge = protocol.pkce_pair()
        state = secrets.token_urlsafe(32)
        expires_at = now + PENDING_TTL
        await self._store.put_pending(McpOAuthPendingSignIn(
            id=state,
            toolset_id=target.toolset_id,
            code_verifier_enc=self._store.seal(verifier),
            redirect_uri=redirect_uri,
            issuer=metadata.issuer,
            token_endpoint=metadata.token_endpoint,
            revocation_endpoint=metadata.revocation_endpoint,
            resource=resource,
            client_id=client.client_id,
            client_source=source,
            token_endpoint_auth_method=client.method,
            config_fingerprint=config_fingerprint(target),
            browser_binding=binding_digest(browser_binding),
            started_by=started_by,
            created_at=now,
            expires_at=expires_at,
        ))
        url = protocol.authorization_url(
            metadata=metadata, client_id=client.client_id, redirect_uri=redirect_uri, scopes=self._scopes(target, found), state=state,
            code_challenge=challenge, resource=resource,
        )
        logger.info("MCP toolset %s: a browser sign-in was started (client: %s)", target.toolset_id, source)
        status = self._status_from(target, await self._store.get_tokens(target.toolset_id), now=now, redirect_uri=redirect_uri)
        return SignInStart(authorize_url=url, redirect_uri=redirect_uri, expires_at=expires_at, status=status)

    async def complete_sign_in(
        self,
        toolset: Toolset | None,
        *,
        toolset_id: str,
        state: str,
        code: str | None,
        error: str | None,
        browser_binding: str | None,
    ) -> ToolsetAuthStatus:
        """Finish the browser sign-in that the provider's redirect for ``toolset_id`` carries: exchange the code and store the tokens.

        ``toolset`` is the toolset as it is NOW (``None`` when it no longer exists). ``browser_binding`` is the nonce the callback's browser presents
        (the cookie the start answer set): a missing one, or one the sign-in was not started with, is ``invalid_state`` and leaves the pending sign-in
        to the browser that started it. Raises :class:`SignInError`; every failure leaves the stored tokens as they were, and once the binding matched
        the state is spent whatever the outcome.
        """
        now = self._clock()
        pending = await self._store.take_pending(state, now=now, browser_binding=browser_binding) if state and browser_binding else None
        if pending is None or pending.toolset_id != toolset_id:
            logger.info(
                "MCP toolset %s: a sign-in callback carried an unknown, used, expired or misdirected state, or came from a browser that did not start "
                "the sign-in", toolset_id,
            )
            raise SignInError("invalid_state", toolset_id)
        if error:
            logger.info("MCP toolset %s: the authorization server ended the sign-in with %s", toolset_id, safe_code(error))
            raise SignInError("access_denied" if error == "access_denied" else "exchange_failed", toolset_id)
        target = OAuthTarget.from_toolset(toolset) if toolset is not None and toolset.id == toolset_id else None
        if target is None or target.oauth.grant != "authorization_code" or config_fingerprint(target) != pending.config_fingerprint:
            logger.info("MCP toolset %s: its settings changed during the sign-in, so the code is not exchanged", toolset_id)
            raise SignInError("settings_changed", toolset_id)
        if not code:
            raise SignInError("exchange_failed", toolset_id)
        try:
            verifier = self._store.unseal(pending.code_verifier_enc)
            client = await self._client_of(target, toolset_id=toolset_id, client_id=pending.client_id, source=pending.client_source,
                                           method=pending.token_endpoint_auth_method)
        except SecretBoxError:
            logger.warning("MCP toolset %s: the pending sign-in cannot be read with the current encryption key", toolset_id)
            raise SignInError("exchange_failed", toolset_id) from None
        try:
            with _calls_for(target):
                grant = await protocol.exchange_code(
                    token_endpoint=pending.token_endpoint, client=client, code=code, redirect_uri=pending.redirect_uri, code_verifier=verifier,
                    resource=pending.resource, http=self._http, now=now,
                )
        except protocol.TokenEndpointRejected as exc:
            logger.warning("MCP toolset %s: the authorization server refused the code (HTTP %s, %s)", toolset_id, exc.status, safe_code(exc.error))
            raise SignInError("exchange_failed", toolset_id) from None
        except PrimerError as exc:
            logger.warning("MCP toolset %s: the code exchange failed (%s)", toolset_id, type(exc).__name__)
            raise SignInError("exchange_failed", toolset_id) from None
        stored = await self._store.replace_tokens(McpOAuthTokenSet(
            id=toolset_id,
            grant="authorization_code",
            access_token_enc=self._store.seal(grant.access_token),
            refresh_token_enc=self._store.seal(grant.refresh_token) if grant.refresh_token else None,
            token_type=grant.token_type,
            expires_at=grant.expires_at,
            scope=grant.scope,
            issuer=pending.issuer,
            token_endpoint=pending.token_endpoint,
            revocation_endpoint=pending.revocation_endpoint,
            resource=pending.resource,
            client_id=pending.client_id,
            client_source=pending.client_source,
            token_endpoint_auth_method=client.method,
            config_fingerprint=pending.config_fingerprint,
            connected_at=now,
            connected_by=pending.started_by,
        ))
        logger.info("MCP toolset %s: connected by a browser sign-in", toolset_id)
        return self._status_from(target, stored, now=now)

    # ---- client credentials ---------------------------------------------------------------------------------------------------------------------------------------------

    @_for_its_target
    async def connect_client_credentials(self, target: OAuthTarget, *, connected_by: str | None) -> ToolsetAuthStatus:
        """Connect a client-credentials toolset at once: exchange its client id and secret at its token endpoint (the configured one, else the
        discovered one) and store the token. Raises :class:`~primer.model.except_.BadRequestError` when the token endpoint refuses the credentials
        (nothing is stored), or for a toolset that does not use client credentials."""
        oauth = target.oauth
        static = oauth.static_client
        secret = _static_secret(target, static.client_id) if static is not None else None
        if oauth.grant != "client_credentials" or static is None or not secret:
            raise BadRequestError(f"the MCP toolset {target.toolset_id} does not sign in with client credentials (a client id and secret)")
        self._require_resource_on_the_server(target)
        now = self._clock()
        metadata: protocol.AuthServerMetadata | None = None
        if oauth.token_endpoint:
            # A named token endpoint is the operator's answer to a server whose discovery does not work: nothing is discovered, so a resource indicator
            # is sent only when one is configured.
            token_endpoint, resource, scopes = oauth.token_endpoint, oauth.resource_uri, list(oauth.scopes)
        else:
            found = await self._discover(target)
            metadata = found.metadata
            token_endpoint, resource, scopes = metadata.token_endpoint, self._resource(target, found), self._scopes(target, found)
        client = protocol.client_auth_for(metadata, static.client_id, secret)
        try:
            grant = await protocol.client_credentials_grant(token_endpoint=token_endpoint, client=client, scopes=scopes, resource=resource, http=self._http, now=now)
        except protocol.TokenEndpointRejected as exc:
            logger.warning("MCP toolset %s: the token endpoint refused the client credentials (HTTP %s, %s)", target.toolset_id, exc.status, safe_code(exc.error))
            raise BadRequestError(f"the token endpoint refused the client credentials ({exc.error or f'HTTP {exc.status}'})") from None
        stored = await self._store.replace_tokens(McpOAuthTokenSet(
            id=target.toolset_id,
            grant="client_credentials",
            access_token_enc=self._store.seal(grant.access_token),
            refresh_token_enc=None,
            token_type=grant.token_type,
            expires_at=grant.expires_at,
            scope=grant.scope,
            issuer=metadata.issuer if metadata is not None else None,
            token_endpoint=token_endpoint,
            revocation_endpoint=metadata.revocation_endpoint if metadata is not None else None,
            resource=resource,
            client_id=static.client_id,
            client_source="static",
            token_endpoint_auth_method=client.method,
            config_fingerprint=config_fingerprint(target),
            connected_at=now,
            connected_by=connected_by,
        ))
        logger.info("MCP toolset %s: connected with client credentials", target.toolset_id)
        return self._status_from(target, stored, now=now)

    # ---- disconnect and forget ------------------------------------------------------------------------------------------------------------------------------------------

    async def disconnect(self, toolset: Toolset) -> ToolsetAuthStatus:
        """End the toolset's sign-in: drop its pending sign-ins, revoke it where the server supports revocation (best effort), then delete the tokens
        whatever the answer.

        The pending sign-ins go first (audit G4-06): a Reconnect left open on the provider's page would otherwise stay valid for its ten minutes, and
        completing it after the Disconnect would connect the toolset again.
        """
        assert toolset.id is not None
        dropped = await self._store.drop_pending(toolset.id)
        if dropped:
            logger.info("MCP toolset %s: %d pending sign-in(s) ended by the disconnect", toolset.id, dropped)
        row = await self._store.get_tokens(toolset.id)
        if row is not None:
            target = OAuthTarget.from_toolset(toolset)
            http_config = _http_config(toolset)
            calls: AbstractContextManager[None] = (
                _calls_for(target) if target is not None else egress.calls_for(str(http_config.url)) if http_config is not None else nullcontext()
            )
            with calls:
                revoked = await self._revoke(target, row)
            await self._store.delete_tokens(toolset.id)
            logger.info("MCP toolset %s: disconnected (revoked at the authorization server: %s)", toolset.id, revoked)
        return await self.status(toolset)

    async def forget(self, toolset_id: str) -> None:
        """Remove everything stored for the toolset: its tokens, its registered client and its pending sign-ins."""
        await self._store.forget(toolset_id)

    # ---- status ---------------------------------------------------------------------------------------------------------------------------------------------------------

    async def status(self, toolset: Toolset, *, redirect_uri: str | None = None) -> ToolsetAuthStatus:
        """The toolset's sign-in status (see :class:`ToolsetAuthStatus`). ``redirect_uri`` is shown for a browser sign-in."""
        assert toolset.id is not None
        target = OAuthTarget.from_toolset(toolset)
        if target is None:
            http_config = _http_config(toolset)
            mode: AuthMode = "header" if http_config is not None and http_config.headers else "none"
            return ToolsetAuthStatus(toolset_id=toolset.id, mode=mode, state="not_applicable")
        return self._status_from(target, await self._store.get_tokens(target.toolset_id), now=self._clock(), redirect_uri=redirect_uri)

    def _status_from(self, target: OAuthTarget, row: McpOAuthTokenSet | None, *, now: datetime, redirect_uri: str | None = None) -> ToolsetAuthStatus:
        mode = _mode_of(target)
        base: dict[str, Any] = {"toolset_id": target.toolset_id, "mode": mode, "redirect_uri": redirect_uri if mode == "oauth_browser" else None}
        if row is None:
            return ToolsetAuthStatus(**base, state="needs_sign_in", reason=NOT_CONNECTED)
        base.update(connected_at=row.connected_at, refreshed_at=row.refreshed_at)
        if row.config_fingerprint != config_fingerprint(target):
            return ToolsetAuthStatus(**base, state="needs_sign_in", reason=SETTINGS_CHANGED)
        try:
            self._store.unseal(row.access_token_enc)
        except SecretBoxError:
            return ToolsetAuthStatus(**base, state="needs_sign_in", reason=KEY_CHANGED)
        if row.state == "needs_sign_in":
            return ToolsetAuthStatus(**base, state="needs_sign_in", reason=row.reason)
        if row.state == "error":
            return ToolsetAuthStatus(**base, state="error", reason=row.reason, expires_at=row.expires_at, scope=row.scope)
        if row.grant == "authorization_code" and row.refresh_token_enc is None and row.expires_at is not None and row.expires_at <= now:
            return ToolsetAuthStatus(**base, state="needs_sign_in", reason=EXPIRED_NO_REFRESH)
        return ToolsetAuthStatus(**base, state="connected", expires_at=row.expires_at, scope=row.scope)

    # ---- using the token ------------------------------------------------------------------------------------------------------------------------------------------------

    @_for_its_target
    async def access_token(self, target: OAuthTarget) -> str:
        """The access token to send to ``target``'s MCP server, refreshed first when it expires within :data:`REFRESH_SKEW`.

        Raises :class:`~primer.model.mcp_oauth.McpSignInRequiredError` when the toolset has no usable sign-in (never connected, its settings changed,
        signed out) or this process's key cannot read it (:data:`KEY_CHANGED`, the row left as it is), and :class:`~primer.model.except_.ProviderError`
        for a failure that may pass (the token endpoint cannot be reached, another process holds the lease for too long), which keeps the tokens.
        """
        row = self._require_usable(target, await self._store.get_tokens(target.toolset_id))
        access = self._readable_access(row)
        now = self._clock()
        if access and _fresh(row, now):
            return access
        if row.grant == "authorization_code" and row.refresh_token_enc is None:
            if access and (row.expires_at is None or row.expires_at > now):
                return access                           # nothing can refresh it, so it serves until it expires
            await self._sign_out(row, EXPIRED_NO_REFRESH)
            raise McpSignInRequiredError(target.toolset_id, reason=EXPIRED_NO_REFRESH)
        return await self._refresh(target, observed=row, forced=False)

    @_for_its_target
    async def refresh_after_401(self, target: OAuthTarget, *, rejected: str, www_authenticate: str | None) -> str:
        """A replacement for the access token ``rejected``, which the MCP server answered with 401: the stored token when another caller or process
        already replaced it, else a forced refresh (whatever the stored expiry says). A browser sign-in without a refresh token is signed out with
        :data:`TOKEN_REJECTED`. Raises like :meth:`access_token`."""
        row = self._require_usable(target, await self._store.get_tokens(target.toolset_id))
        access = self._readable_access(row)
        if row.state == "connected" and access and access != rejected:
            return access
        if row.grant == "authorization_code" and row.refresh_token_enc is None:
            await self._sign_out(row, TOKEN_REJECTED)
            raise McpSignInRequiredError(target.toolset_id, reason=TOKEN_REJECTED)
        challenge_error = protocol.parse_www_authenticate(www_authenticate).get("error")
        logger.info("MCP toolset %s: the MCP server rejected the access token (%s); refreshing it", target.toolset_id, safe_code(challenge_error) or "no error code")
        return await self._refresh(target, observed=row, forced=True)

    async def sign_out_rejected(self, target: OAuthTarget, *, rejected: str) -> None:
        """The MCP server rejected ``rejected`` as well: the token a 401 had just been answered with (the second 401 of one request, audit G4-11).

        A freshly refreshed token that the server will not take either means the sign-in no longer works for this server (another audience, a
        consent withdrawn on the resource side), so the toolset is signed out with :data:`TOKEN_REJECTED`: its status stops saying connected, and the
        next call fails at once instead of spending another refresh token. The sign-out is fenced on the version that holds ``rejected`` (and on its
        lease as read): a reconnect, or a newer token another caller or process stored meanwhile, is left alone. A row this process cannot read is
        left alone too (:data:`KEY_CHANGED`).
        """
        row = await self._store.get_tokens(target.toolset_id)
        if row is None or row.state != "connected" or row.config_fingerprint != config_fingerprint(target):
            return
        try:
            current = self._store.unseal(row.access_token_enc)
        except SecretBoxError:
            return
        if current != rejected:
            return
        await self._sign_out(row, TOKEN_REJECTED)

    async def _refresh(self, target: OAuthTarget, *, observed: McpOAuthTokenSet, forced: bool) -> str:
        """Refresh the token set ``observed`` was read from, or take the token another caller or process stored meanwhile.

        Under the per-toolset lock of this process, until :data:`LEASE_WAIT_S` has passed: read the row; a connected, fresh token is returned (for a
        FORCED refresh only a newer version than the observed one: the observed token was rejected, whatever its expiry says); a free or expired lease is
        taken by compare-and-set on the version and the lease as read, and the winner exchanges; anyone else polls every :data:`LEASE_POLL_S`.
        """
        async with self._lock_for(target.toolset_id):
            deadline = time.monotonic() + LEASE_WAIT_S
            while True:
                row = self._require_usable(target, await self._store.get_tokens(target.toolset_id))
                now = self._clock()
                if row.state == "connected" and (row.version > observed.version or not forced) and _fresh(row, now):
                    access = self._readable_access(row)
                    if access:
                        return access
                if row.refresh_lease_until is None or row.refresh_lease_until <= now:
                    token = await self._shielded(self._lease_and_exchange(target, row, now=now, forced=forced))
                    if token is not None:
                        return token
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"the MCP server {target.toolset_id} could not refresh its sign-in: another server process is refreshing it; try again"
                    )
                await asyncio.sleep(LEASE_POLL_S)

    async def _lease_and_exchange(self, target: OAuthTarget, row: McpOAuthTokenSet, *, now: datetime, forced: bool) -> str | None:
        """Take the refresh lease of ``row`` (``None`` when another caller or process took it first), then exchange and store."""
        owner = f"{self._owner_id}#{secrets.token_hex(4)}"         # one lease token per lease: the fence of every write this exchange makes
        won = await self._store.patch_tokens(
            target.toolset_id,
            {"refresh_lease_until": now + LEASE_TTL, "refresh_lease_owner": owner},
            where={"version": [raw_generation(row, "version")], "refresh_lease_until": [raw_generation(row, "refresh_lease_until")]},
        )
        if won is None:
            return None
        return await self._exchange_and_store(target, won, owner=owner, forced=forced)

    async def _exchange_and_store(self, target: OAuthTarget, won: McpOAuthTokenSet, *, owner: str, forced: bool) -> str:
        """Holding the lease of ``won``: exchange its refresh token (or its client credentials) and store the answer.

        Every write is fenced by the version that was leased AND this lease's token, so a reconnect, a disconnect or a process that took over a dead lease
        is never overwritten; a write that does not apply re-reads, and a newer connected token stored meanwhile is the answer. A success stores the new
        access token, the new refresh token only when the answer carries one (else the old one is kept), and bumps the version. A refusal signs the
        toolset out (``needs_sign_in``; ``error`` for client credentials, which can recover without a person) and raises
        :class:`~primer.model.mcp_oauth.McpSignInRequiredError`. A token endpoint that cannot be reached, or does not answer within
        :data:`TOKEN_CALL_TIMEOUT_S`, keeps the tokens, marks the state ``error`` and raises :class:`~primer.model.except_.ProviderError`.
        """
        toolset_id = target.toolset_id
        fence = {"version": [raw_generation(won, "version")], "refresh_lease_owner": [owner]}
        try:
            client = await self._client_of(target, toolset_id=toolset_id, client_id=won.client_id, source=won.client_source,
                                           method=won.token_endpoint_auth_method)
            refresh_token = self._store.unseal(won.refresh_token_enc)
        except SecretBoxError:
            # Lead ruling on audit G4-10: this process's key cannot open what another process sealed. Only this call fails; the shared tokens stay
            # for the processes that can open them, and the lease this process took is given back untouched otherwise.
            await self._store.patch_tokens(toolset_id, {"refresh_lease_until": None, "refresh_lease_owner": None}, where={"refresh_lease_owner": [owner]})
            self._key_differs(toolset_id)
            raise McpSignInRequiredError(toolset_id, reason=KEY_CHANGED) from None
        if won.grant == "authorization_code" and refresh_token is None:
            return await self._signed_out_or_newer(target, won, fence, TOKEN_REJECTED if forced else EXPIRED_NO_REFRESH, state="needs_sign_in")
        now = self._clock()
        try:
            async with asyncio.timeout(TOKEN_CALL_TIMEOUT_S):
                if won.grant == "client_credentials":
                    grant = await protocol.client_credentials_grant(
                        token_endpoint=won.token_endpoint, client=client, scopes=_client_credentials_scopes(target, won), resource=won.resource,
                        http=self._http, now=now,
                    )
                else:
                    assert refresh_token is not None
                    grant = await protocol.refresh_grant(
                        token_endpoint=won.token_endpoint, client=client, refresh_token=refresh_token, resource=won.resource, http=self._http, now=now,
                    )
        except protocol.TokenEndpointRejected as exc:
            what = "client credentials" if won.grant == "client_credentials" else "refresh token"
            reason = f"The authorization server refused the {what} ({exc.error or f'HTTP {exc.status}'})."
            state: Literal["needs_sign_in", "error"] = "error" if won.grant == "client_credentials" else "needs_sign_in"
            return await self._signed_out_or_newer(target, won, fence, reason, state=state)
        except TimeoutError:
            return await self._kept_after_outage(target, won, owner, f"the token endpoint did not answer within {TOKEN_CALL_TIMEOUT_S:g}s")
        except PrimerError as exc:
            return await self._kept_after_outage(target, won, owner, exc.message)
        patch: dict[str, Any] = {
            "access_token_enc": self._store.seal(grant.access_token),
            "token_type": grant.token_type,
            "expires_at": grant.expires_at,
            "state": "connected",
            "reason": None,
            "version": won.version + 1,
            "refresh_lease_until": None,
            "refresh_lease_owner": None,
            "refreshed_at": now,
        }
        if grant.refresh_token and won.grant == "authorization_code":
            patch["refresh_token_enc"] = self._store.seal(grant.refresh_token)
        if grant.scope:
            patch["scope"] = grant.scope
        if await self._store.patch_tokens(toolset_id, patch, where=fence) is not None:
            logger.info("MCP toolset %s: refreshed its access token", toolset_id)
            return grant.access_token
        newer = await self._newer_token(target, won)
        if newer is not None:
            return newer
        raise ProviderError(f"the MCP server {toolset_id} could not refresh its sign-in: the sign-in changed while it was being refreshed; try again")

    async def _signed_out_or_newer(
        self,
        target: OAuthTarget,
        won: McpOAuthTokenSet,
        fence: dict[str, list[Any]],
        reason: str,
        *,
        state: Literal["needs_sign_in", "error"],
    ) -> str:
        """Drop the tokens of ``won`` with ``reason`` and raise :class:`~primer.model.mcp_oauth.McpSignInRequiredError`; when the row moved on
        meanwhile, a newer connected token stored by someone else is the answer instead."""
        patch = {"access_token_enc": None, "refresh_token_enc": None, "state": state, "reason": reason, "version": won.version + 1,
                 "refresh_lease_until": None, "refresh_lease_owner": None}
        if await self._store.patch_tokens(target.toolset_id, patch, where=fence) is None:
            newer = await self._newer_token(target, won)
            if newer is not None:
                return newer
        else:
            logger.warning("MCP toolset %s lost its sign-in: %s", target.toolset_id, reason)
        raise McpSignInRequiredError(target.toolset_id, reason=reason)

    async def _kept_after_outage(self, target: OAuthTarget, won: McpOAuthTokenSet, owner: str, detail: str) -> str:
        """The token endpoint could not answer: keep the tokens, mark the state ``error``, release the lease and raise a temporary
        :class:`~primer.model.except_.ProviderError`; when the row moved on meanwhile, a newer connected token is the answer instead."""
        reason = f"The authorization server could not be reached: {detail}"
        kept = await self._store.patch_tokens(
            target.toolset_id,
            {"state": "error", "reason": reason, "refresh_lease_until": None, "refresh_lease_owner": None},
            where={"refresh_lease_owner": [owner]},
        )
        if kept is None:
            newer = await self._newer_token(target, won)
            if newer is not None:
                return newer
        logger.warning("MCP toolset %s could not refresh its sign-in, so its tokens are kept: %s", target.toolset_id, reason)
        raise ProviderError(f"the MCP server {target.toolset_id} could not refresh its sign-in: {reason}")

    async def _newer_token(self, target: OAuthTarget, won: McpOAuthTokenSet) -> str | None:
        """A connected token stored after ``won`` was leased (a reconnect, a holder that took over), or ``None``. Raises when the sign-in is gone."""
        row = self._require_usable(target, await self._store.get_tokens(target.toolset_id))
        if row.version > won.version and row.state == "connected":
            return self._readable_access(row)
        return None

    @staticmethod
    def _require_usable(target: OAuthTarget, row: McpOAuthTokenSet | None) -> McpOAuthTokenSet:
        """``row`` when the toolset can use it; else :class:`~primer.model.mcp_oauth.McpSignInRequiredError`: no row, settings that no longer hash to
        the row's fingerprint (the token is never sent to a URL it was not issued for), or a signed-out row."""
        if row is None:
            raise McpSignInRequiredError(target.toolset_id, reason=NOT_CONNECTED)
        if row.config_fingerprint != config_fingerprint(target):
            raise McpSignInRequiredError(target.toolset_id, reason=SETTINGS_CHANGED)
        if row.state == "needs_sign_in":
            raise McpSignInRequiredError(target.toolset_id, reason=row.reason or NOT_CONNECTED)
        return row

    def _readable_access(self, row: McpOAuthTokenSet) -> str | None:
        """The row's access token. One the current key cannot open fails THIS call with :data:`KEY_CHANGED` and leaves the row as it is.

        Lead ruling on audit G4-10: the row is shared by every process, and a process whose key differs (a worker with another or no
        ``PRIMER_ENCRYPTION_KEY``, a rolling deploy that introduces the key, the loser of a fresh database's session-secret race) used to wipe the
        tokens for all of them on first use, and again after every reconnect. Now it fails alone and says so in an ERROR line; only an admin
        Disconnect or Reconnect replaces the row.
        """
        try:
            return self._store.unseal(row.access_token_enc)
        except SecretBoxError:
            self._key_differs(row.id or "")
            raise McpSignInRequiredError(row.id or "", reason=KEY_CHANGED) from None

    @staticmethod
    def _key_differs(toolset_id: str) -> None:
        logger.error(
            "MCP toolset %s: this process cannot read the toolset's stored sign-in because its encryption key differs from the key that sealed it "
            "(PRIMER_ENCRYPTION_KEY must be the same on every API and worker process); the call fails here and the sign-in is left in place for the "
            "processes that can read it",
            toolset_id,
        )

    async def _sign_out(self, row: McpOAuthTokenSet, reason: str) -> None:
        """Drop the tokens of ``row`` (read without a lease) with ``reason``, unless the row changed or was leased since it was read."""
        signed_out = await self._store.patch_tokens(
            row.id,
            {"access_token_enc": None, "refresh_token_enc": None, "state": "needs_sign_in", "reason": reason, "version": row.version + 1},
            where={"version": [raw_generation(row, "version")], "refresh_lease_until": [raw_generation(row, "refresh_lease_until")]},
        )
        if signed_out is not None:
            logger.warning("MCP toolset %s lost its sign-in: %s", row.id, reason)

    async def _shielded(self, work: Coroutine[Any, Any, str | None]) -> str | None:
        """Run ``work`` (a lease and its exchange) to its end even when the caller is cancelled: an exchange the token endpoint already answered with a
        rotated refresh token must be stored, or the next refresh would spend a dead token and sign the toolset out. The caller's cancellation still
        propagates at once; the work's outcome is in the store."""
        task = asyncio.create_task(work)
        self._inflight.add(task)
        task.add_done_callback(self._settled)
        return await asyncio.shield(task)

    def _settled(self, task: asyncio.Task[Any]) -> None:
        self._inflight.discard(task)
        if not task.cancelled():
            task.exception()                            # retrieved: the outcome of an exchange whose caller left is in the store, not lost

    def _lock_for(self, toolset_id: str) -> asyncio.Lock:
        lock = self._locks.get(toolset_id)
        if lock is None:
            lock = self._locks[toolset_id] = asyncio.Lock()
        return lock

    # ---- helpers --------------------------------------------------------------------------------------------------------------------------------------------------------

    async def _discover(self, target: OAuthTarget) -> Discovery:
        challenge = await protocol.probe_challenge(target.server_url, headers=target.headers, http=self._http)
        return await protocol.discover(target.server_url, www_authenticate=challenge, http=self._http, resource_uri=target.oauth.resource_uri)

    @staticmethod
    def _require_resource_on_the_server(target: OAuthTarget) -> None:
        """Refuse a ``resource_uri`` that is not on the MCP server's origin (audit G4-07): every write refuses one
        (:func:`primer.toolset.toolset_checks.check_oauth_resource_uri`), and this keeps a row stored before that rule from starting a sign-in."""
        resource = target.oauth.resource_uri
        if resource is not None and (origin_of(resource)[0] != "url" or origin_of(resource) != origin_of(target.server_url)):
            raise BadRequestError(
                f"the MCP toolset {target.toolset_id} names an oauth.resource_uri that is not on the MCP server's own origin (scheme, host and port), "
                "so no token is requested for it: edit the toolset"
            )

    @staticmethod
    def _scopes(target: OAuthTarget, found: Discovery) -> list[str]:
        """The configured scopes, else the scope the server's challenge named, else the protected resource's ``scopes_supported``, else none."""
        if target.oauth.scopes:
            return list(target.oauth.scopes)
        if found.challenged_scope:
            return found.challenged_scope.split()
        if found.protected_resource is not None and found.protected_resource.scopes_supported:
            return list(found.protected_resource.scopes_supported)
        return []

    @staticmethod
    def _resource(target: OAuthTarget, found: Discovery) -> str | None:
        """The resource indicator: the configured one, else the server's full URL, and only for a server that publishes protected-resource metadata."""
        if not found.uses_resource_indicator:
            return None
        return target.oauth.resource_uri or protocol.canonical_resource(target.server_url)

    async def _sign_in_client(self, target: OAuthTarget, found: Discovery, *, redirect_uri: str, now: datetime) -> tuple[ClientAuth, ClientSource]:
        """The client a browser sign-in uses: the toolset's static client, else its registration when the issuer and redirect URI still match (and it
        can be read), else a new dynamic registration, which replaces the old one."""
        metadata = found.metadata
        static = target.oauth.static_client
        if static is not None:
            return protocol.client_auth_for(metadata, static.client_id, _static_secret(target, static.client_id)), "static"
        registered = await self._store.get_client(target.toolset_id)
        if registered is not None and registered.issuer == metadata.issuer and registered.redirect_uri == redirect_uri:
            try:
                secret = self._store.unseal(registered.client_secret_enc)
            except SecretBoxError:
                logger.warning("MCP toolset %s: its client registration cannot be read with the current encryption key; registering again", target.toolset_id)
            else:
                return protocol.client_auth_for(metadata, registered.client_id, secret), "registered"
        made = await protocol.register_client(metadata=metadata, redirect_uri=redirect_uri, client_name=target.oauth.client_name, http=self._http)
        await self._store.put_client(McpOAuthClient(
            id=target.toolset_id,
            issuer=metadata.issuer,
            redirect_uri=redirect_uri,
            client_id=made.client_id,
            client_secret_enc=self._store.seal(made.client_secret) if made.client_secret else None,
            registered_at=now,
        ))
        logger.info("MCP toolset %s: registered a client at its authorization server", target.toolset_id)
        return protocol.client_auth_for(metadata, made.client_id, made.client_secret), "registered"

    async def _client_of(
        self,
        target: OAuthTarget | None,
        *,
        toolset_id: str,
        client_id: str,
        source: ClientSource,
        method: ClientAuthMethod | None,
    ) -> ClientAuth:
        """How a stored sign-in's client authenticates now: a static client with the toolset's current secret, a registered one with the secret its
        registration holds (raises :class:`~primer.common.secret_box.SecretBoxError` when that cannot be read)."""
        if source == "static":
            return _client_auth(client_id, _static_secret(target, client_id), method)
        registered = await self._store.get_client(toolset_id)
        secret = self._store.unseal(registered.client_secret_enc) if registered is not None and registered.client_id == client_id else None
        return _client_auth(client_id, secret, method)

    async def _revoke(self, target: OAuthTarget | None, row: McpOAuthTokenSet) -> bool:
        """Revoke the row's refresh token, else its access token, where the server advertised revocation. Best effort: ``False`` for anything but a
        2xx, and when the tokens or the client secret cannot be read."""
        if not row.revocation_endpoint:
            return False
        try:
            refresh = self._store.unseal(row.refresh_token_enc)
            access = self._store.unseal(row.access_token_enc)
            client = await self._client_of(target, toolset_id=row.id, client_id=row.client_id, source=row.client_source,
                                           method=row.token_endpoint_auth_method)
        except SecretBoxError:
            return False
        token, hint = (refresh, "refresh_token") if refresh else (access, "access_token")
        if not token:
            return False
        return await protocol.revoke(revocation_endpoint=row.revocation_endpoint, client=client, token=token, token_type_hint=hint, http=self._http)
