"""OAuth flow for the legacy 2025-03-26 MCP authorization spec.

In this version the MCP server itself acts as the auth server -- there
is no protected-resource indirection -- and the ``resource`` parameter
(RFC 8707) is not used. Aside from those two differences, everything
else is identical to the modern flow in
:mod:`primer.toolset.oauth.discovery`, so this module is mostly a thin
layer over the same helpers.
"""

from __future__ import annotations

import httpx

from primer.model.except_ import BadRequestError
from primer.model.provider import OAuthClientCredentials
from primer.toolset.oauth.discovery import (
    AuthServerMetadata,
    _fetch_auth_server_metadata,
    build_authorization_url,
    exchange_code,
)
from primer.toolset.oauth.discovery import refresh_token as _refresh_token
from primer.toolset.oauth.token_store import TokenRecord


async def discover_legacy(
    mcp_url: str,
    http: httpx.AsyncClient,
) -> AuthServerMetadata:
    """Fetch the auth-server metadata directly from the MCP origin."""
    meta = await _fetch_auth_server_metadata(mcp_url, http)
    if meta is None:
        raise BadRequestError(
            f"MCP server at {mcp_url!r} does not advertise legacy OAuth metadata"
        )
    return meta


def build_authorization_url_legacy(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    redirect_uri: str,
    scopes: list[str],
    pkce_challenge: str,
    state_id: str,
) -> str:
    return build_authorization_url(
        metadata=metadata,
        client=client,
        redirect_uri=redirect_uri,
        scopes=scopes,
        resource_uri=None,
        pkce_challenge=pkce_challenge,
        state_id=state_id,
        spec_version="2025-03-26",
    )


async def exchange_code_legacy(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    code: str,
    redirect_uri: str,
    pkce_verifier: str,
    http: httpx.AsyncClient,
) -> TokenRecord:
    return await exchange_code(
        metadata=metadata,
        client=client,
        code=code,
        redirect_uri=redirect_uri,
        pkce_verifier=pkce_verifier,
        resource_uri=None,
        spec_version="2025-03-26",
        http=http,
    )


async def refresh_token_legacy(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    refresh_token: str,
    scopes: list[str],
    http: httpx.AsyncClient,
) -> TokenRecord:
    return await _refresh_token(
        metadata=metadata,
        client=client,
        refresh_token=refresh_token,
        scopes=scopes,
        resource_uri=None,
        spec_version="2025-03-26",
        http=http,
    )
