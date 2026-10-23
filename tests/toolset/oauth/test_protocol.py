"""The OAuth protocol client of an MCP toolset's shared sign-in (plan 07, Task 6; design 12.3, 12.4 and 12.5).

Pure HTTP, against respx routes and, at the end, the hermetic mock server: the discovery order, the PKCE S256 rule, the full-URL resource, client
registration, the token grants and revocation. Next to the plan's rules, the security properties are pinned on their own:

* an error text carries the HTTP status and the RFC 6749 ``error`` / ``error_description`` fields only, cut to 200 characters: never the response body,
  and never a secret the request carried, even when the server echoes it;
* metadata that names another issuer, or holds an endpoint that is not an http(s) URL (a ``javascript:`` authorization endpoint would run in the console
  that opens it), is never used;
* a rate limit is not a refusal, so a refresh that meets one keeps its refresh token;
* no secret shows in a ``repr``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import string
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from primer.model.except_ import BadRequestError, NetworkError, RateLimitError, ServerError
from primer.toolset.oauth.protocol import (
    AuthServerMetadata,
    ClientAuth,
    RegisteredClient,
    TokenEndpointRejected,
    TokenGrant,
    auth_server_metadata_urls,
    authorization_url,
    canonical_resource,
    client_auth_for,
    client_credentials_grant,
    discover,
    exchange_code,
    parse_www_authenticate,
    pkce_pair,
    probe_challenge,
    protected_resource_urls,
    refresh_grant,
    register_client,
    require_s256,
    revoke,
)
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401 - the fixture of the last test

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
MCP = "https://m.example/mcp"
ISSUER = "https://idp.example/tenant1"
TOKEN = ISSUER + "/token"
REVOKE = ISSUER + "/revoke"
PRM_DOC = {"resource": MCP, "authorization_servers": [ISSUER], "scopes_supported": ["read", "write"]}
AS_DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": TOKEN,
    "registration_endpoint": ISSUER + "/register",
    "revocation_endpoint": REVOKE,
    "code_challenge_methods_supported": ["S256"],
}
PRM_PATH = "https://m.example/.well-known/oauth-protected-resource/mcp"
PRM_ROOT = "https://m.example/.well-known/oauth-protected-resource"
AS_OAUTH = "https://idp.example/.well-known/oauth-authorization-server/tenant1"
AS_OIDC_INSERTED = "https://idp.example/.well-known/openid-configuration/tenant1"
AS_OIDC_APPENDED = "https://idp.example/tenant1/.well-known/openid-configuration"
PUBLIC = ClientAuth(client_id="pub-client", client_secret=None, method="none")
BASIC = ClientAuth(client_id="conf client", client_secret="s3cr/t+x", method="client_secret_basic")


@pytest.fixture
async def http():
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        yield client


def _metadata(**over) -> AuthServerMetadata:
    return AuthServerMetadata.model_validate({**AS_DOC, **over})


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode("utf-8"), keep_blank_values=True).items()}


async def _refresh(http: httpx.AsyncClient, client: ClientAuth = PUBLIC, refresh_token: str = "rt-1") -> TokenGrant:
    return await refresh_grant(token_endpoint=TOKEN, client=client, refresh_token=refresh_token, resource=None, http=http, now=NOW)


# ---- WWW-Authenticate and the discovery URL lists ------------------------------------------------------------------------------------------------------------------------


def test_www_authenticate_is_parsed():
    assert parse_www_authenticate('Bearer resource_metadata="https://m/prm", scope="a b", error="invalid_token"') == {
        "resource_metadata": "https://m/prm", "scope": "a b", "error": "invalid_token",
    }
    assert parse_www_authenticate(None) == {} and parse_www_authenticate("") == {}
    assert parse_www_authenticate('Basic realm="x"') == {}


def test_www_authenticate_reads_the_bearer_challenge_among_others():
    header = 'Basic realm="files", Bearer realm="mcp", scope=mcp, error_description="say \\"hi\\", then go"'
    assert parse_www_authenticate(header) == {"realm": "mcp", "scope": "mcp", "error_description": 'say "hi", then go'}
    assert parse_www_authenticate('bearer Scope="a"') == {"scope": "a"}, "the scheme and the parameter names are case-insensitive"
    assert parse_www_authenticate('Bearer scope="a", Basic realm="b"') == {"scope": "a"}, "the next challenge's parameters are not Bearer's"


def test_protected_resource_urls_order():
    assert protected_resource_urls("https://m.example/mcp", "https://m.example/prm") == [
        "https://m.example/prm",
        "https://m.example/.well-known/oauth-protected-resource/mcp",
        "https://m.example/.well-known/oauth-protected-resource",
    ]


def test_protected_resource_urls_without_a_path_and_with_odd_inputs():
    assert protected_resource_urls("https://m.example/", None) == [PRM_ROOT]
    assert protected_resource_urls("https://m.example", None) == [PRM_ROOT]
    assert protected_resource_urls(MCP, PRM_PATH) == [PRM_PATH, PRM_ROOT], "a header URL that is the path-aware one is fetched once"
    # No trailing "/", no query and no credentials in what is built; a header URL that is not an http(s) URL is never fetched.
    assert protected_resource_urls("https://user:pw@M.Example:8443/a/mcp/?key=1", "javascript:alert(1)") == [
        "https://m.example:8443/.well-known/oauth-protected-resource/a/mcp",
        "https://m.example:8443/.well-known/oauth-protected-resource",
    ]


def test_auth_server_metadata_urls_order():
    assert auth_server_metadata_urls("https://idp.example/tenant1") == [AS_OAUTH, AS_OIDC_INSERTED, AS_OIDC_APPENDED]
    assert auth_server_metadata_urls("https://idp.example") == [
        "https://idp.example/.well-known/oauth-authorization-server",
        "https://idp.example/.well-known/openid-configuration",
    ]
    assert auth_server_metadata_urls("https://idp.example/") == auth_server_metadata_urls("https://idp.example")
    assert auth_server_metadata_urls("https://idp.example/tenant1/") == auth_server_metadata_urls(ISSUER)


# ---- discovery -----------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_discover_uses_the_www_authenticate_metadata_first(http):
    with respx.mock(assert_all_called=False) as router:
        from_header = router.get("https://m.example/prm").mock(return_value=httpx.Response(200, json=PRM_DOC))
        path_aware = router.get(PRM_PATH).mock(return_value=httpx.Response(200, json=PRM_DOC))
        router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json=AS_DOC))
        found = await discover(MCP, www_authenticate='Bearer resource_metadata="https://m.example/prm", scope="read"', http=http)
    assert from_header.called and not path_aware.called
    assert found.metadata.token_endpoint == TOKEN and found.protected_resource.authorization_servers == [ISSUER]
    assert found.challenged_scope == "read" and found.uses_resource_indicator is True


async def test_discover_falls_back_to_openid_configuration(http):
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(404))
        router.get(PRM_ROOT).mock(return_value=httpx.Response(200, json=PRM_DOC))
        oauth = router.get(AS_OAUTH).mock(return_value=httpx.Response(404))
        inserted = router.get(AS_OIDC_INSERTED).mock(return_value=httpx.Response(200, text="<html>an app page, not JSON</html>"))
        appended = router.get(AS_OIDC_APPENDED).mock(return_value=httpx.Response(200, json=AS_DOC))
        found = await discover(MCP, www_authenticate=None, http=http)
    assert oauth.called and inserted.called and appended.called
    assert found.metadata.issuer == ISSUER and found.challenged_scope is None and found.uses_resource_indicator


async def test_discover_without_protected_resource_metadata_uses_the_server_origin(http):
    legacy = {**AS_DOC, "issuer": "https://m.example/", "authorization_endpoint": "https://m.example/authorize", "token_endpoint": "https://m.example/token"}
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(404))
        router.get(PRM_ROOT).mock(return_value=httpx.Response(404))
        router.get("https://m.example/.well-known/oauth-authorization-server").mock(return_value=httpx.Response(200, json=legacy))
        found = await discover(MCP, www_authenticate=None, http=http)
    assert found.protected_resource is None and found.uses_resource_indicator is False
    assert found.metadata.token_endpoint == "https://m.example/token", "a root issuer with a trailing slash names the same origin"


async def test_a_protected_resource_document_for_another_server_is_refused(http):
    """Audit G4-07, RFC 9728 section 3.3: an MCP server may not aim its 401 at another server's metadata. A document whose ``resource`` is not this
    server is not used, and the authorization server it names is never asked."""
    other = {**PRM_DOC, "resource": "https://another-server.example/mcp"}
    with respx.mock(assert_all_called=False) as router:
        router.get("https://another-server.example/prm").mock(return_value=httpx.Response(200, json=other))
        as_doc = router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json=AS_DOC))
        with pytest.raises(BadRequestError, match=r"is for the resource https://another-server\.example/mcp, not https://m\.example/mcp.*RFC 9728"):
            await discover(MCP, www_authenticate='Bearer resource_metadata="https://another-server.example/prm"', http=http)
    assert not as_doc.called


@pytest.mark.parametrize("resource", [None, "", "https://m.example/other", "https://m.example/mcp-v2", "http://m.example/mcp", "https://m.example:8443/mcp",
                                      "https://evil.example", "https://m.example/mcp?tenant=2"])
async def test_a_protected_resource_document_for_any_other_resource_is_refused(http, resource):
    """No resource, a sibling path, another scheme or port, another host, another query: none is this server's resource."""
    document = {key: value for key, value in PRM_DOC.items() if key != "resource"} if resource is None else {**PRM_DOC, "resource": resource}
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(200, json=document))
        router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json=AS_DOC))
        with pytest.raises(BadRequestError, match="RFC 9728 section 3.3"):
            await discover(MCP, www_authenticate=None, http=http)


