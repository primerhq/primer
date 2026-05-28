"""Tests for primer.toolset.oauth.handler.MatrixOAuthHandler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from primer.model.except_ import (
    AuthenticationError,
    AuthRequiredError,
    BadRequestError,
)
from primer.model.provider import OAuthClientCredentials, OAuthConfig
from primer.toolset.oauth.handler import MatrixOAuthHandler
from primer.toolset.oauth.registration import InMemoryClientCredentialsCache
from primer.toolset.oauth.state import InMemoryStateStore
from primer.toolset.oauth.token_store import (
    InMemoryTokenStore,
    TokenRecord,
)


_PROTECTED_RESOURCE_DOC = {
    "resource": "https://mcp.example",
    "authorization_servers": ["https://idp.example"],
}

_AUTH_SERVER_DOC = {
    "issuer": "https://idp.example",
    "authorization_endpoint": "https://idp.example/auth",
    "token_endpoint": "https://idp.example/token",
    "registration_endpoint": "https://idp.example/register",
}


def _config(static: bool = True) -> OAuthConfig:
    static_client = (
        OAuthClientCredentials(client_id="static-abc") if static else None
    )
    return OAuthConfig(
        redirect_uri="https://app.example/cb",
        scopes=["read"],
        resource_uri="https://mcp.example",
        static_client=static_client,
        spec_version="2025-06-18",
    )


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


def _handler(http: httpx.AsyncClient, *, static: bool = True) -> MatrixOAuthHandler:
    return MatrixOAuthHandler(
        oauth_config=_config(static=static),
        mcp_url="https://mcp.example/mcp",
        toolset_id="ts1",
        token_store=InMemoryTokenStore(),
        state_store=InMemoryStateStore(),
        client_cache=InMemoryClientCredentialsCache(),
        http=http,
    )


class TestAuthorize:
    async def test_returns_header_when_token_cached(
        self, http_client: httpx.AsyncClient
    ) -> None:
        h = _handler(http_client)
        rec = TokenRecord(
            access_token="at-cached",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await h._token_store.set(h._cache_key("user-1"), rec)

        headers = await h.authorize(principal="user-1")
        assert headers == {"Authorization": "Bearer at-cached"}

    @respx.mock
    async def test_no_token_raises_auth_required(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )

        h = _handler(http_client)
        with pytest.raises(AuthRequiredError) as exc:
            await h.authorize(principal="user-1")
        assert exc.value.auth_url.startswith("https://idp.example/auth?")
        assert "state=" in exc.value.auth_url
        assert "code_challenge=" in exc.value.auth_url

    @respx.mock
    async def test_refresh_succeeds_and_returns_header(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-new",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "rt-new",
                },
            )
        )
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )

        h = _handler(http_client)
        expired = TokenRecord(
            access_token="at-old",
            refresh_token="rt-old",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        h._token_store._store[h._cache_key("user-1")] = expired

        headers = await h.authorize(principal="user-1")
        assert headers == {"Authorization": "Bearer at-new"}

    @respx.mock
    async def test_refresh_failure_raises_auth_required(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )

        h = _handler(http_client)
        expired = TokenRecord(
            access_token="at-old",
            refresh_token="rt-broken",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        h._token_store._store[h._cache_key("user-1")] = expired

        with pytest.raises(AuthRequiredError):
            await h.authorize(principal="user-1")


class TestCompleteOAuth:
    @respx.mock
    async def test_round_trip_caches_token(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-new",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        )

        h = _handler(http_client)
        with pytest.raises(AuthRequiredError) as exc:
            await h.authorize(principal="user-1")
        state_id = exc.value.state

        await h.complete_oauth(code="auth-code", state_id=state_id)

        headers = await h.authorize(principal="user-1")
        assert headers == {"Authorization": "Bearer at-new"}

    async def test_unknown_state_raises_bad_request(
        self, http_client: httpx.AsyncClient
    ) -> None:
        h = _handler(http_client)
        with pytest.raises(BadRequestError):
            await h.complete_oauth(code="x", state_id="never-issued")

    @respx.mock
    async def test_token_endpoint_4xx_raises_authentication_error(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_code"})
        )

        h = _handler(http_client)
        with pytest.raises(AuthRequiredError) as exc:
            await h.authorize(principal="user-1")
        with pytest.raises(AuthenticationError):
            await h.complete_oauth(code="bad", state_id=exc.value.state)


class TestCacheKey:
    async def test_isolates_per_principal_and_toolset(
        self, http_client: httpx.AsyncClient
    ) -> None:
        h = _handler(http_client)
        k1 = h._cache_key("user-1")
        k2 = h._cache_key("user-2")
        assert k1 != k2

    async def test_anonymous_principal_has_stable_key(
        self, http_client: httpx.AsyncClient
    ) -> None:
        h = _handler(http_client)
        assert h._cache_key(None) == h._cache_key(None)
