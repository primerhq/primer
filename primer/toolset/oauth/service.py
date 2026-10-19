"""One shared OAuth sign-in per MCP toolset (plan 07, Tasks 7 and 8; design 12.3 to 12.6 and 20 Q2).

A toolset that signs in with OAuth has ONE sign-in, owned by the toolset and used by every agent and caller of it. :class:`ToolsetOAuthService` (one
per server process) is the only code that starts, finishes, uses or ends one:

* **Browser sign-in** (``authorization_code``). :meth:`~ToolsetOAuthService.start_sign_in` discovers the authorization server
  (:mod:`primer.toolset.oauth.protocol`), refuses one without PKCE S256, picks the client (the toolset's static client, else its earlier registration
  when the issuer and redirect URI still match, else a new dynamic registration), stores a pending sign-in (the PKCE verifier sealed, single use, ten
  minutes) and returns the authorize URL. :meth:`~ToolsetOAuthService.complete_sign_in` takes the pending sign-in back by its ``state`` and exchanges the
  code. A callback whose state is unknown, used, expired or another toolset's ends as ``invalid_state``; one the provider declined as
  ``access_denied``; one for a toolset whose URL or OAuth settings changed since the start as ``settings_changed``; a refused or failed code exchange as
  ``exchange_failed``. None of them writes a token.
* **Client credentials**. :meth:`~ToolsetOAuthService.connect_client_credentials` exchanges the toolset's client id and secret at once, at the
  configured token endpoint or the discovered one.
* **Disconnect** revokes the refresh token (else the access token) where the server advertises RFC 7009 revocation, best effort, and deletes the tokens
  whatever the answer. :meth:`~ToolsetOAuthService.forget` removes everything stored for a toolset (its deletion, a change of its settings).
* **Status** (:meth:`~ToolsetOAuthService.status`) says ``connected``, ``needs_sign_in`` or ``error`` with the reason and expiry, or ``not_applicable``
  for a toolset without OAuth. It never carries a token.

The tokens are bound to the settings they were obtained for: :func:`config_fingerprint` hashes the URL and the OAuth settings, and a toolset whose
settings no longer hash to the stored value has no usable sign-in, so a token is never sent to a URL it was not issued for.

Nothing here logs a token, a code, a PKCE verifier, a ``state`` or a client secret: log lines name the toolset, HTTP statuses and RFC 6749 error codes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from primer.common.error_codes import safe_code
from primer.common.secret_box import SecretBox, SecretBoxError
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import BadRequestError, ConfigError, PrimerError
from primer.model.mcp_oauth import ClientAuthMethod, ClientSource, McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet
from primer.model.provider import HttpConfig, McpConfig, OAuthConfig, Toolset, ToolsetProviderType, TransportType
from primer.toolset.oauth import protocol
from primer.toolset.oauth.protocol import ClientAuth, Discovery
from primer.toolset.oauth.store import McpOAuthStore

__all__ = [
    "EXPIRED_NO_REFRESH",
    "HTTP_TIMEOUT_S",
    "KEY_CHANGED",
    "NOT_CONNECTED",
    "PENDING_TTL",
    "SETTINGS_CHANGED",
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

NOT_CONNECTED = "Not connected."
SETTINGS_CHANGED = "The URL or OAuth settings changed since this toolset was connected."
KEY_CHANGED = "The stored sign-in cannot be read with the current encryption key."
EXPIRED_NO_REFRESH = "The access token expired and the server issued no refresh token."


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    """A browser sign-in did not complete. ``code`` says why: ``invalid_state`` (unknown, used, expired or another toolset's state),
    ``access_denied`` (declined at the provider), ``exchange_failed`` (the provider refused or could not exchange the code) or ``settings_changed``
    (the toolset was edited, removed or lost its OAuth settings during the sign-in). No token was written."""

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
        self._http = http if http is not None else httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=False)
        self._clock = clock
        self._owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"

    @property
    def store(self) -> McpOAuthStore:
        """The store the service reads and writes (sealing with the service's key)."""
        return self._store

    async def aclose(self) -> None:
        """Close the HTTP client the service made (one handed in is the caller's to close)."""
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

    async def start_sign_in(self, target: OAuthTarget, *, redirect_uri: str, started_by: str | None) -> SignInStart:
        """Start a browser sign-in of ``target``: the authorize URL to open, valid for :data:`PENDING_TTL`.

        Raises :class:`~primer.model.except_.BadRequestError` when the toolset uses client credentials, the authorization server cannot be discovered,
        does not advertise PKCE S256, has no authorization endpoint, or (without a configured client) refuses a registration; and
        :class:`~primer.model.except_.NetworkError` when it cannot be reached.
        """
        if target.oauth.grant != "authorization_code":
            raise BadRequestError(f"the MCP toolset {target.toolset_id} signs in with client credentials, so it connects without a browser sign-in")
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
    ) -> ToolsetAuthStatus:
        """Finish the browser sign-in that the provider's redirect for ``toolset_id`` carries: exchange the code and store the tokens.

        ``toolset`` is the toolset as it is NOW (``None`` when it no longer exists). Raises :class:`SignInError`; every failure leaves the stored tokens
        as they were, and the state is spent whatever the outcome.
        """
        now = self._clock()
        pending = await self._store.take_pending(state, now=now) if state else None
        if pending is None or pending.toolset_id != toolset_id:
            logger.info("MCP toolset %s: a sign-in callback carried an unknown, used, expired or misdirected state", toolset_id)
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

    async def connect_client_credentials(self, target: OAuthTarget, *, connected_by: str | None) -> ToolsetAuthStatus:
        """Connect a client-credentials toolset at once: exchange its client id and secret at its token endpoint (the configured one, else the
        discovered one) and store the token. Raises :class:`~primer.model.except_.BadRequestError` when the token endpoint refuses the credentials
        (nothing is stored), or for a toolset that does not use client credentials."""
        oauth = target.oauth
        static = oauth.static_client
        secret = _static_secret(target, static.client_id) if static is not None else None
        if oauth.grant != "client_credentials" or static is None or not secret:
            raise BadRequestError(f"the MCP toolset {target.toolset_id} does not sign in with client credentials (a client id and secret)")
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
        """End the toolset's sign-in: revoke it where the server supports revocation (best effort), then delete the tokens whatever the answer."""
        assert toolset.id is not None
        row = await self._store.get_tokens(toolset.id)
        if row is not None:
            revoked = await self._revoke(OAuthTarget.from_toolset(toolset), row)
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

    # ---- helpers --------------------------------------------------------------------------------------------------------------------------------------------------------

    async def _discover(self, target: OAuthTarget) -> Discovery:
        challenge = await protocol.probe_challenge(target.server_url, headers=target.headers, http=self._http)
        return await protocol.discover(target.server_url, www_authenticate=challenge, http=self._http)

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