@pytest.mark.parametrize("resource", ["https://m.example/mcp", "HTTPS://M.Example/mcp/", "https://m.example:443/mcp", "https://m.example", "https://m.example/"])
async def test_this_servers_resource_and_a_same_origin_parent_are_accepted(http, resource):
    """The server's own URL in any equivalent spelling, and a parent of it on the same origin (a document that covers the whole origin, as many
    servers publish; the MCP SDK's client accepts the same, ``mcp.shared.auth_utils.check_resource_allowed``)."""
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(200, json={**PRM_DOC, "resource": resource}))
        router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json=AS_DOC))
        found = await discover(MCP, www_authenticate=None, http=http)
    assert found.protected_resource.resource == resource and found.metadata.issuer == ISSUER


async def test_the_configured_resource_uri_is_accepted(http):
    configured = "https://m.example/api/tenant-1"
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(200, json={**PRM_DOC, "resource": configured}))
        router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json=AS_DOC))
        found = await discover(MCP, www_authenticate=None, http=http, resource_uri=configured)
        with pytest.raises(BadRequestError, match="RFC 9728"):
            await discover(MCP, www_authenticate=None, http=http)
    assert found.protected_resource.resource == configured


async def test_metadata_naming_another_issuer_is_refused(http):
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(200, json=PRM_DOC))
        router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json={**AS_DOC, "issuer": "https://evil.example"}))
        with pytest.raises(BadRequestError, match=r"names the issuer https://evil\.example, not https://idp\.example/tenant1.*RFC 8414"):
            await discover(MCP, www_authenticate=None, http=http)


