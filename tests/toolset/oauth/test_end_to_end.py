"""End-to-end regression guard for the MCP OAuth 2.1 (PKCE) handshake.

This is the always-on, network-free regression test that walks the FULL
authorization-code + PKCE (S256) flow through
:class:`primer.toolset.oauth.handler.PrimerOAuthHandler` and proves the
resulting ``Authorization: Bearer`` header is applied to an MCP
``tools/call``.

Every HTTP hop is mocked with ``respx``. The test asserts the precise
wire sequence end to end:

1. RFC 9728 protected-resource document at the MCP origin
   (``/.well-known/oauth-protected-resource``) -> ``authorization_servers[0]``.
2. RFC 8414 authorization-server metadata at the issuer origin
   (``/.well-known/oauth-authorization-server``).
3. RFC 7591 Dynamic Client Registration POST ``/register`` (static_client
   is None, so primer must register a public PKCE-only client).
4. The built authorization URL carries ``response_type=code`` +
   ``code_challenge_method=S256`` + ``code_challenge`` + ``state`` +
   ``resource`` (RFC 8707).
5. POST ``/token`` with ``grant_type=authorization_code`` +
   ``code_verifier`` + the DCR-issued ``client_id`` + ``resource``.
6. The resulting bearer header is handed to the MCP HTTP transport when a
   ``tools/call`` session is opened.

The legacy 2025-03-26 path (MCP origin IS the auth server, no
protected-resource indirection, no ``resource`` parameter) is covered by a
second, cheaper case.
"""

from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import mcp.types as mcp_types
import pytest
import respx

from primer.model.provider import (
    HttpConfig,
    McpConfig,
    OAuthConfig,
    TransportType,
)
from primer.toolset.mcp import McpToolsetProvider
from primer.toolset.oauth.handler import PrimerOAuthHandler
from primer.toolset.oauth.registration import InMemoryClientCredentialsCache
from primer.toolset.oauth.state import InMemoryStateStore
from primer.toolset.oauth.token_store import InMemoryTokenStore


_MCP_ORIGIN = "https://mcp.example"
_MCP_URL = "https://mcp.example/mcp"
_ISSUER = "https://idp.example"
_REDIRECT_URI = "https://app.example/cb"

_PROTECTED_RESOURCE_DOC = {
    "resource": _MCP_ORIGIN,
    "authorization_servers": [_ISSUER],
    "bearer_methods_supported": ["header"],
}

_AUTH_SERVER_DOC = {
    "issuer": _ISSUER,
    "authorization_endpoint": f"{_ISSUER}/auth",
    "token_endpoint": f"{_ISSUER}/token",
    "registration_endpoint": f"{_ISSUER}/register",
    "scopes_supported": ["read", "write"],
    "code_challenge_methods_supported": ["S256"],
}

_DCR_CLIENT_ID = "dcr-client-xyz"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


def _modern_config() -> OAuthConfig:
    # static_client=None forces Dynamic Client Registration.
    return OAuthConfig(
        redirect_uri=_REDIRECT_URI,
        scopes=["read"],
        resource_uri=_MCP_ORIGIN,
        static_client=None,
        spec_version="2025-06-18",
        client_name="primer-e2e",
    )


def _handler(http: httpx.AsyncClient, config: OAuthConfig) -> PrimerOAuthHandler:
    return PrimerOAuthHandler(
        oauth_config=config,
        mcp_url=_MCP_URL,
        toolset_id="ts-e2e",
        token_store=InMemoryTokenStore(),
        state_store=InMemoryStateStore(),
        client_cache=InMemoryClientCredentialsCache(),
        http=http,
    )


