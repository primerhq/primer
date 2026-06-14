"""OAuth metadata discovery, PKCE, authorization-URL build, token exchange.

Modern path (2025-06-18 + 2025-11-25). The 2025-03-26 legacy variant
lives in :mod:`primer.toolset.oauth.legacy` -- the wire shapes diverge
enough (no protected-resource indirection, no ``resource`` parameter)
that splitting the path keeps each side small enough to read in one
sitting.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from pydantic import BaseModel, HttpUrl

from primer.common.mcp_errors import classify_mcp_exception
from primer.model.except_ import BadRequestError
from primer.model.provider import OAuthClientCredentials
from primer.toolset.oauth.token_store import TokenRecord


logger = logging.getLogger(__name__)


SpecVersion = Literal["2025-03-26", "2025-06-18", "2025-11-25"]


class AuthServerMetadata(BaseModel):
    """Subset of the RFC 8414 authorization-server metadata document."""

    issuer: HttpUrl
    authorization_endpoint: HttpUrl
    token_endpoint: HttpUrl
    registration_endpoint: HttpUrl | None = None
    scopes_supported: list[str] | None = None
    code_challenge_methods_supported: list[str] | None = None


def pkce_pair() -> tuple[str, str]:
    """Generate a fresh PKCE (verifier, challenge) pair using S256.

    Verifier is 32 random bytes encoded base64url (43 chars after stripping
    padding). Challenge is base64url(sha256(verifier)).
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode(
        "ascii"
    )
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _origin(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


def _protected_resource_urls(mcp_url: str) -> list[str]:
    """Candidate RFC 9728 protected-resource metadata URLs, in priority order.

    RFC 9728 section 3.1 inserts ``/.well-known/oauth-protected-resource``
    between the host and the resource's path component, so a resource at
    ``https://host/mcp`` advertises its metadata at
    ``https://host/.well-known/oauth-protected-resource/mcp`` -- NOT at the
    bare origin. (The bare-origin form is only correct for a resource with
    no path.) We try the spec's path-aware form first, then fall back to the
    origin form for servers that only serve the doc there.
    """
    origin = _origin(mcp_url)
    urls: list[str] = []
    path = urlparse(mcp_url).path.rstrip("/")
    if path:
        urls.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return urls


async def _fetch_protected_resource(
    mcp_url: str, http: httpx.AsyncClient
) -> dict | None:
    for url in _protected_resource_urls(mcp_url):
        try:
            resp = await http.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        if not isinstance(data, dict) or "authorization_servers" not in data:
            continue
        return data
    return None


async def _fetch_auth_server_metadata(
    issuer_url: str, http: httpx.AsyncClient
) -> AuthServerMetadata | None:
    url = _origin(issuer_url) + "/.well-known/oauth-authorization-server"
    try:
        resp = await http.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return AuthServerMetadata.model_validate(resp.json())
    except (ValueError, TypeError):
        return None


async def negotiate(
    mcp_url: str,
    forced: SpecVersion | None,
    http: httpx.AsyncClient,
) -> tuple[SpecVersion, AuthServerMetadata]:
    """Probe the MCP server, return (spec_version, auth-server metadata)."""
    if forced == "2025-03-26":
        meta = await _fetch_auth_server_metadata(_origin(mcp_url), http)
        if meta is None:
            raise BadRequestError(
                f"MCP server at {mcp_url!r} does not advertise legacy OAuth metadata"
            )
        return ("2025-03-26", meta)

    if forced in ("2025-06-18", "2025-11-25"):
        pr = await _fetch_protected_resource(mcp_url, http)
        if pr is None:
            raise BadRequestError(
                f"MCP server at {mcp_url!r} does not advertise protected-resource metadata"
            )
        servers = pr.get("authorization_servers") or []
        if not servers:
            raise BadRequestError(
                "protected-resource document missing authorization_servers"
            )
        meta = await _fetch_auth_server_metadata(servers[0], http)
        if meta is None:
            raise BadRequestError(
                f"auth server {servers[0]!r} does not advertise OAuth metadata"
            )
        return (forced, meta)

    pr = await _fetch_protected_resource(mcp_url, http)
    if pr is not None:
        servers = pr.get("authorization_servers") or []
        if servers:
            meta = await _fetch_auth_server_metadata(servers[0], http)
            if meta is not None:
                return ("2025-06-18", meta)

    legacy_meta = await _fetch_auth_server_metadata(_origin(mcp_url), http)
    if legacy_meta is not None:
        return ("2025-03-26", legacy_meta)

    raise BadRequestError(
        f"MCP server at {mcp_url!r} does not advertise any OAuth metadata"
    )


def build_authorization_url(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    redirect_uri: str,
    scopes: list[str],
    resource_uri: str | None,
    pkce_challenge: str,
    state_id: str,
    spec_version: SpecVersion,
) -> str:
    """Build the authorization URL the user is redirected to for consent."""
    params: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", client.client_id),
        ("redirect_uri", redirect_uri),
        ("state", state_id),
        ("code_challenge", pkce_challenge),
        ("code_challenge_method", "S256"),
    ]
    if scopes:
        params.append(("scope", " ".join(scopes)))
    if spec_version != "2025-03-26" and resource_uri:
        params.append(("resource", resource_uri))

    base = str(metadata.authorization_endpoint)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def _parse_token_response(data: dict) -> TokenRecord:
    expires_in = data.get("expires_in", 3600)
    return TokenRecord(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)),
        token_type=data.get("token_type", "Bearer"),
    )


async def _post_token(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    body: dict[str, str],
    http: httpx.AsyncClient,
) -> TokenRecord:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth: tuple[str, str] | None = None
    if client.client_secret is not None:
        auth = (client.client_id, client.client_secret.get_secret_value())
    else:
        body = {**body, "client_id": client.client_id}

    try:
        resp = await http.post(
            str(metadata.token_endpoint),
            data=body,
            headers=headers,
            auth=auth,
        )
    except httpx.HTTPError as exc:
        raise classify_mcp_exception(exc) from exc

    if resp.status_code >= 400:
        if 400 <= resp.status_code < 500:
            raise BadRequestError(
                f"token endpoint returned {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise classify_mcp_exception(exc) from exc

    return _parse_token_response(resp.json())


async def exchange_code(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    code: str,
    redirect_uri: str,
    pkce_verifier: str,
    resource_uri: str | None,
    spec_version: SpecVersion,
    http: httpx.AsyncClient,
) -> TokenRecord:
    """Trade an authorization code for a TokenRecord."""
    body: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": pkce_verifier,
    }
    if spec_version != "2025-03-26" and resource_uri:
        body["resource"] = resource_uri
    return await _post_token(metadata=metadata, client=client, body=body, http=http)


async def refresh_token(
    *,
    metadata: AuthServerMetadata,
    client: OAuthClientCredentials,
    refresh_token: str,
    scopes: list[str],
    resource_uri: str | None,
    spec_version: SpecVersion,
    http: httpx.AsyncClient,
) -> TokenRecord:
    """Use a refresh_token to mint a new TokenRecord."""
    body: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if scopes:
        body["scope"] = " ".join(scopes)
    if spec_version != "2025-03-26" and resource_uri:
        body["resource"] = resource_uri
    return await _post_token(metadata=metadata, client=client, body=body, http=http)