async def test_metadata_with_an_endpoint_that_is_not_http_is_not_used(http):
    """A ``javascript:`` authorization endpoint would run in the console that opens the sign-in: such a document is never used."""
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(200, json=PRM_DOC))
        router.get(AS_OAUTH).mock(return_value=httpx.Response(200, json={**AS_DOC, "authorization_endpoint": "javascript:alert(1)"}))
        router.get(AS_OIDC_INSERTED).mock(return_value=httpx.Response(404))
        router.get(AS_OIDC_APPENDED).mock(return_value=httpx.Response(404))
        with pytest.raises(BadRequestError, match="publishes no usable OAuth metadata") as caught:
            await discover(MCP, www_authenticate=None, http=http)
    assert "authorization_endpoint" in str(caught.value) and "javascript" not in str(caught.value)
    for field_name, bad in (("token_endpoint", "file:///etc/passwd"), ("registration_endpoint", "https://user:pw@idp.example/r"), ("issuer", "idp.example")):
        with pytest.raises(ValueError, match=field_name):
            _metadata(**{field_name: bad})


async def test_a_server_without_metadata_is_a_bad_request(http):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://m\.example/.*").mock(return_value=httpx.Response(404))
        with pytest.raises(BadRequestError, match=r"^the authorization server https://m\.example publishes no usable OAuth metadata"):
            await discover(MCP, www_authenticate=None, http=http)


async def test_an_unreachable_authorization_server_is_a_network_error(http):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://m\.example/.*").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NetworkError, match="could not be reached"):
            await discover(MCP, www_authenticate=None, http=http)


# ---- PKCE, the resource and the authorize URL ----------------------------------------------------------------------------------------------------------------------------


