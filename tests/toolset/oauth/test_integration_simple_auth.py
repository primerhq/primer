"""Opt-in headless integration test against the MCP python-sdk simple-auth example.

This is NOT a CI test. It is skipped unless ``PRIMER_OAUTH_MCP_URL`` points
at a running MCP python-sdk ``examples/servers/simple-auth`` resource server
(streamable-http). That example server auto-consents via credential auth, so
the authorization-code redirect yields ``?code=&state=`` with NO human click,
making the OAuth 2.1 + PKCE flow fully scriptable.

Run it like::

    # In /tmp: clone the SDK and start the simple-auth AS + RS per its README.
    PRIMER_OAUTH_MCP_URL=http://localhost:8001/mcp \
        uv run pytest tests/toolset/oauth/test_integration_simple_auth.py -n0 -q

Optional env (read per the no-hardcoded-secrets rule):
  PRIMER_OAUTH_MCP_URL       MCP resource-server URL (required to run)
  PRIMER_OAUTH_REDIRECT_URI  callback URI registered via DCR (default
                             http://localhost:8765/callback -- this test never
                             binds it; it intercepts the redirect itself)
  PRIMER_OAUTH_SCOPES        space-separated scopes (default "user")
  PRIMER_OAUTH_SPEC_VERSION  force a spec version (default: probe)
  PRIMER_OAUTH_TOOL          tool name to call after auth (default: first tool)
  PRIMER_OAUTH_USERNAME      demo-login username (default "demo_user")
  PRIMER_OAUTH_PASSWORD      demo-login password (default "demo_password")

The simple-auth example presents a credential login form rather than a
zero-click consent screen, so the helper below auto-submits the demo
credentials (read from env, never hardcoded as secrets -- the defaults are
the example's public demo values) to obtain the redirect carrying the code.

What it asserts: primer does DCR -> builds a PKCE authorization URL ->
the (auto-consenting) AS redirects back with a code -> complete_oauth()
exchanges it for a bearer token -> an authenticated MCP tools/call succeeds.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pytest

from primer.model.except_ import AuthRequiredError
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


_MCP_URL = os.environ.get("PRIMER_OAUTH_MCP_URL")

pytestmark = pytest.mark.skipif(
    not _MCP_URL,
    reason="set PRIMER_OAUTH_MCP_URL to a running simple-auth MCP server to run",
)


def _config() -> OAuthConfig:
    scopes = os.environ.get("PRIMER_OAUTH_SCOPES", "user").split()
    spec = os.environ.get("PRIMER_OAUTH_SPEC_VERSION") or None
    return OAuthConfig(
        redirect_uri=os.environ.get(
            "PRIMER_OAUTH_REDIRECT_URI", "http://localhost:8765/callback"
        ),
        scopes=scopes,
        # None -> primer derives the MCP origin as the resource indicator.
        resource_uri=None,
        # None -> primer performs Dynamic Client Registration (RFC 7591).
        static_client=None,
        spec_version=spec,  # type: ignore[arg-type]
        client_name="primer-oauth-integration-test",
    )


def _code_from_location(location: str) -> tuple[str, str] | None:
    q = parse_qs(urlparse(location).query)
    if "code" in q and "state" in q:
        return q["code"][0], q["state"][0]
    return None


async def _follow_auth_url_for_code(auth_url: str) -> tuple[str, str]:
    """Drive the auth URL through login and pull (code, state).

    The simple-auth AS redirects the authorization endpoint to a
    credential login form. We follow redirects until we either land on
    the redirect_uri (carrying ?code=&state=) or hit the login form,
    which we auto-submit with the demo credentials. We never bind the
    redirect URI; we read the code off the Location header.
    """
    username = os.environ.get("PRIMER_OAUTH_USERNAME", "demo_user")
    password = os.environ.get("PRIMER_OAUTH_PASSWORD", "demo_password")

    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        url = auth_url
        for _ in range(8):
            resp = await client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers["location"]
                found = _code_from_location(location)
                if found is not None:
                    return found
                url = urljoin(str(resp.url), location)
                continue

            if resp.status_code == 200 and "<form" in resp.text.lower():
                # Credential login form: extract the POST action + hidden
                # state, then submit the demo credentials.
                action_match = re.search(
                    r'<form[^>]*action="([^"]+)"', resp.text, re.IGNORECASE
                )
                state_match = re.search(
                    r'name="state"[^>]*value="([^"]+)"', resp.text, re.IGNORECASE
                )
                if not action_match or not state_match:
                    raise AssertionError(
                        "login form present but action/state not parseable"
                    )
                action = urljoin(str(resp.url), action_match.group(1))
                post = await client.post(
                    action,
                    data={
                        "username": username,
                        "password": password,
                        "state": state_match.group(1),
                    },
                )
                if post.status_code in (301, 302, 303, 307, 308):
                    found = _code_from_location(post.headers["location"])
                    if found is not None:
                        return found
                    url = urljoin(str(post.url), post.headers["location"])
                    continue
                raise AssertionError(
                    f"login POST did not redirect; status={post.status_code}, "
                    f"body={post.text[:300]}"
                )

            raise AssertionError(
                "auth URL did not reach a code redirect or a login form; "
                f"status={resp.status_code}, url={resp.url}, body={resp.text[:300]}"
            )

        raise AssertionError("too many redirects following the auth URL")


async def test_headless_oauth_flow_against_simple_auth() -> None:
    assert _MCP_URL is not None
    async with httpx.AsyncClient(timeout=30.0) as http:
        handler = PrimerOAuthHandler(
            oauth_config=_config(),
            mcp_url=_MCP_URL,
            toolset_id="integration-ts",
            token_store=InMemoryTokenStore(),
            state_store=InMemoryStateStore(),
            client_cache=InMemoryClientCredentialsCache(),
            http=http,
        )

        # Phase 1: authorize() with no cached token must raise AuthRequired
        # after doing discovery + DCR + building the PKCE authorization URL.
        with pytest.raises(AuthRequiredError) as exc:
            await handler.authorize(principal="integration-user")
        auth_url = exc.value.auth_url
        state_id = exc.value.state

        q = parse_qs(urlparse(auth_url).query)
        assert q["response_type"] == ["code"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["state"] == [state_id]

        # Phase 2: script-follow the auth URL (server auto-consents).
        code, returned_state = await _follow_auth_url_for_code(auth_url)
        assert returned_state == state_id

        # Phase 3: exchange the code for a token.
        await handler.complete_oauth(code=code, state_id=returned_state)

        # The next authorize() now returns the bearer header from cache.
        headers = await handler.authorize(principal="integration-user")
        assert headers["Authorization"].startswith("Bearer ")

        # Phase 4: an authenticated MCP tools/call must succeed.
        provider = McpToolsetProvider(
            toolset_id="integration-ts",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url=_MCP_URL),
            ),
            oauth=handler,
        )
        tools = [t async for t in provider.list_tools(principal="integration-user")]
        assert tools, "authenticated list_tools returned no tools"

        tool_name = os.environ.get("PRIMER_OAUTH_TOOL") or tools[0].id
        result = await provider.call(
            tool_name=tool_name,
            arguments={},
            principal="integration-user",
        )
        assert result is not None
