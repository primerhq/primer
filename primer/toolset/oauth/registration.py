"""Dynamic Client Registration + static-credential resolver.

When the operator supplies static credentials on
:attr:`primer.model.provider.OAuthConfig.static_client`, DCR is
skipped entirely. Otherwise we POST to the auth server's
``registration_endpoint`` (RFC 7591) with a minimum payload, cache
the response, and return it. The cache key is
``(issuer, redirect_uri, client_name)`` -- different applications
hitting the same auth server get distinct registrations.

DCR-registered clients are always public (PKCE only,
``token_endpoint_auth_method=none``). Operators who need a
confidential client should use :class:`OAuthClientCredentials` with
both id and secret.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from pydantic import BaseModel

from primer.common.mcp_errors import classify_mcp_exception
from primer.model.except_ import BadRequestError, ConfigError
from primer.model.provider import OAuthClientCredentials
from primer.toolset.oauth.discovery import AuthServerMetadata


logger = logging.getLogger(__name__)


class ClientCredentialsCache(Protocol):
    async def get(self, key: str) -> OAuthClientCredentials | None: ...
    async def set(
        self,
        key: str,
        creds: OAuthClientCredentials,
        ttl: timedelta,
    ) -> None: ...


class _CacheEntry(BaseModel):
    creds: OAuthClientCredentials
    expires_at: datetime


class InMemoryClientCredentialsCache:
    """Process-local cache. 24-hour default TTL is a reasonable starting point."""

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> OAuthClientCredentials | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= datetime.now(timezone.utc):
                del self._store[key]
                return None
            return entry.creds

    async def set(
        self,
        key: str,
        creds: OAuthClientCredentials,
        ttl: timedelta,
    ) -> None:
        async with self._lock:
            self._store[key] = _CacheEntry(
                creds=creds,
                expires_at=datetime.now(timezone.utc) + ttl,
            )


_DEFAULT_CACHE_TTL = timedelta(hours=24)


def _cache_key(issuer: str, redirect_uri: str, client_name: str) -> str:
    return f"{issuer}|{redirect_uri}|{client_name}"


async def _post_dcr(
    *,
    metadata: AuthServerMetadata,
    redirect_uri: str,
    client_name: str,
    http: httpx.AsyncClient,
) -> OAuthClientCredentials:
    payload = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        resp = await http.post(str(metadata.registration_endpoint), json=payload)
    except httpx.HTTPError as exc:
        raise classify_mcp_exception(exc) from exc

    if resp.status_code >= 400:
        if 400 <= resp.status_code < 500:
            raise BadRequestError(
                f"DCR endpoint returned {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise classify_mcp_exception(exc) from exc

    data = resp.json()
    return OAuthClientCredentials(
        client_id=data["client_id"],
        client_secret=data.get("client_secret"),
    )


async def resolve(
    *,
    metadata: AuthServerMetadata,
    static: OAuthClientCredentials | None,
    redirect_uri: str,
    client_name: str,
    cache: ClientCredentialsCache,
    http: httpx.AsyncClient,
) -> OAuthClientCredentials:
    """Return usable client credentials for an auth server."""
    if static is not None:
        return static

    if metadata.registration_endpoint is None:
        raise ConfigError(
            "MCP server's auth server does not support Dynamic Client Registration "
            "and OAuthConfig.static_client was not provided"
        )

    key = _cache_key(str(metadata.issuer), redirect_uri, client_name)
    cached = await cache.get(key)
    if cached is not None:
        logger.debug("Reusing cached DCR registration for %s", key)
        return cached

    creds = await _post_dcr(
        metadata=metadata,
        redirect_uri=redirect_uri,
        client_name=client_name,
        http=http,
    )
    await cache.set(key, creds, ttl=_DEFAULT_CACHE_TTL)
    logger.info("Registered new OAuth client via DCR: %s", creds.client_id)
    return creds
