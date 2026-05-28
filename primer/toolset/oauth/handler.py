"""High-level OAuth orchestrator for :class:`McpToolsetProvider`.

Owns one TokenStore + StateStore + ClientCredentialsCache + httpx
client per provider instance. Exposes two operations that the provider
calls into:

* :meth:`PrimerOAuthHandler.authorize` -- preflight before opening an
  MCP session. Returns an ``Authorization`` header dict OR raises
  :class:`primer.model.except_.AuthRequiredError`.
* :meth:`PrimerOAuthHandler.complete_oauth` -- called from the
  application's OAuth callback. Exchanges code+state for a token,
  persists it, returns. Subsequent ``authorize(principal)`` calls
  succeed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import httpx

from primer.model.except_ import (
    AuthenticationError,
    AuthRequiredError,
    BadRequestError,
    PrimerError,
)
from primer.model.provider import OAuthConfig
from primer.toolset.oauth.discovery import (
    build_authorization_url,
    exchange_code,
    negotiate,
    pkce_pair,
    refresh_token as refresh_token_modern,
)
from primer.toolset.oauth.legacy import (
    build_authorization_url_legacy,
    discover_legacy,
    exchange_code_legacy,
    refresh_token_legacy,
)
from primer.toolset.oauth.registration import (
    ClientCredentialsCache,
    InMemoryClientCredentialsCache,
    resolve as resolve_credentials,
)
from primer.toolset.oauth.state import (
    InMemoryStateStore,
    OAuthState,
    StateStore,
)
from primer.toolset.oauth.token_store import (
    InMemoryTokenStore,
    TokenRecord,
    TokenStore,
)


logger = logging.getLogger(__name__)


_STATE_TTL = timedelta(minutes=10)


def _origin(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


class PrimerOAuthHandler:
    """Single-point OAuth orchestrator. See module docstring."""

    def __init__(
        self,
        oauth_config: OAuthConfig,
        mcp_url: str,
        toolset_id: str,
        token_store: TokenStore | None = None,
        state_store: StateStore | None = None,
        client_cache: ClientCredentialsCache | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = oauth_config
        self._mcp_url = mcp_url
        self._toolset_id = toolset_id
        self._token_store = token_store or InMemoryTokenStore()
        self._state_store = state_store or InMemoryStateStore()
        self._client_cache = client_cache or InMemoryClientCredentialsCache()
        self._http = http
        self._owns_http = http is None

    async def authorize(self, *, principal: str | None) -> dict[str, str]:
        """Return the Authorization header dict, or raise AuthRequiredError."""
        key = self._cache_key(principal)
        # Snapshot the raw record before calling get() so we can recover a
        # refresh_token even if get() evicts the expired entry.
        stale = self._stale_record(key)
        rec = await self._token_store.get(key)
        if rec is not None:
            return {
                "Authorization": f"{rec.token_type} {rec.access_token.get_secret_value()}"
            }

        if stale is not None and stale.refresh_token is not None:
            try:
                new_rec = await self._refresh(stale)
            except PrimerError:
                logger.info(
                    "Refresh failed for principal %r; reissuing AuthRequiredError",
                    principal,
                )
                await self._token_store.delete(key)
            else:
                await self._token_store.set(key, new_rec)
                return {
                    "Authorization": f"{new_rec.token_type} {new_rec.access_token.get_secret_value()}"
                }

        await self._raise_auth_required(principal=principal)
        raise AssertionError("unreachable")

    async def complete_oauth(self, *, code: str, state_id: str) -> None:
        """Exchange code for a token and persist."""
        state = await self._state_store.take(state_id)
        spec = state.spec_version
        http = await self._get_http()
        if spec == "2025-03-26":
            metadata = await discover_legacy(self._mcp_url, http)
        else:
            _, metadata = await negotiate(
                mcp_url=self._mcp_url,
                forced=spec,
                http=http,
            )
        client = await resolve_credentials(
            metadata=metadata,
            static=self._config.static_client,
            redirect_uri=str(self._config.redirect_uri),
            client_name=self._config.client_name,
            cache=self._client_cache,
            http=http,
        )

        try:
            if spec == "2025-03-26":
                rec = await exchange_code_legacy(
                    metadata=metadata,
                    client=client,
                    code=code,
                    redirect_uri=str(self._config.redirect_uri),
                    pkce_verifier=state.code_verifier.get_secret_value(),
                    http=http,
                )
            else:
                rec = await exchange_code(
                    metadata=metadata,
                    client=client,
                    code=code,
                    redirect_uri=str(self._config.redirect_uri),
                    pkce_verifier=state.code_verifier.get_secret_value(),
                    resource_uri=self._effective_resource(),
                    spec_version=spec,
                    http=http,
                )
        except BadRequestError as exc:
            raise AuthenticationError(
                f"OAuth token endpoint refused code exchange: {exc.message}",
                status_code=exc.status_code,
                cause=exc,
            ) from exc

        await self._token_store.set(self._cache_key(state.principal), rec)

    def _cache_key(self, principal: str | None) -> str:
        bucket = _origin(self._mcp_url)
        return f"{bucket}|{principal or ''}|{self._toolset_id}"

    def _stale_record(self, key: str) -> TokenRecord | None:
        store = getattr(self._token_store, "_store", None)
        if store is None or key not in store:
            return None
        return store[key]

    def _effective_resource(self) -> str | None:
        return self._config.resource_uri or _origin(self._mcp_url)

    async def _refresh(self, rec: TokenRecord) -> TokenRecord:
        http = await self._get_http()
        version, metadata = await negotiate(
            mcp_url=self._mcp_url,
            forced=self._config.spec_version,
            http=http,
        )
        client = await resolve_credentials(
            metadata=metadata,
            static=self._config.static_client,
            redirect_uri=str(self._config.redirect_uri),
            client_name=self._config.client_name,
            cache=self._client_cache,
            http=http,
        )
        if version == "2025-03-26":
            return await refresh_token_legacy(
                metadata=metadata,
                client=client,
                refresh_token=rec.refresh_token.get_secret_value(),
                scopes=self._config.scopes,
                http=http,
            )
        return await refresh_token_modern(
            metadata=metadata,
            client=client,
            refresh_token=rec.refresh_token.get_secret_value(),
            scopes=self._config.scopes,
            resource_uri=self._effective_resource(),
            spec_version=version,
            http=http,
        )

    async def _raise_auth_required(self, *, principal: str | None) -> None:
        http = await self._get_http()
        version, metadata = await negotiate(
            mcp_url=self._mcp_url,
            forced=self._config.spec_version,
            http=http,
        )
        client = await resolve_credentials(
            metadata=metadata,
            static=self._config.static_client,
            redirect_uri=str(self._config.redirect_uri),
            client_name=self._config.client_name,
            cache=self._client_cache,
            http=http,
        )

        verifier, challenge = pkce_pair()
        state = OAuthState(
            principal=principal,
            toolset_id=self._toolset_id,
            code_verifier=verifier,
            spec_version=version,
            auth_server_metadata_url=str(metadata.issuer)
            + "/.well-known/oauth-authorization-server",
            issued_at=datetime.now(timezone.utc),
        )
        state_id = await self._state_store.put(state, ttl=_STATE_TTL)

        if version == "2025-03-26":
            url = build_authorization_url_legacy(
                metadata=metadata,
                client=client,
                redirect_uri=str(self._config.redirect_uri),
                scopes=self._config.scopes,
                pkce_challenge=challenge,
                state_id=state_id,
            )
        else:
            url = build_authorization_url(
                metadata=metadata,
                client=client,
                redirect_uri=str(self._config.redirect_uri),
                scopes=self._config.scopes,
                resource_uri=self._effective_resource(),
                pkce_challenge=challenge,
                state_id=state_id,
                spec_version=version,
            )

        raise AuthRequiredError(
            f"OAuth consent required for toolset {self._toolset_id!r}",
            auth_url=url,
            state=state_id,
        )

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