def test_s256_is_required():
    for methods in (["plain"], None, []):
        with pytest.raises(BadRequestError, match=r"^the authorization server https://idp\.example/tenant1 does not advertise PKCE S256 \(code_challenge_methods_supported\), which MCP requires$"):
            require_s256(_metadata(code_challenge_methods_supported=methods))
    require_s256(_metadata(code_challenge_methods_supported=["plain", "S256"]))


def test_pkce_pair():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128 and set(verifier) <= set(string.ascii_letters + string.digits + "-._~")
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert pkce_pair()[0] != verifier


def test_the_resource_is_the_full_server_url():
    assert canonical_resource("HTTPS://M.Example/mcp/") == "https://m.example/mcp" and canonical_resource("https://m.example/") == "https://m.example"
    assert canonical_resource("https://m.example:8443/a/mcp#frag") == "https://m.example:8443/a/mcp"
    assert canonical_resource("https://user:pw@m.example/mcp?tenant=1") == "https://m.example/mcp?tenant=1", "credentials never go to the authorization server"


def test_authorization_url_carries_pkce_state_and_resource():
    url = authorization_url(metadata=_metadata(), client_id="c1", redirect_uri="https://p.example/cb", scopes=["a", "b"], state="st", code_challenge="ch", resource=MCP)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ISSUER + "/authorize"
    assert parse_qs(parts.query) == {
        "response_type": ["code"], "client_id": ["c1"], "redirect_uri": ["https://p.example/cb"], "state": ["st"], "code_challenge": ["ch"],
        "code_challenge_method": ["S256"], "scope": ["a b"], "resource": [MCP],
    }
    bare = parse_qs(urlsplit(authorization_url(metadata=_metadata(), client_id="c1", redirect_uri="https://p.example/cb", scopes=[], state="st",
                                               code_challenge="ch", resource=None)).query)
    assert "resource" not in bare and "scope" not in bare


def test_authorization_url_keeps_the_endpoints_own_query_and_overrides_its_protocol_fields():
    metadata = _metadata(authorization_endpoint=ISSUER + "/authorize?tenant=t1&response_type=token")
    query = parse_qs(urlsplit(authorization_url(metadata=metadata, client_id="c1", redirect_uri="https://p.example/cb", scopes=[], state="st",
                                                code_challenge="ch", resource=None)).query)
    assert query["tenant"] == ["t1"] and query["response_type"] == ["code"]


def test_authorization_url_needs_an_authorization_endpoint():
    with pytest.raises(BadRequestError, match="authorization endpoint"):
        authorization_url(metadata=_metadata(authorization_endpoint=None), client_id="c1", redirect_uri="https://p.example/cb", scopes=[], state="st",
                          code_challenge="ch", resource=None)


# ---- registration --------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_register_client_posts_rfc7591_metadata(http):
    with respx.mock() as router:
        route = router.post(ISSUER + "/register").mock(return_value=httpx.Response(201, json={"client_id": "dcr-1", "client_id_issued_at": 1}))
        client = await register_client(metadata=_metadata(), redirect_uri="https://p.example/cb", client_name="primer", http=http)
    assert client == RegisteredClient(client_id="dcr-1", client_secret=None)
    assert json.loads(route.calls[0].request.content) == {
        "client_name": "primer", "redirect_uris": ["https://p.example/cb"], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    }


async def test_register_client_without_an_endpoint_asks_for_a_client_id(http):
    with pytest.raises(BadRequestError, match=r"^the authorization server does not support dynamic client registration; enter a client id from the provider$"):
        await register_client(metadata=_metadata(registration_endpoint=None), redirect_uri="https://p.example/cb", client_name="primer", http=http)


async def test_a_refused_registration_names_the_status_and_the_error_only(http):
    with respx.mock() as router:
        router.post(ISSUER + "/register").mock(return_value=httpx.Response(400, json={"error": "invalid_redirect_uri", "error_description": "no", "debug": "body-xyz"}))
        with pytest.raises(BadRequestError) as caught:
            await register_client(metadata=_metadata(), redirect_uri="https://p.example/cb", client_name="primer", http=http)
    assert str(caught.value) == "registration endpoint answered 400: invalid_redirect_uri: no"


