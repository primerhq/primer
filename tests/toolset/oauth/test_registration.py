"""Tests for matrix.toolset.oauth.registration."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from primer.model.except_ import ConfigError
from primer.model.provider import OAuthClientCredentials
from primer.toolset.oauth.discovery import AuthServerMetadata
from primer.toolset.oauth.registration import (
    InMemoryClientCredentialsCache,
    resolve,
)


_META_WITH_DCR = AuthServerMetadata(
    issuer="https://idp.example",
    authorization_endpoint="https://idp.example/auth",
    token_endpoint="https://idp.example/token",
    registration_endpoint="https://idp.example/register",
)

_META_NO_DCR = AuthServerMetadata(
    issuer="https://idp.example",
    authorization_endpoint="https://idp.example/auth",
    token_endpoint="https://idp.example/token",
)


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


class TestInMemoryClientCredentialsCache:
    async def test_set_then_get(self) -> None:
        cache = InMemoryClientCredentialsCache()
        creds = OAuthClientCredentials(client_id="abc")
        await cache.set("k", creds, ttl=timedelta(hours=1))
        got = await cache.get("k")
        assert got is not None
        assert got.client_id == "abc"

    async def test_missing_returns_none(self) -> None:
        cache = InMemoryClientCredentialsCache()
        assert await cache.get("nope") is None

    async def test_expired_evicted(self) -> None:
        cache = InMemoryClientCredentialsCache()
        creds = OAuthClientCredentials(client_id="abc")
        await cache.set("k", creds, ttl=timedelta(seconds=-1))
        assert await cache.get("k") is None


class TestResolve:
    async def test_static_credentials_skip_dcr(
        self, http_client: httpx.AsyncClient
    ) -> None:
        cache = InMemoryClientCredentialsCache()
        static = OAuthClientCredentials(client_id="static-abc")
        result = await resolve(
            metadata=_META_WITH_DCR,
            static=static,
            redirect_uri="https://app.example/cb",
            client_name="matrix",
            cache=cache,
            http=http_client,
        )
        assert result is static

    @respx.mock
    async def test_dcr_when_no_static_and_endpoint_present(
        self, http_client: httpx.AsyncClient
    ) -> None:
        cache = InMemoryClientCredentialsCache()
        respx.post("https://idp.example/register").mock(
            return_value=httpx.Response(
                201,
                json={"client_id": "dcr-issued-1", "client_id_issued_at": 0},
            )
        )

        result = await resolve(
            metadata=_META_WITH_DCR,
            static=None,
            redirect_uri="https://app.example/cb",
            client_name="matrix",
            cache=cache,
            http=http_client,
        )
        assert result.client_id == "dcr-issued-1"
        assert result.client_secret is None

    @respx.mock
    async def test_dcr_caches_for_subsequent_resolves(
        self, http_client: httpx.AsyncClient
    ) -> None:
        cache = InMemoryClientCredentialsCache()
        route = respx.post("https://idp.example/register").mock(
            return_value=httpx.Response(
                201,
                json={"client_id": "dcr-issued-1"},
            )
        )

        await resolve(
            metadata=_META_WITH_DCR,
            static=None,
            redirect_uri="https://app.example/cb",
            client_name="matrix",
            cache=cache,
            http=http_client,
        )
        await resolve(
            metadata=_META_WITH_DCR,
            static=None,
            redirect_uri="https://app.example/cb",
            client_name="matrix",
            cache=cache,
            http=http_client,
        )
        assert route.call_count == 1

    @respx.mock
    async def test_dcr_returns_secret_when_server_provides_one(
        self, http_client: httpx.AsyncClient
    ) -> None:
        cache = InMemoryClientCredentialsCache()
        respx.post("https://idp.example/register").mock(
            return_value=httpx.Response(
                201,
                json={
                    "client_id": "dcr-with-secret",
                    "client_secret": "shh",
                },
            )
        )

        result = await resolve(
            metadata=_META_WITH_DCR,
            static=None,
            redirect_uri="https://app.example/cb",
            client_name="matrix",
            cache=cache,
            http=http_client,
        )
        assert result.client_id == "dcr-with-secret"
        assert result.client_secret is not None
        assert result.client_secret.get_secret_value() == "shh"

    async def test_no_static_and_no_dcr_endpoint_raises_config_error(
        self, http_client: httpx.AsyncClient
    ) -> None:
        cache = InMemoryClientCredentialsCache()
        with pytest.raises(ConfigError):
            await resolve(
                metadata=_META_NO_DCR,
                static=None,
                redirect_uri="https://app.example/cb",
                client_name="matrix",
                cache=cache,
                http=http_client,
            )

    @respx.mock
    async def test_dcr_4xx_propagates_as_bad_request(
        self, http_client: httpx.AsyncClient
    ) -> None:
        from primer.model.except_ import BadRequestError

        cache = InMemoryClientCredentialsCache()
        respx.post("https://idp.example/register").mock(
            return_value=httpx.Response(400, json={"error": "invalid_redirect_uri"})
        )
        with pytest.raises(BadRequestError):
            await resolve(
                metadata=_META_WITH_DCR,
                static=None,
                redirect_uri="https://app.example/cb",
                client_name="matrix",
                cache=cache,
                http=http_client,
            )
