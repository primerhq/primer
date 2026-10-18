"""The hermetic mock OAuth authorization server and MCP resource server (plan 07, Task 2).

Every later OAuth test (the protocol client, the service, the routes, the journeys) is written against this mock, so this file pins the contract it
offers: path-aware metadata documents, the 401 challenge, PKCE S256, single-use codes, rotating refresh tokens, client credentials, revocation and the
knobs a test turns. The mock is test support, not product code: it speaks the specs strictly on purpose, so that a client that gets a detail wrong fails
here with a precise answer instead of passing against a forgiving server.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests._support.mock_oauth_mcp import MockOAuthServer, MockOAuthState
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp, mock_oauth_mcp_lan  # noqa: F401 - the fixtures under test

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
REDIRECT = "http://127.0.0.1:9/cb"          # nothing listens there: the tests read the redirect and never follow it
PRE = ("pre-client", "pre-secret")
CC = ("cc-client", "cc-secret")


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize(server: MockOAuthServer, challenge: str, **overrides: str | None) -> httpx.Response:
    """GET the authorization endpoint without following the redirect. A value of ``None`` in ``overrides`` drops that parameter."""
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": PRE[0],
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
        "scope": "mcp",
        "resource": server.mcp_url,
    }
    params.update(overrides)
    return httpx.get(server.issuer + "/authorize", params={k: v for k, v in params.items() if v is not None}, follow_redirects=False)


def _redirect_query(response: httpx.Response) -> dict[str, list[str]]:
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)


def _code(server: MockOAuthServer, challenge: str, **overrides: str | None) -> str:
    return _redirect_query(_authorize(server, challenge, **overrides))["code"][0]


def _token(server: MockOAuthServer, auth: tuple[str, str] | None = None, **form: str) -> httpx.Response:
    return httpx.post(server.issuer + "/token", data=form, auth=auth)


def _exchange(server: MockOAuthServer, code: str, verifier: str, auth: tuple[str, str] | None = PRE, **extra: str) -> httpx.Response:
    return _token(server, auth, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, code_verifier=verifier, **extra)


def _sign_in(server: MockOAuthServer) -> dict:
    """A whole browser-less sign-in as the pre-registered client; the token answer."""
    verifier, challenge = _pkce()
    answer = _exchange(server, _code(server, challenge), verifier)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _refresh(server: MockOAuthServer, refresh_token: str, auth: tuple[str, str] | None = PRE) -> httpx.Response:
    return _token(server, auth, grant_type="refresh_token", refresh_token=refresh_token)


def _mcp(server: MockOAuthServer, token: str | None = None, body: dict | None = None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(server.mcp_url, json=body or INITIALIZE, headers=headers)


def _client_credentials_token(server: MockOAuthServer) -> str:
    answer = _token(server, CC, grant_type="client_credentials")
    assert answer.status_code == 200, answer.text
    return answer.json()["access_token"]


# ---- the resource server --------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_mcp_endpoint_challenges_without_a_token(mock_oauth_mcp):
    r = httpx.post(mock_oauth_mcp.mcp_url, json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == f'Bearer resource_metadata="{mock_oauth_mcp.base_url}/.well-known/oauth-protected-resource/mcp", scope="mcp"'


def test_an_unknown_token_gets_the_same_challenge(mock_oauth_mcp):
    r = _mcp(mock_oauth_mcp, "not-a-token-the-server-issued")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Bearer resource_metadata=")


def test_a_token_opens_the_mcp_endpoint_until_revoked(mock_oauth_mcp):
    token = _client_credentials_token(mock_oauth_mcp)
    opened = _mcp(mock_oauth_mcp, token)
    assert opened.status_code == 200
    assert opened.json()["result"]["serverInfo"]["name"] == "mock-oauth-mcp"
    assert token in mock_oauth_mcp.state.seen_tokens

    mock_oauth_mcp.state.revoke_all()

    assert _mcp(mock_oauth_mcp, token).status_code == 401


def test_the_mcp_endpoint_serves_the_shared_fixture_tools(mock_oauth_mcp):
    token = _client_credentials_token(mock_oauth_mcp)
    listed = _mcp(mock_oauth_mcp, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert sorted(t["name"] for t in listed.json()["result"]["tools"]) == ["bump", "echo"]
    called = _mcp(mock_oauth_mcp, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})
    assert called.json()["result"]["content"][0]["text"] == "hi"


def test_an_expired_access_token_is_challenged_but_its_refresh_token_still_works(mock_oauth_mcp):
    answer = _sign_in(mock_oauth_mcp)
    assert _mcp(mock_oauth_mcp, answer["access_token"]).status_code == 200

    mock_oauth_mcp.state.expire_access_tokens()

    expired = _mcp(mock_oauth_mcp, answer["access_token"])
    assert expired.status_code == 401 and expired.headers["WWW-Authenticate"].startswith("Bearer resource_metadata=")
    renewed = _refresh(mock_oauth_mcp, answer["refresh_token"]).json()
    assert _mcp(mock_oauth_mcp, renewed["access_token"]).status_code == 200


def test_an_access_token_really_ends_at_its_lifetime(mock_oauth_mcp):
    assert _mcp(mock_oauth_mcp, _client_credentials_token(mock_oauth_mcp)).status_code == 200      # warm the MCP transport up: the one-second token below must not spend its life on a cold start
    mock_oauth_mcp.state.access_token_ttl_s = 1
    answer = _sign_in(mock_oauth_mcp)
    assert answer["expires_in"] == 1
    assert _mcp(mock_oauth_mcp, answer["access_token"]).status_code == 200
    time.sleep(1.2)
    assert _mcp(mock_oauth_mcp, answer["access_token"]).status_code == 401


# ---- the metadata documents -----------------------------------------------------------------------------------------------------------------------------------------------


def test_the_metadata_documents_are_path_aware(mock_oauth_mcp):
    server = mock_oauth_mcp
    resource = httpx.get(server.base_url + "/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json() == {"resource": server.mcp_url, "authorization_servers": [server.issuer], "scopes_supported": ["mcp"]}

    meta = httpx.get(server.base_url + "/.well-known/oauth-authorization-server/as")
    assert meta.status_code == 200
    doc = meta.json()
    assert doc["issuer"] == server.issuer
    assert doc["authorization_endpoint"] == server.issuer + "/authorize"
    assert doc["token_endpoint"] == server.issuer + "/token"
    assert doc["registration_endpoint"] == server.issuer + "/register"
    assert doc["revocation_endpoint"] == server.issuer + "/revoke"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none", "client_secret_basic", "client_secret_post"]
    assert doc["grant_types_supported"] == ["authorization_code", "refresh_token", "client_credentials"]

    for root in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        assert httpx.get(server.base_url + root).status_code == 404, root


# ---- dynamic client registration ------------------------------------------------------------------------------------------------------------------------------------------


def _register(server: MockOAuthServer, **metadata) -> httpx.Response:
    body = {"client_name": "primer", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}
    body.update(metadata)
    return httpx.post(server.issuer + "/register", json=body)


def test_registration_issues_a_public_client_and_records_the_metadata(mock_oauth_mcp):
    first, second = _register(mock_oauth_mcp), _register(mock_oauth_mcp)
    assert first.status_code == 201
    body = first.json()
    assert body["client_id"] and "client_secret" not in body and body["token_endpoint_auth_method"] == "none"
    assert body["client_id"] != second.json()["client_id"]
    assert [r["redirect_uris"] for r in mock_oauth_mcp.state.registrations] == [[REDIRECT], [REDIRECT]]

    verifier, challenge = _pkce()
    code = _code(mock_oauth_mcp, challenge, client_id=body["client_id"])
    answer = _exchange(mock_oauth_mcp, code, verifier, auth=None, client_id=body["client_id"])
    assert answer.status_code == 200 and answer.json()["refresh_token"]


@pytest.mark.parametrize("metadata", [{"redirect_uris": []}, {"redirect_uris": "http://x/cb"}, {"redirect_uris": None}, {"token_endpoint_auth_method": "client_secret_basic"}])
def test_registration_refuses_metadata_it_cannot_honour(mock_oauth_mcp, metadata):
    refused = _register(mock_oauth_mcp, **metadata)
    assert refused.status_code == 400 and refused.json()["error"].startswith("invalid_")
    assert mock_oauth_mcp.state.registrations == []


def test_a_registered_client_may_only_use_its_registered_redirect(mock_oauth_mcp):
    client_id = _register(mock_oauth_mcp).json()["client_id"]
    _, challenge = _pkce()
    refused = _authorize(mock_oauth_mcp, challenge, client_id=client_id, redirect_uri="http://127.0.0.1:9/elsewhere")
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_request"
    # a static client may use any redirect
    assert _authorize(mock_oauth_mcp, challenge, redirect_uri="http://127.0.0.1:9/elsewhere").status_code == 302


# ---- authorization code + PKCE --------------------------------------------------------------------------------------------------------------------------------------------


def test_authorize_then_token_with_pkce(mock_oauth_mcp):
    verifier, challenge = _pkce()
    redirect = _redirect_query(_authorize(mock_oauth_mcp, challenge, state="s-77"))
    assert redirect["state"] == ["s-77"] and set(redirect) == {"code", "state"}

    good = _exchange(mock_oauth_mcp, redirect["code"][0], verifier)
    assert good.status_code == 200
    body = good.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600 and body["scope"] == "mcp"
    assert body["access_token"] and body["refresh_token"]
    assert good.headers["Cache-Control"] == "no-store"

    other_verifier, _ = _pkce()
    bad = _exchange(mock_oauth_mcp, _code(mock_oauth_mcp, challenge), other_verifier)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"


def test_a_code_is_single_use(mock_oauth_mcp):
    verifier, challenge = _pkce()
    code = _code(mock_oauth_mcp, challenge)
    assert _exchange(mock_oauth_mcp, code, verifier).status_code == 200
    again = _exchange(mock_oauth_mcp, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_a_code_belongs_to_one_client_and_one_redirect(mock_oauth_mcp):
    verifier, challenge = _pkce()
    other = _register(mock_oauth_mcp).json()["client_id"]
    elsewhere = _token(mock_oauth_mcp, PRE, grant_type="authorization_code", code=_code(mock_oauth_mcp, challenge), redirect_uri="http://127.0.0.1:9/other", code_verifier=verifier)
    assert elsewhere.status_code == 400 and elsewhere.json()["error"] == "invalid_grant"
    stolen = _exchange(mock_oauth_mcp, _code(mock_oauth_mcp, challenge), verifier, auth=None, client_id=other)
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "override",
    [
        {"client_id": "nobody"},
        {"response_type": "token"},
        {"response_type": None},
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"code_challenge": None},
        {"state": None},
        {"redirect_uri": None},
        {"resource": "https://elsewhere.example/mcp"},
        {"resource": None},
    ],
    ids=["unknown client", "wrong response_type", "no response_type", "plain PKCE", "no PKCE method", "no challenge", "no state", "no redirect", "wrong resource", "no resource"],
)
def test_authorize_refuses_what_a_client_must_get_right(mock_oauth_mcp, override):
    _, challenge = _pkce()
    refused = _authorize(mock_oauth_mcp, challenge, **override)
    assert refused.status_code == 400, refused.headers.get("location")
    assert refused.json()["error"] in ("invalid_request", "unauthorized_client")


def test_deny_next_authorize(mock_oauth_mcp):
    mock_oauth_mcp.state.deny_next_authorize = True
    _, challenge = _pkce()
    denied = _redirect_query(_authorize(mock_oauth_mcp, challenge, state="s-9"))
    assert denied == {"error": ["access_denied"], "state": ["s-9"]}
    # only the next one is denied
    assert "code" in _redirect_query(_authorize(mock_oauth_mcp, challenge, state="s-10"))
    assert mock_oauth_mcp.state.deny_next_authorize is False


# ---- refresh tokens -------------------------------------------------------------------------------------------------------------------------------------------------------


def test_refresh_tokens_rotate_and_a_reuse_fails(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    second = _refresh(mock_oauth_mcp, first["refresh_token"])
    assert second.status_code == 200
    renewed = second.json()
    assert renewed["access_token"] != first["access_token"] and renewed["refresh_token"] != first["refresh_token"]
    assert renewed["scope"] == first["scope"]

    reuse = _refresh(mock_oauth_mcp, first["refresh_token"])
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert _refresh(mock_oauth_mcp, renewed["refresh_token"]).status_code == 200


def test_without_rotation_the_refresh_token_stays_valid_and_is_not_repeated(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    mock_oauth_mcp.state.rotate_refresh_tokens = False
    for _ in range(2):
        again = _refresh(mock_oauth_mcp, first["refresh_token"])
        assert again.status_code == 200 and "refresh_token" not in again.json()


def test_a_server_that_issues_no_refresh_token_keeps_the_old_one_valid(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    mock_oauth_mcp.state.issue_refresh_tokens = False
    answer = _refresh(mock_oauth_mcp, first["refresh_token"])
    assert answer.status_code == 200 and "refresh_token" not in answer.json()
    assert _refresh(mock_oauth_mcp, first["refresh_token"]).status_code == 200      # not rotated away: no replacement was issued
    assert "refresh_token" not in _sign_in(mock_oauth_mcp)                          # and a new sign-in gets none


def test_a_refresh_token_belongs_to_its_client(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    other = _register(mock_oauth_mcp).json()["client_id"]
    refused = _token(mock_oauth_mcp, None, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=other)
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"


# ---- client credentials and client authentication -------------------------------------------------------------------------------------------------------------------------


def test_client_credentials(mock_oauth_mcp):
    basic = _token(mock_oauth_mcp, CC, grant_type="client_credentials", scope="mcp")
    assert basic.status_code == 200
    body = basic.json()
    assert body["token_type"] == "Bearer" and body["access_token"] and "refresh_token" not in body

    post = _token(mock_oauth_mcp, None, grant_type="client_credentials", client_id=CC[0], client_secret=CC[1])
    assert post.status_code == 200

    wrong = _token(mock_oauth_mcp, (CC[0], "wrong"), grant_type="client_credentials")
    assert wrong.status_code == 401 and wrong.json()["error"] == "invalid_client"
    assert wrong.headers["WWW-Authenticate"].startswith("Basic")
    assert _token(mock_oauth_mcp, None, grant_type="client_credentials", client_id=CC[0]).status_code == 401


def test_client_credentials_are_for_static_clients_only(mock_oauth_mcp):
    registered = _register(mock_oauth_mcp).json()["client_id"]
    refused = _token(mock_oauth_mcp, None, grant_type="client_credentials", client_id=registered)
    assert refused.status_code == 400 and refused.json()["error"] == "unauthorized_client"


def test_an_unknown_grant_type_is_refused(mock_oauth_mcp):
    refused = _token(mock_oauth_mcp, CC, grant_type="password", username="u", password="p")
    assert refused.status_code == 400 and refused.json()["error"] == "unsupported_grant_type"


def test_a_confidential_static_client_must_authenticate_at_the_token_endpoint(mock_oauth_mcp):
    verifier, challenge = _pkce()
    refused = _exchange(mock_oauth_mcp, _code(mock_oauth_mcp, challenge), verifier, auth=None, client_id=PRE[0])
    assert refused.status_code == 401 and refused.json()["error"] == "invalid_client"


def test_a_static_client_with_an_empty_secret_is_public(mock_oauth_mcp):
    mock_oauth_mcp.state.static_clients["pub-client"] = ""
    verifier, challenge = _pkce()
    code = _code(mock_oauth_mcp, challenge, client_id="pub-client")
    assert _exchange(mock_oauth_mcp, code, verifier, auth=None, client_id="pub-client").status_code == 200


def test_the_token_endpoint_wants_a_form_and_no_repeated_parameters(mock_oauth_mcp):
    as_json = httpx.post(mock_oauth_mcp.issuer + "/token", json={"grant_type": "client_credentials"}, auth=CC)
    assert as_json.status_code == 400 and as_json.json()["error"] == "invalid_request"
    repeated = httpx.post(mock_oauth_mcp.issuer + "/token", content="grant_type=client_credentials&scope=a&scope=b", headers={"Content-Type": "application/x-www-form-urlencoded"}, auth=CC)
    assert repeated.status_code == 400 and repeated.json()["error"] == "invalid_request"


def test_a_token_request_for_another_resource_is_refused(mock_oauth_mcp):
    refused = _token(mock_oauth_mcp, CC, grant_type="client_credentials", resource="https://elsewhere.example/mcp")
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_target"
    assert _token(mock_oauth_mcp, CC, grant_type="client_credentials", resource=mock_oauth_mcp.mcp_url).status_code == 200


# ---- the knobs and the records --------------------------------------------------------------------------------------------------------------------------------------------


def test_the_token_endpoint_records_the_form_it_was_sent(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    _refresh(mock_oauth_mcp, first["refresh_token"])
    grants = [r["grant_type"] for r in mock_oauth_mcp.state.token_requests]
    assert grants == ["authorization_code", "refresh_token"]
    assert mock_oauth_mcp.state.token_requests[1]["refresh_token"] == first["refresh_token"]


def test_a_forced_status_answers_without_touching_the_books(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    mock_oauth_mcp.state.token_status_override = 503
    down = _refresh(mock_oauth_mcp, first["refresh_token"])
    assert down.status_code == 503 and down.json()["error"] == "server_error"
    mock_oauth_mcp.state.token_status_override = 400
    assert _refresh(mock_oauth_mcp, first["refresh_token"]).json()["error"] == "invalid_request"
    mock_oauth_mcp.state.token_status_override = None
    assert _refresh(mock_oauth_mcp, first["refresh_token"]).status_code == 200      # the refresh token was not spent by the forced answers
    assert [r["grant_type"] for r in mock_oauth_mcp.state.token_requests].count("refresh_token") == 3


def test_a_token_delay_holds_the_answer(mock_oauth_mcp):
    mock_oauth_mcp.state.token_delay_s = 0.3
    started = time.monotonic()
    _token(mock_oauth_mcp, CC, grant_type="client_credentials")
    assert time.monotonic() - started >= 0.3


def test_revoke_all_drops_every_access_and_refresh_token(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    mock_oauth_mcp.state.revoke_all()
    assert _mcp(mock_oauth_mcp, first["access_token"]).status_code == 401
    refused = _refresh(mock_oauth_mcp, first["refresh_token"])
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"


def test_revocation_is_recorded_and_ends_the_token(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    revoked = httpx.post(mock_oauth_mcp.issuer + "/revoke", data={"token": first["refresh_token"], "token_type_hint": "refresh_token"}, auth=PRE)
    assert revoked.status_code == 200
    assert mock_oauth_mcp.state.revocations == [first["refresh_token"]]
    assert _refresh(mock_oauth_mcp, first["refresh_token"]).status_code == 400
    assert _mcp(mock_oauth_mcp, first["access_token"]).status_code == 401          # revoking a refresh token takes its grant's access tokens with it
    # an unknown token is still a 200 (RFC 7009) and is recorded
    assert httpx.post(mock_oauth_mcp.issuer + "/revoke", data={"token": "who-knows"}, auth=PRE).status_code == 200
    assert mock_oauth_mcp.state.revocations[-1] == "who-knows"


def test_revocation_needs_client_authentication(mock_oauth_mcp):
    first = _sign_in(mock_oauth_mcp)
    refused = httpx.post(mock_oauth_mcp.issuer + "/revoke", data={"token": first["refresh_token"]}, auth=(PRE[0], "wrong"))
    assert refused.status_code == 401
    assert mock_oauth_mcp.state.revocations == []


def test_each_server_has_its_own_state(mock_oauth_mcp):
    assert isinstance(mock_oauth_mcp.state, MockOAuthState)
    assert mock_oauth_mcp.state.static_clients == {"cc-client": "cc-secret", "pre-client": "pre-secret"}
    assert (mock_oauth_mcp.mcp_url, mock_oauth_mcp.issuer) == (mock_oauth_mcp.base_url + "/mcp", mock_oauth_mcp.base_url + "/as")
    assert mock_oauth_mcp.state.token_requests == mock_oauth_mcp.state.seen_tokens == mock_oauth_mcp.state.revocations == []


# ---- the LAN variant ------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_lan_variant_advertises_its_own_base_url(mock_oauth_mcp_lan):
    server = mock_oauth_mcp_lan
    port = urlsplit(server.base_url).port
    document = httpx.get(f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp").json()
    assert document["resource"] == server.mcp_url and server.mcp_url == server.base_url + "/mcp"
    meta = httpx.get(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server/as").json()
    assert meta["token_endpoint"] == server.base_url + "/as/token"