class _HeaderCapturingProvider(McpToolsetProvider):
    """Captures the headers the HTTP transport would receive on a call.

    The base provider builds the per-call header set (base headers +
    the Authorization header returned by ``oauth.authorize``) and hands
    it to the streamable-http transport. We intercept ``_open_session``
    one level up so the real ``oauth.authorize`` preflight runs (proving
    the bearer header is applied) without standing up a network session.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.captured_headers: dict[str, str] | None = None

    @asynccontextmanager
    async def _open_session(self, *, principal: str | None = None):  # type: ignore[override]
        assert self._oauth is not None
        # This is exactly the preflight the real HTTP path performs.
        auth_headers = await self._oauth.authorize(principal=principal)
        base_headers: dict[str, str] = {}
        base_headers.update(auth_headers)
        self.captured_headers = base_headers

        from unittest.mock import AsyncMock, MagicMock

        fake_session = MagicMock()
        fake_session.call_tool = AsyncMock(
            return_value=mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text="ok")]
            )
        )
        yield fake_session


class TestModernEndToEnd:
    @respx.mock
    async def test_full_handshake_applies_bearer_to_tools_call(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # RFC 9728 section 3.1: a resource with a /mcp path advertises its
        # metadata at the path-suffixed well-known URL (this mirrors the
        # MCP python-sdk simple-auth server's real behaviour).
        pr_route = respx.get(
            f"{_MCP_ORIGIN}/.well-known/oauth-protected-resource/mcp"
        ).mock(return_value=httpx.Response(200, json=_PROTECTED_RESOURCE_DOC))
        as_route = respx.get(
            f"{_ISSUER}/.well-known/oauth-authorization-server"
        ).mock(return_value=httpx.Response(200, json=_AUTH_SERVER_DOC))
        dcr_route = respx.post(f"{_ISSUER}/register").mock(
            return_value=httpx.Response(
                201,
                json={
                    "client_id": _DCR_CLIENT_ID,
                    "token_endpoint_auth_method": "none",
                    "redirect_uris": [_REDIRECT_URI],
                },
            )
        )
        token_route = respx.post(f"{_ISSUER}/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-final",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "rt-final",
                },
            )
        )

        config = _modern_config()
        handler = _handler(http_client, config)

        # ---- Phase 1: authorize() with no cached token -> AuthRequiredError.
        from primer.model.except_ import AuthRequiredError

        with pytest.raises(AuthRequiredError) as exc:
            await handler.authorize(principal="user-e2e")
        auth_url = exc.value.auth_url
        state_id = exc.value.state

        # Sequence so far: protected-resource doc, then AS metadata, then DCR.
        assert pr_route.called, "RFC 9728 protected-resource doc must be fetched"
        assert as_route.called, "RFC 8414 auth-server metadata must be fetched"
        assert dcr_route.called, "RFC 7591 DCR /register must be POSTed"

        # protected-resource was fetched at the RFC 9728 path-suffixed URL.
        assert (
            str(pr_route.calls[0].request.url)
            == f"{_MCP_ORIGIN}/.well-known/oauth-protected-resource/mcp"
        )

        # DCR payload: public PKCE-only client.
        dcr_body = dcr_route.calls[0].request
        import json as _json

        dcr_payload = _json.loads(dcr_body.content.decode())
        assert dcr_payload["token_endpoint_auth_method"] == "none"
        assert dcr_payload["redirect_uris"] == [_REDIRECT_URI]
        assert dcr_payload["client_name"] == "primer-e2e"

        # ---- The authorization URL carries the PKCE + resource params.
        parsed = urlparse(auth_url)
        assert parsed.netloc == "idp.example"
        assert parsed.path == "/auth"
        q = parse_qs(parsed.query)
        assert q["response_type"] == ["code"]
        assert q["client_id"] == [_DCR_CLIENT_ID]
        assert q["redirect_uri"] == [_REDIRECT_URI]
        assert q["state"] == [state_id]
        assert q["code_challenge_method"] == ["S256"]
        challenge = q["code_challenge"][0]
        assert challenge  # non-empty
        assert q["resource"] == [_MCP_ORIGIN]  # RFC 8707 indicator
        assert q["scope"] == ["read"]

        # ---- Phase 2: complete_oauth() -> POST /token, grant=authorization_code.
        await handler.complete_oauth(code="the-auth-code", state_id=state_id)

        assert token_route.called, "token endpoint must be POSTed"
        token_body = httpx.QueryParams(token_route.calls[0].request.content.decode())
        assert token_body["grant_type"] == "authorization_code"
        assert token_body["code"] == "the-auth-code"
        assert token_body["redirect_uri"] == _REDIRECT_URI
        assert token_body["client_id"] == _DCR_CLIENT_ID
        assert token_body["resource"] == _MCP_ORIGIN

        # The submitted code_verifier must hash (S256) to the earlier
        # code_challenge -- proving the PKCE pair is consistent end to end.
        verifier = token_body["code_verifier"]
        recomputed = (
            base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        assert recomputed == challenge

        # ---- Phase 3: the bearer header is applied to an MCP tools/call.
        provider = _HeaderCapturingProvider(
            toolset_id="ts-e2e",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url=_MCP_URL),
            ),
            oauth=handler,
        )
        result = await provider.call(
            tool_name="do_thing",
            arguments={},
            principal="user-e2e",
        )
        assert result is not None
        assert provider.captured_headers == {"Authorization": "Bearer at-final"}

        # The discovery hops happened during the authorize() preflight and
        # again inside complete_oauth() (which re-negotiates to find the
        # token endpoint): 2 protected-resource fetches total. The call-time
        # authorize() at Phase 3 hit the cached token, so it added no extra
        # discovery round trip.
        assert pr_route.call_count == 2


class TestLegacyEndToEnd:
    @respx.mock
    async def test_legacy_handshake_no_resource_param(
        self, http_client: httpx.AsyncClient
    ) -> None:
        # Legacy: MCP origin IS the auth server. No protected-resource doc.
        as_route = respx.get(
            f"{_MCP_ORIGIN}/.well-known/oauth-authorization-server"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "issuer": _MCP_ORIGIN,
                    "authorization_endpoint": f"{_MCP_ORIGIN}/auth",
                    "token_endpoint": f"{_MCP_ORIGIN}/token",
                    "registration_endpoint": f"{_MCP_ORIGIN}/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        )
        dcr_route = respx.post(f"{_MCP_ORIGIN}/register").mock(
            return_value=httpx.Response(
                201, json={"client_id": _DCR_CLIENT_ID}
            )
        )
        token_route = respx.post(f"{_MCP_ORIGIN}/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-legacy",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        )

        config = OAuthConfig(
            redirect_uri=_REDIRECT_URI,
            scopes=["read"],
            resource_uri=_MCP_ORIGIN,
            static_client=None,
            spec_version="2025-03-26",
            client_name="primer-e2e",
        )
        handler = _handler(http_client, config)

        from primer.model.except_ import AuthRequiredError

        with pytest.raises(AuthRequiredError) as exc:
            await handler.authorize(principal="user-legacy")
        auth_url = exc.value.auth_url
        state_id = exc.value.state

        assert as_route.called
        assert dcr_route.called

        q = parse_qs(urlparse(auth_url).query)
        assert q["response_type"] == ["code"]
        assert q["code_challenge_method"] == ["S256"]
        # Legacy omits the RFC 8707 resource indicator.
        assert "resource" not in q

        await handler.complete_oauth(code="legacy-code", state_id=state_id)
        assert token_route.called
        token_body = httpx.QueryParams(token_route.calls[0].request.content.decode())
        assert token_body["grant_type"] == "authorization_code"
        assert token_body["code_verifier"]
        assert "resource" not in token_body

        # Bearer is cached and returned on the next authorize().
        headers = await handler.authorize(principal="user-legacy")
        assert headers == {"Authorization": "Bearer at-legacy"}
