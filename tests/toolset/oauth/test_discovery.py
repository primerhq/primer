"""Tests for primer.toolset.oauth.discovery."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import respx

from primer.model.except_ import BadRequestError
from primer.model.provider import OAuthClientCredentials
from primer.toolset.oauth.discovery import (
    AuthServerMetadata,
    build_authorization_url,
    exchange_code,
    negotiate,
    pkce_pair,
    refresh_token,
)


_PROTECTED_RESOURCE_DOC = {
    "resource": "https://mcp.example",
    "authorization_servers": ["https://idp.example"],
    "bearer_methods_supported": ["header"],
}

_AUTH_SERVER_DOC = {
    "issuer": "https://idp.example",
    "authorization_endpoint": "https://idp.example/auth",
    "token_endpoint": "https://idp.example/token",
    "registration_endpoint": "https://idp.example/register",
    "scopes_supported": ["read", "write"],
    "code_challenge_methods_supported": ["S256"],
}


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


class TestPkcePair:
    def test_returns_two_strings_and_valid_s256(self) -> None:
        verifier, challenge = pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected
        assert len(verifier) >= 43

    def test_pairs_are_unique(self) -> None:
        a = pkce_pair()
        b = pkce_pair()
        assert a != b


class TestNegotiate:
    @respx.mock
    async def test_modern_path_negotiated_when_protected_resource_returned(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )

        version, metadata = await negotiate(
            mcp_url="https://mcp.example",
            forced=None,
            http=http_client,
        )
        assert version in ("2025-06-18", "2025-11-25")
        assert str(metadata.token_endpoint) == "https://idp.example/token"

    @respx.mock
    async def test_legacy_fallback_when_protected_resource_404s(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )

        version, metadata = await negotiate(
            mcp_url="https://mcp.example",
            forced=None,
            http=http_client,
        )
        assert version == "2025-03-26"
        assert str(metadata.token_endpoint) == "https://idp.example/token"

    @respx.mock
    async def test_forced_version_skips_probing(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )

        version, metadata = await negotiate(
            mcp_url="https://mcp.example",
            forced="2025-06-18",
            http=http_client,
        )
        assert version == "2025-06-18"

    @respx.mock
    async def test_no_metadata_anywhere_raises_bad_request(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced=None,
                http=http_client,
            )


class TestProtectedResourcePathSuffix:
    """RFC 9728 section 3.1: the protected-resource metadata for a resource
    with a path component lives at the path-suffixed well-known URL, not at
    the bare origin. Regression guard for the MCP python-sdk simple-auth
    server, which serves the doc ONLY at the path-suffixed location.
    """

    @respx.mock
    async def test_path_suffixed_protected_resource_is_discovered(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # Origin form 404s; the spec-compliant path-suffixed form serves it.
        origin_route = respx.get(
            "https://mcp.example/.well-known/oauth-protected-resource"
        ).mock(return_value=httpx.Response(404))
        suffixed_route = respx.get(
            "https://mcp.example/.well-known/oauth-protected-resource/mcp"
        ).mock(return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC))
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )

        version, metadata = await negotiate(
            mcp_url="https://mcp.example/mcp",
            forced=None,
            http=http_client,
        )
        assert version in ("2025-06-18", "2025-11-25")
        assert str(metadata.token_endpoint) == "https://idp.example/token"
        # The path-suffixed form must be tried (and is tried first, so the
        # origin form need not even be reached -- but tolerate either order).
        assert suffixed_route.called

    @respx.mock
    async def test_path_suffixed_tried_before_origin(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # When the path-suffixed form serves the doc, primer uses it without
        # needing the origin form at all.
        suffixed_route = respx.get(
            "https://mcp.example/.well-known/oauth-protected-resource/mcp"
        ).mock(return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC))
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        version, _ = await negotiate(
            mcp_url="https://mcp.example/mcp",
            forced="2025-06-18",
            http=http_client,
        )
        assert version == "2025-06-18"
        assert suffixed_route.call_count == 1

    @respx.mock
    async def test_origin_form_still_works_when_only_origin_serves_it(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # Path-suffixed form 404s; origin form serves it -> fallback holds.
        respx.get(
            "https://mcp.example/.well-known/oauth-protected-resource/mcp"
        ).mock(return_value=httpx.Response(404))
        origin_route = respx.get(
            "https://mcp.example/.well-known/oauth-protected-resource"
        ).mock(return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC))
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        version, _ = await negotiate(
            mcp_url="https://mcp.example/mcp",
            forced="2025-06-18",
            http=http_client,
        )
        assert version == "2025-06-18"
        assert origin_route.called

    @respx.mock
    async def test_no_path_component_uses_origin_only(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # A resource with no path advertises at the bare origin; there is no
        # path-suffixed candidate to try.
        from primer.toolset.oauth.discovery import _protected_resource_urls

        urls = _protected_resource_urls("https://mcp.example")
        assert urls == [
            "https://mcp.example/.well-known/oauth-protected-resource"
        ]

    def test_url_candidate_order_for_pathed_resource(self) -> None:
        from primer.toolset.oauth.discovery import _protected_resource_urls

        urls = _protected_resource_urls("https://mcp.example/mcp")
        assert urls == [
            "https://mcp.example/.well-known/oauth-protected-resource/mcp",
            "https://mcp.example/.well-known/oauth-protected-resource",
        ]


class TestBuildAuthorizationUrl:
    def test_includes_required_params(self) -> None:
        from urllib.parse import parse_qs, urlparse

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="client-abc")
        verifier, challenge = pkce_pair()
        url = build_authorization_url(
            metadata=metadata,
            client=client,
            redirect_uri="https://app.example/cb",
            scopes=["read"],
            resource_uri="https://mcp.example",
            pkce_challenge=challenge,
            state_id="state-1",
            spec_version="2025-06-18",
        )
        parsed = urlparse(url)
        assert parsed.netloc == "idp.example"
        assert parsed.path == "/auth"
        q = parse_qs(parsed.query)
        assert q["response_type"] == ["code"]
        assert q["client_id"] == ["client-abc"]
        assert q["redirect_uri"] == ["https://app.example/cb"]
        assert q["state"] == ["state-1"]
        assert q["code_challenge"] == [challenge]
        assert q["code_challenge_method"] == ["S256"]
        assert q["scope"] == ["read"]
        assert q["resource"] == ["https://mcp.example"]

    def test_resource_omitted_when_none(self) -> None:
        from urllib.parse import parse_qs, urlparse

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="abc")
        url = build_authorization_url(
            metadata=metadata,
            client=client,
            redirect_uri="https://app.example/cb",
            scopes=[],
            resource_uri=None,
            pkce_challenge="ch",
            state_id="s",
            spec_version="2025-06-18",
        )
        q = parse_qs(urlparse(url).query)
        assert "resource" not in q
        assert "scope" not in q

    def test_legacy_version_omits_resource_param(self) -> None:
        from urllib.parse import parse_qs, urlparse

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="abc")
        url = build_authorization_url(
            metadata=metadata,
            client=client,
            redirect_uri="https://app.example/cb",
            scopes=[],
            resource_uri="https://mcp.example",
            pkce_challenge="ch",
            state_id="s",
            spec_version="2025-03-26",
        )
        q = parse_qs(urlparse(url).query)
        assert "resource" not in q


class TestExchangeCode:
    @respx.mock
    async def test_public_client_uses_client_id_only(
        self, http_client: httpx.AsyncClient
    ) -> None:
        token_response = {
            "access_token": "at-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "rt-1",
        }
        route = respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(200, json=token_response)
        )

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="client-abc")
        rec = await exchange_code(
            metadata=metadata,
            client=client,
            code="auth-code",
            redirect_uri="https://app.example/cb",
            pkce_verifier="verifier",
            resource_uri="https://mcp.example",
            spec_version="2025-06-18",
            http=http_client,
        )
        assert rec.access_token.get_secret_value() == "at-1"
        assert rec.refresh_token.get_secret_value() == "rt-1"
        request = route.calls[0].request
        body = httpx.QueryParams(request.content.decode())
        assert body["client_id"] == "client-abc"
        assert body["code"] == "auth-code"
        assert body["code_verifier"] == "verifier"
        assert body["resource"] == "https://mcp.example"
        assert "Authorization" not in request.headers or not request.headers.get("Authorization", "").startswith("Basic")

    @respx.mock
    async def test_confidential_client_sends_basic_auth(
        self, http_client: httpx.AsyncClient
    ) -> None:
        token_response = {
            "access_token": "at",
            "token_type": "Bearer",
            "expires_in": 1800,
        }
        route = respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(200, json=token_response)
        )

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(
            client_id="client-abc",
            client_secret="shh",
        )
        await exchange_code(
            metadata=metadata,
            client=client,
            code="auth-code",
            redirect_uri="https://app.example/cb",
            pkce_verifier="verifier",
            resource_uri=None,
            spec_version="2025-06-18",
            http=http_client,
        )
        auth_header = route.calls[0].request.headers["Authorization"]
        assert auth_header.startswith("Basic ")

    @respx.mock
    async def test_token_endpoint_4xx_raises_bad_request(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="abc")
        with pytest.raises(BadRequestError):
            await exchange_code(
                metadata=metadata,
                client=client,
                code="bad",
                redirect_uri="https://app.example/cb",
                pkce_verifier="v",
                resource_uri=None,
                spec_version="2025-06-18",
                http=http_client,
            )


class TestRefreshToken:
    @respx.mock
    async def test_refresh_returns_new_record(
        self, http_client: httpx.AsyncClient
    ) -> None:
        route = respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-2",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "rt-2",
                },
            )
        )

        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="abc")
        rec = await refresh_token(
            metadata=metadata,
            client=client,
            refresh_token="old-rt",
            scopes=["read"],
            resource_uri="https://mcp.example",
            spec_version="2025-06-18",
            http=http_client,
        )
        assert rec.access_token.get_secret_value() == "at-2"
        assert rec.refresh_token.get_secret_value() == "rt-2"
        body = httpx.QueryParams(route.calls[0].request.content.decode())
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-rt"
        assert body["scope"] == "read"
        assert body["resource"] == "https://mcp.example"


# ---- Edge-case coverage for _fetch_*, negotiate forced paths, _post_token --


class TestDiscoveryEdgeCases:
    @respx.mock
    async def test_protected_resource_http_error_falls_back_to_legacy(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # ConnectError is an httpx.HTTPError subclass -> _fetch_protected_resource
        # returns None; negotiate falls back to legacy fetch.
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            side_effect=httpx.ConnectError("boom")
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        version, _ = await negotiate(
            mcp_url="https://mcp.example",
            forced=None,
            http=http_client,
        )
        assert version == "2025-03-26"

    @respx.mock
    async def test_protected_resource_invalid_json_falls_back(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, content=b"not-json")
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        version, _ = await negotiate(
            mcp_url="https://mcp.example",
            forced=None,
            http=http_client,
        )
        assert version == "2025-03-26"

    @respx.mock
    async def test_protected_resource_missing_servers_field_falls_back(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json={"resource": "x"})
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json=_AUTH_SERVER_DOC)
        )
        version, _ = await negotiate(
            mcp_url="https://mcp.example",
            forced=None,
            http=http_client,
        )
        assert version == "2025-03-26"

    @respx.mock
    async def test_auth_server_metadata_http_error_negotiates_no_metadata(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced=None,
                http=http_client,
            )

    @respx.mock
    async def test_auth_server_metadata_invalid_payload_negotiates_failure(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(200, json={"issuer": "https://idp.example"})
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced=None,
                http=http_client,
            )

    @respx.mock
    async def test_forced_legacy_missing_metadata_raises(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced="2025-03-26",
                http=http_client,
            )

    @respx.mock
    async def test_forced_modern_missing_protected_resource_raises(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced="2025-06-18",
                http=http_client,
            )

    @respx.mock
    async def test_forced_modern_empty_servers_list_raises(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(
                200, json={"resource": "x", "authorization_servers": []}
            )
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced="2025-06-18",
                http=http_client,
            )

    @respx.mock
    async def test_forced_modern_missing_auth_server_meta_raises(
        self, http_client: httpx.AsyncClient
    ) -> None:
        respx.get("https://mcp.example/.well-known/oauth-protected-resource").mock(
            return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC)
        )
        respx.get("https://idp.example/.well-known/oauth-authorization-server").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(BadRequestError):
            await negotiate(
                mcp_url="https://mcp.example",
                forced="2025-06-18",
                http=http_client,
            )

    @respx.mock
    async def test_post_token_http_error_is_classified(
        self, http_client: httpx.AsyncClient
    ) -> None:
        from primer.model.except_ import PrimerError

        respx.post("https://idp.example/token").mock(
            side_effect=httpx.ConnectError("net down")
        )
        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="abc")
        with pytest.raises(PrimerError):
            await exchange_code(
                metadata=metadata,
                client=client,
                code="c",
                redirect_uri="https://app.example/cb",
                pkce_verifier="v",
                resource_uri=None,
                spec_version="2025-06-18",
                http=http_client,
            )

    @respx.mock
    async def test_post_token_5xx_is_classified(
        self, http_client: httpx.AsyncClient
    ) -> None:
        from primer.model.except_ import PrimerError

        respx.post("https://idp.example/token").mock(
            return_value=httpx.Response(503, text="upstream down")
        )
        metadata = AuthServerMetadata(**_AUTH_SERVER_DOC)
        client = OAuthClientCredentials(client_id="abc")
        with pytest.raises(PrimerError):
            await exchange_code(
                metadata=metadata,
                client=client,
                code="c",
                redirect_uri="https://app.example/cb",
                pkce_verifier="v",
                resource_uri=None,
                spec_version="2025-06-18",
                http=http_client,
            )