# ---- token grants --------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_the_code_exchange_sends_the_verifier_the_redirect_and_the_resource(http):
    answer = {"access_token": "at-1", "refresh_token": "rt-1", "token_type": "Bearer", "expires_in": 3600, "scope": "read"}
    with respx.mock() as router:
        route = router.post(TOKEN).mock(return_value=httpx.Response(200, json=answer))
        grant = await exchange_code(token_endpoint=TOKEN, client=PUBLIC, code="code-1", redirect_uri="https://p.example/cb", code_verifier="v" * 43,
                                    resource=MCP, http=http, now=NOW)
    request = route.calls[0].request
    assert _form(request) == {"grant_type": "authorization_code", "code": "code-1", "redirect_uri": "https://p.example/cb", "code_verifier": "v" * 43,
                              "resource": MCP, "client_id": "pub-client"}
    assert "authorization" not in request.headers and request.headers["accept"] == "application/json"
    assert grant == TokenGrant(access_token="at-1", refresh_token="rt-1", token_type="Bearer", expires_at=NOW + timedelta(hours=1), scope="read")


async def test_token_errors_never_echo_the_body(http):
    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired", "x": "secret-xyz"}))
        with pytest.raises(TokenEndpointRejected) as caught:
            await _refresh(http)
    exc = caught.value
    assert isinstance(exc, BadRequestError) and exc.error == "invalid_grant" and exc.status == 400
    assert str(exc) == "token endpoint answered 400: invalid_grant: expired" and "secret-xyz" not in str(exc)


async def test_an_error_text_never_carries_a_secret_the_request_sent(http):
    echo = "refresh token rt-secret-123 of client conf client with secret s3cr/t+x (form: s3cr%2Ft%2Bx) is unknown\n" + "x" * 300
    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": echo}))
        with pytest.raises(TokenEndpointRejected) as caught:
            await _refresh(http, client=BASIC, refresh_token="rt-secret-123")
    text = str(caught.value)
    assert "rt-secret-123" not in text and "s3cr/t+x" not in text and "s3cr%2Ft%2Bx" not in text and "[REDACTED]" in text
    assert len(text) <= 200 and text.endswith("...") and "\n" not in text


async def test_token_endpoint_outages_are_transient(http):
    with respx.mock() as router:
        route = router.post(TOKEN)
        route.mock(return_value=httpx.Response(503, json={"error": "temporarily_unavailable"}))
        with pytest.raises(ServerError) as caught:
            await _refresh(http)
        assert not isinstance(caught.value, TokenEndpointRejected) and caught.value.message == "token endpoint answered 503: temporarily_unavailable"
        route.mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NetworkError, match=r"the token endpoint did not answer \(ConnectError\)"):
            await _refresh(http)
        route.mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(NetworkError, match="ReadTimeout"):
            await _refresh(http)


async def test_a_rate_limit_or_a_request_timeout_is_not_a_refusal(http):
    with respx.mock() as router:
        route = router.post(TOKEN)
        route.mock(return_value=httpx.Response(429, json={"error": "slow_down"}))
        with pytest.raises(RateLimitError) as limited:
            await _refresh(http)
        route.mock(return_value=httpx.Response(408))
        with pytest.raises(ServerError) as timed_out:
            await _refresh(http)
    assert not isinstance(limited.value, TokenEndpointRejected) and not isinstance(timed_out.value, TokenEndpointRejected)


async def test_a_token_response_without_expires_in_has_no_expiry(http):
    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "at-1", "token_type": "bearer"}))
        grant = await _refresh(http)
    assert grant.expires_at is None and grant.refresh_token is None and grant.scope is None and grant.token_type == "bearer"


async def test_a_success_without_an_access_token_is_a_server_fault(http):
    with respx.mock() as router:
        route = router.post(TOKEN)
        for answer in (httpx.Response(200, json={"token_type": "Bearer"}), httpx.Response(200, text="<html>ok</html>")):
            route.mock(return_value=answer)
            with pytest.raises(ServerError) as caught:
                await _refresh(http)
            assert not isinstance(caught.value, TokenEndpointRejected)


