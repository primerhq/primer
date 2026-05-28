"""Tests for primer.toolset.oauth.legacy (2025-03-26 spec)."""

from __future__ import annotations

import httpx
import pytest
import respx

from primer.model.except_ import BadRequestError
from primer.model.provider import OAuthClientCredentials
from primer.toolset.oauth.discovery import AuthServerMetadata
from primer.toolset.oauth.legacy import (
    build_authorization_url_legacy,
    discover_legacy,
    exchange_code_legacy,
    refresh_token_legacy,
)


_AUTH_DOC = {
    "issuer": "https://mcp.example",
    "authorization_endpoint": "https://mcp.example/auth",
    "token_endpoint": "https://mcp.example/token",
}


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


class TestDiscoverLegacy:
    @respx.mock
    async def test_fetches_auth_metadata_directly_from_mcp_origin(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_DOC)
        )
        meta = await discover_legacy("https://mcp.example/mcp", http_client)
        assert str(meta.token_endpoint) == "https://mcp.example/token"

    @respx.mock
    async def test_404_raises_bad_request(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(BadRequestError):
            await discover_legacy("https://mcp.example/mcp", http_client)


class TestBuildAuthUrlLegacy:
    def test_omits_resource_param(self) -> None:
        from urllib.parse import parse_qs, urlparse

        meta = AuthServerMetadata(**_AUTH_DOC)
        client = OAuthClientCredentials(client_id="abc")
        url = build_authorization_url_legacy(
            metadata=meta,
            client=client,
            redirect_uri="https://app.example/cb",
            scopes=["read"],
            pkce_challenge="ch",
            state_id="s",
        )
        q = parse_qs(urlparse(url).query)
        assert "resource" not in q
        assert q["client_id"] == ["abc"]
        assert q["code_challenge_method"] == ["S256"]


class TestExchangeCodeLegacy:
    @respx.mock
    async def test_token_request_omits_resource(
        self, http_client: httpx.AsyncClient
    ) -> None:
        route = respx.post("https://mcp.example/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        )
        meta = AuthServerMetadata(**_AUTH_DOC)
        client = OAuthClientCredentials(client_id="abc")
        await exchange_code_legacy(
            metadata=meta,
            client=client,
            code="c",
            redirect_uri="https://app.example/cb",
            pkce_verifier="v",
            http=http_client,
        )
        body = httpx.QueryParams(route.calls[0].request.content.decode())
        assert "resource" not in body

    @respx.mock
    async def test_4xx_raises_bad_request(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.post("https://mcp.example/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        meta = AuthServerMetadata(**_AUTH_DOC)
        client = OAuthClientCredentials(client_id="abc")
        with pytest.raises(BadRequestError):
            await exchange_code_legacy(
                metadata=meta,
                client=client,
                code="c",
                redirect_uri="https://app.example/cb",
                pkce_verifier="v",
                http=http_client,
            )


class TestRefreshTokenLegacy:
    @respx.mock
    async def test_refresh_omits_resource(
        self, http_client: httpx.AsyncClient
    ) -> None:
        route = respx.post("https://mcp.example/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-2",
                    "token_type": "Bearer",
                    "expires_in": 60,
                },
            )
        )
        meta = AuthServerMetadata(**_AUTH_DOC)
        client = OAuthClientCredentials(client_id="abc")
        await refresh_token_legacy(
            metadata=meta,
            client=client,
            refresh_token="rt",
            scopes=["read"],
            http=http_client,
        )
        body = httpx.QueryParams(route.calls[0].request.content.decode())
        assert "resource" not in body
        assert body["grant_type"] == "refresh_token"
        assert body["scope"] == "read"