def test_client_auth_prefers_basic_and_follows_the_metadata():
    assert client_auth_for(None, "c", None) == ClientAuth("c", None, "none")
    assert client_auth_for(None, "c", "s") == ClientAuth("c", "s", "client_secret_basic")
    both = _metadata(token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"])
    assert client_auth_for(both, "c", "s").method == "client_secret_basic"
    post_only = _metadata(token_endpoint_auth_methods_supported=["none", "client_secret_post"])
    assert client_auth_for(post_only, "c", "s").method == "client_secret_post"
    assert client_auth_for(_metadata(token_endpoint_auth_methods_supported=["private_key_jwt"]), "c", "s").method == "client_secret_basic"
    assert client_auth_for(post_only, "c", "").method == "none"


async def test_client_credentials_grant_body(http):
    with respx.mock() as router:
        route = router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "at-cc", "expires_in": 60}))
        grant = await client_credentials_grant(token_endpoint=TOKEN, client=BASIC, scopes=["read", "write"], resource=MCP, http=http, now=NOW)
    request = route.calls[0].request
    assert _form(request) == {"grant_type": "client_credentials", "scope": "read write", "resource": MCP}
    scheme, _, value = request.headers["authorization"].partition(" ")
    assert scheme == "Basic" and base64.b64decode(value).decode("utf-8") == "conf%20client:s3cr%2Ft%2Bx", "RFC 6749 2.3.1: form-encoded, then base64"
    assert grant.access_token == "at-cc" and grant.expires_at == NOW + timedelta(seconds=60)


async def test_client_secret_post_puts_the_credentials_in_the_body(http):
    with respx.mock() as router:
        route = router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "at-cc"}))
        await client_credentials_grant(token_endpoint=TOKEN, client=ClientAuth("c1", "s1", "client_secret_post"), scopes=[], resource=None, http=http, now=NOW)
    request = route.calls[0].request
    assert _form(request) == {"grant_type": "client_credentials", "client_id": "c1", "client_secret": "s1"} and "authorization" not in request.headers


async def test_refresh_grant_sends_no_scope(http):
    with respx.mock() as router:
        route = router.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}))
        grant = await refresh_grant(token_endpoint=TOKEN, client=PUBLIC, refresh_token="rt-1", resource=MCP, http=http, now=NOW)
    assert _form(route.calls[0].request) == {"grant_type": "refresh_token", "refresh_token": "rt-1", "resource": MCP, "client_id": "pub-client"}
    assert grant.refresh_token == "rt-2" and grant.expires_at == NOW + timedelta(hours=1)


async def test_revoke_never_raises(http):
    with respx.mock() as router:
        route = router.post(REVOKE)
        route.mock(return_value=httpx.Response(500))
        assert await revoke(revocation_endpoint=REVOKE, client=PUBLIC, token="rt-1", token_type_hint="refresh_token", http=http) is False
        route.mock(side_effect=httpx.ConnectError("refused"))
        assert await revoke(revocation_endpoint=REVOKE, client=PUBLIC, token="rt-1", token_type_hint="refresh_token", http=http) is False
        route.mock(return_value=httpx.Response(200))
        assert await revoke(revocation_endpoint=REVOKE, client=PUBLIC, token="rt-1", token_type_hint="refresh_token", http=http) is True
    assert _form(route.calls[-1].request) == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "pub-client"}


async def test_probe_challenge_reads_the_401_header(http):
    challenge = 'Bearer resource_metadata="https://m.example/prm"'
    with respx.mock() as router:
        route = router.post(MCP)
        route.mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": challenge}))
        assert await probe_challenge(MCP, headers={"X-Tenant": "t1", "Authorization": "Bearer stale"}, http=http) == challenge
        sent = route.calls[0].request
        assert sent.headers["x-tenant"] == "t1" and "authorization" not in sent.headers
        assert sent.headers["accept"] == "application/json, text/event-stream" and json.loads(sent.content)["method"] == "initialize"
        route.mock(return_value=httpx.Response(200, json={}))
        assert await probe_challenge(MCP, headers={}, http=http) is None
        route.mock(side_effect=httpx.ConnectError("refused"))
        assert await probe_challenge(MCP, headers={}, http=http) is None


# ---- answer bodies are read to a cap (audit G4-15) ------------------------------------------------------------------------------------------------------------------------

MIB = 1024 * 1024


class _BigBody(httpx.AsyncByteStream):
    """An answer body of ``total`` bytes sent in 64 KiB chunks: a JSON object, padded, that would parse if read whole. Counts what was read."""

    def __init__(self, document: dict, total: int) -> None:
        head = json.dumps(document).encode()[:-1] + b', "padding": "'
        self._parts = [head, *([b"x" * 65536] * ((total - len(head)) // 65536)), b'"}']
        self.read = 0

    async def __aiter__(self):
        for part in self._parts:
            self.read += len(part)
            yield part


def _big(document: dict, total: int = 8 * MIB) -> tuple[httpx.Response, _BigBody]:
    body = _BigBody(document, total)
    return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=body), body


async def test_a_token_answer_over_the_cap_is_refused_unread(http):
    """A server named by the MCP metadata could answer with a body of any size inside the timeout; it is read to 1 MiB and no further."""
    response, body = _big({"access_token": "at-1", "token_type": "Bearer"})
    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=response)
        with pytest.raises(ServerError, match=r"token endpoint answered 200 with a body larger than 1 MiB"):
            await _refresh(http)
    assert body.read <= 2 * MIB, f"{body.read} bytes were read"


async def test_a_registration_answer_over_the_cap_is_refused_unread(http):
    response, body = _big({"client_id": "dcr-1"})
    with respx.mock() as router:
        router.post(ISSUER + "/register").mock(return_value=response)
        with pytest.raises(ServerError, match=r"registration endpoint answered 200 with a body larger than 1 MiB"):
            await register_client(metadata=_metadata(), redirect_uri="https://p.example/cb", client_name="primer", http=http)
    assert body.read <= 2 * MIB


async def test_a_discovery_document_over_the_cap_is_not_used(http):
    response, body = _big(AS_DOC)
    with respx.mock(assert_all_called=False) as router:
        router.get(PRM_PATH).mock(return_value=httpx.Response(200, json=PRM_DOC))
        router.get(AS_OAUTH).mock(return_value=response)
        router.get(AS_OIDC_INSERTED).mock(return_value=httpx.Response(404))
        router.get(AS_OIDC_APPENDED).mock(return_value=httpx.Response(404))
        with pytest.raises(BadRequestError, match="publishes no usable OAuth metadata"):
            await discover(MCP, www_authenticate=None, http=http)
    assert body.read <= 2 * MIB


async def test_an_error_answer_over_the_cap_carries_no_fields(http):
    big = _BigBody({"error": "invalid_grant", "error_description": "big"}, 4 * MIB)
    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=httpx.Response(400, headers={"Content-Type": "application/json"}, stream=big))
        with pytest.raises(TokenEndpointRejected) as caught:
            await _refresh(http)
    assert str(caught.value) == "token endpoint answered 400" and caught.value.error is None and big.read <= 2 * MIB


async def test_a_body_under_the_cap_is_read_whole(http):
    response, _ = _big({"access_token": "at-1", "token_type": "Bearer"}, total=MIB - 4096)
    with respx.mock() as router:
        router.post(TOKEN).mock(return_value=response)
        assert (await _refresh(http)).access_token == "at-1"


async def test_a_revocation_answer_is_never_read(http):
    response, body = _big({"anything": 1})
    with respx.mock() as router:
        router.post(REVOKE).mock(return_value=response)
        assert await revoke(revocation_endpoint=REVOKE, client=PUBLIC, token="rt-1", token_type_hint="refresh_token", http=http) is True
    assert body.read <= 2 * MIB


def test_no_secret_shows_in_a_repr():
    values = (
        TokenGrant(access_token="at-plain", refresh_token="rt-plain", token_type="Bearer", expires_at=None, scope=None),
        ClientAuth("c1", "secret-plain", "client_secret_basic"),
        RegisteredClient("c1", "secret-plain"),
    )
    for value in values:
        assert "plain" not in repr(value), repr(value)


# ---- against the hermetic mock -------------------------------------------------------------------------------------------------------------------------------------------


async def test_discovery_and_a_grant_against_the_mock(mock_oauth_mcp, http):
    challenge = await probe_challenge(mock_oauth_mcp.mcp_url, headers={}, http=http)
    found = await discover(mock_oauth_mcp.mcp_url, www_authenticate=challenge, http=http)
    require_s256(found.metadata)
    assert found.metadata.issuer == mock_oauth_mcp.issuer and found.uses_resource_indicator and found.challenged_scope == "mcp"
    assert found.protected_resource.resource == canonical_resource(mock_oauth_mcp.mcp_url)
    client = client_auth_for(found.metadata, "cc-client", "cc-secret")
    grant = await client_credentials_grant(token_endpoint=found.metadata.token_endpoint, client=client, scopes=["mcp"],
                                           resource=canonical_resource(mock_oauth_mcp.mcp_url), http=http, now=NOW)
    assert grant.access_token.startswith("mock-at-") and grant.expires_at == NOW + timedelta(seconds=3600) and grant.refresh_token is None
