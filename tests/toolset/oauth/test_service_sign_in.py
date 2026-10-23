"""The shared MCP sign-in: start and finish a browser sign-in, connect client credentials, disconnect, read the status (plan 07, Task 7; design 12.3,
12.4, 12.5 and 20 Q2).

Against the hermetic mock OAuth and MCP server, over the in-memory storage fake, with a clock the test moves. Review Focus 3 is pinned here: a callback
that is replayed, late, declined at the provider, or for a toolset edited meanwhile writes no tokens and ends with its own code. So is the rule that no
token, code, verifier or state reaches the log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from primer.common.secret_box import SecretBox
from primer.model.common import dump_for_storage
from primer.model.except_ import BadRequestError, ConfigError
from primer.model.mcp_oauth import McpOAuthPendingSignIn
from primer.model.provider import Toolset
from primer.toolset.oauth.service import (
    EXPIRED_NO_REFRESH,
    KEY_CHANGED,
    NOT_CONNECTED,
    PENDING_TTL,
    SETTINGS_CHANGED,
    OAuthTarget,
    SignInError,
    ToolsetOAuthService,
    config_fingerprint,
)
from primer.toolset.oauth.store import McpOAuthStore, pending_key
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401 - the fixture
from tests.toolset.oauth._service_helpers import (
    KEY,
    NOW,
    OTHER_KEY,
    REDIRECT,
    Clock,
    authorize,
    connect,
    make_toolset,
    mcp_answers,
    target_of,
    token_row,
)

OTHER_REDIRECT = "http://127.0.0.1:9/v1/toolsets/kb/oauth/callback-moved"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox.from_key_material(KEY)


@pytest.fixture
def store(fake_storage_provider, box) -> McpOAuthStore:
    return McpOAuthStore(fake_storage_provider, box)


@pytest.fixture
async def svc(fake_storage_provider, box, clock):
    service = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=box, clock=clock, owner_id="pod-a")
    yield service
    await service.aclose()


@pytest.fixture
def kb(mock_oauth_mcp):
    return make_toolset(mock_oauth_mcp.mcp_url, oauth={})


@pytest.fixture
def target(kb) -> OAuthTarget:
    return target_of(kb)


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


# ---- the target and its fingerprint --------------------------------------------------------------------------------------------------------------------------------------


def test_the_target_of_a_toolset(mock_oauth_mcp):
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={"scopes": ["a"]}, headers={"X-Tenant": "t1", "Authorization": "Bearer configured"}, transport="sse")
    target = target_of(toolset)
    assert (target.toolset_id, target.server_url, target.oauth.scopes) == ("kb", mock_oauth_mcp.mcp_url, ["a"])
    assert target.headers == {"X-Tenant": "t1"}, "header values are unwrapped and a configured Authorization is never sent"
    assert "t1" not in repr(target) and "configured" not in repr(target)
    assert OAuthTarget.from_toolset(make_toolset(mock_oauth_mcp.mcp_url, oauth=None, headers={"A": "b"})) is None
    assert OAuthTarget.from_toolset(Toolset(id="kb", provider="internal")) is None
    assert OAuthTarget.from_toolset(Toolset(id="kb", provider="mcp", config={"transport": "stdio", "config": {"command": ["x"]}})) is None


def test_the_fingerprint_covers_the_sign_in_settings_and_not_the_secret(mock_oauth_mcp):
    base = {"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "one"}, "scopes": ["b", "a"]}
    fingerprint = config_fingerprint(target_of(make_toolset(mock_oauth_mcp.mcp_url, oauth=base)))
    assert len(fingerprint) == 64
    same = {**base, "static_client": {"client_id": "cc-client", "client_secret": "two"}, "scopes": ["a", "b"]}
    assert config_fingerprint(target_of(make_toolset(mock_oauth_mcp.mcp_url, oauth=same))) == fingerprint, "the secret and the scope order do not count"
    for changed in ({**base, "scopes": ["a"]}, {**base, "static_client": {"client_id": "other", "client_secret": "one"}},
                    {**base, "token_endpoint": "https://as.example/token"}, {**base, "resource_uri": "https://r.example"},
                    {**base, "client_name": "primer-2"}, {"static_client": base["static_client"]}):
        assert config_fingerprint(target_of(make_toolset(mock_oauth_mcp.mcp_url, oauth=changed))) != fingerprint, changed
    assert config_fingerprint(target_of(make_toolset(mock_oauth_mcp.mcp_url + "/v2", oauth=base))) != fingerprint


# ---- starting a browser sign-in ------------------------------------------------------------------------------------------------------------------------------------------


async def test_start_returns_an_authorize_url_and_stores_a_pending_sign_in(svc, target, store, mock_oauth_mcp):
    start = await svc.start_sign_in(target, redirect_uri=REDIRECT, started_by="admin")
    q = parse_qs(urlsplit(start.authorize_url).query)
    assert start.authorize_url.startswith(mock_oauth_mcp.issuer + "/authorize")
    assert q["code_challenge_method"] == ["S256"] and q["resource"] == [mock_oauth_mcp.mcp_url] and q["redirect_uri"] == [REDIRECT]
    assert q["scope"] == ["mcp"], "no configured scopes: the scope the server's challenge named"
    assert q["client_id"][0].startswith("dcr-") and len(q["state"][0]) >= 43
    assert start.redirect_uri == REDIRECT and start.expires_at == NOW + timedelta(minutes=10) == NOW + PENDING_TTL
    assert (start.status.state, start.status.reason, start.status.mode, start.status.redirect_uri) == ("needs_sign_in", NOT_CONNECTED, "oauth_browser", REDIRECT)
    pending = await store.take_pending(q["state"][0], now=NOW)
    assert pending.expires_at == NOW + timedelta(minutes=10) and pending.created_at == NOW
    assert (pending.toolset_id, pending.started_by, pending.client_source, pending.resource) == ("kb", "admin", "registered", mock_oauth_mcp.mcp_url)
    assert (pending.issuer, pending.token_endpoint, pending.revocation_endpoint) == (mock_oauth_mcp.issuer, mock_oauth_mcp.issuer + "/token",
                                                                                     mock_oauth_mcp.issuer + "/revoke")
    assert pending.config_fingerprint == config_fingerprint(target) and pending.token_endpoint_auth_method == "none"
    assert _s256(store.unseal(pending.code_verifier_enc)) == q["code_challenge"][0]


async def test_start_reuses_the_registration(svc, target, store, mock_oauth_mcp):
    first = await svc.start_sign_in(target, redirect_uri=REDIRECT, started_by="admin")
    second = await svc.start_sign_in(target, redirect_uri=REDIRECT, started_by="admin")
    assert len(mock_oauth_mcp.state.registrations) == 1
    client_ids = {parse_qs(urlsplit(start.authorize_url).query)["client_id"][0] for start in (first, second)}
    assert client_ids == {(await store.get_client("kb")).client_id}


async def test_start_reregisters_when_the_redirect_uri_changes(svc, target, store, mock_oauth_mcp):
    await svc.start_sign_in(target, redirect_uri=REDIRECT, started_by="admin")
    await svc.start_sign_in(target, redirect_uri=OTHER_REDIRECT, started_by="admin")
    assert [r["redirect_uris"] for r in mock_oauth_mcp.state.registrations] == [[REDIRECT], [OTHER_REDIRECT]]
    assert (await store.get_client("kb")).redirect_uri == OTHER_REDIRECT


async def test_start_reregisters_when_the_stored_registration_cannot_be_read(svc, target, store, mock_oauth_mcp, fake_storage_provider):
    """A registration sealed with another key (a client secret the server issued) is registered again instead of failing the sign-in."""
    from primer.model.mcp_oauth import McpOAuthClient

    other = McpOAuthStore(fake_storage_provider, SecretBox.from_key_material(OTHER_KEY))
    await other.put_client(McpOAuthClient(id="kb", issuer=mock_oauth_mcp.issuer, redirect_uri=REDIRECT, client_id="old-client",
                                          client_secret_enc=other.seal("old-secret"), registered_at=NOW))
    start = await svc.start_sign_in(target, redirect_uri=REDIRECT, started_by="admin")
    assert len(mock_oauth_mcp.state.registrations) == 1 and parse_qs(urlsplit(start.authorize_url).query)["client_id"] != ["old-client"]


async def test_start_with_a_static_client_does_not_register(svc, store, mock_oauth_mcp):
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={"static_client": {"client_id": "pre-client", "client_secret": "pre-secret"}})
    start = await svc.start_sign_in(target_of(toolset), redirect_uri=REDIRECT, started_by="admin")
    assert mock_oauth_mcp.state.registrations == [] and parse_qs(urlsplit(start.authorize_url).query)["client_id"] == ["pre-client"]
    back = authorize(start.authorize_url)
    status = await svc.complete_sign_in(toolset, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert status.state == "connected"
    exchange = mock_oauth_mcp.state.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code" and "client_secret" not in exchange, "the confidential client authenticated with Basic"
    row = await store.get_tokens("kb")
    assert (row.client_id, row.client_source, row.token_endpoint_auth_method) == ("pre-client", "static", "client_secret_basic")


async def test_start_uses_the_configured_scopes_and_resource(svc, mock_oauth_mcp):
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={"scopes": ["mcp", "extra"], "resource_uri": "https://r.example/x"})
    start = await svc.start_sign_in(target_of(toolset), redirect_uri=REDIRECT, started_by="admin")
    q = parse_qs(urlsplit(start.authorize_url).query)
    assert q["scope"] == ["mcp extra"] and q["resource"] == ["https://r.example/x"]


async def test_start_purges_expired_pending_sign_ins(svc, target, store, fake_storage_provider):
    for state, expires in (("old", NOW - timedelta(minutes=1)), ("live", NOW + timedelta(minutes=5))):
        await store.put_pending(McpOAuthPendingSignIn(
            id=state, toolset_id="kb", code_verifier_enc=store.seal("v"), redirect_uri=REDIRECT, issuer="https://as.example", token_endpoint="https://as.example/t",
            client_id="c", client_source="registered", config_fingerprint="f" * 64, created_at=NOW - timedelta(minutes=9), expires_at=expires,
        ))
    await svc.start_sign_in(target, redirect_uri=REDIRECT, started_by="admin")
    pending = fake_storage_provider.get_storage(McpOAuthPendingSignIn)
    assert await pending.get(pending_key("old")) is None and await pending.get(pending_key("live")) is not None


async def test_start_refuses_an_authorization_server_without_s256(svc, store):
    toolset = make_toolset("https://m.example/mcp", oauth={})
    as_doc = {"issuer": "https://m.example", "authorization_endpoint": "https://m.example/authorize", "token_endpoint": "https://m.example/token",
              "registration_endpoint": "https://m.example/register", "code_challenge_methods_supported": ["plain"]}
    with respx.mock(assert_all_called=False) as router:
        router.post("https://m.example/mcp").mock(return_value=httpx.Response(401, headers={"WWW-Authenticate": "Bearer"}))
        router.get(url__regex=r"https://m\.example/\.well-known/oauth-protected-resource.*").mock(return_value=httpx.Response(404))
        router.get("https://m.example/.well-known/oauth-authorization-server").mock(return_value=httpx.Response(200, json=as_doc))
        register = router.post("https://m.example/register").mock(return_value=httpx.Response(201, json={"client_id": "x"}))
        with pytest.raises(BadRequestError, match="does not advertise PKCE S256"):
            await svc.start_sign_in(target_of(toolset), redirect_uri=REDIRECT, started_by="admin")
    assert not register.called
    assert await store.purge_pending("kb", now=NOW + timedelta(days=1)) == 0, "no pending sign-in was stored"


async def test_start_for_a_client_credentials_toolset_is_refused(svc, mock_oauth_mcp):
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "cc-secret"}})
    with pytest.raises(BadRequestError, match="client credentials"):
        await svc.start_sign_in(target_of(toolset), redirect_uri=REDIRECT, started_by="admin")


# ---- finishing it (Review Focus 3) ---------------------------------------------------------------------------------------------------------------------------------------


async def test_complete_stores_encrypted_tokens_and_reports_connected(svc, kb, store, mock_oauth_mcp):
    status = await connect(svc, kb)
    assert (status.state, status.mode, status.reason) == ("connected", "oauth_browser", None)
    assert (status.connected_at, status.expires_at, status.scope, status.refreshed_at) == (NOW, NOW + timedelta(hours=1), "mcp", None)
    row = await store.get_tokens("kb")
    access, refresh = store.unseal(row.access_token_enc), store.unseal(row.refresh_token_enc)
    assert access.startswith("mock-at-") and refresh.startswith("mock-rt-")
    document = json.dumps(dump_for_storage(row))
    assert access not in document and refresh not in document
    assert (row.grant, row.connected_by, row.client_source, row.resource, row.version, row.state) == (
        "authorization_code", "admin", "registered", mock_oauth_mcp.mcp_url, 1, "connected")
    assert row.config_fingerprint == config_fingerprint(target_of(kb)) and row.revocation_endpoint == mock_oauth_mcp.issuer + "/revoke"
    assert mcp_answers(mock_oauth_mcp.mcp_url, access) == 200


async def test_complete_with_a_used_state_is_invalid_state(svc, kb, store, mock_oauth_mcp):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    exchanges = len(mock_oauth_mcp.state.token_requests)
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert (caught.value.code, caught.value.toolset_id) == ("invalid_state", "kb")
    assert len(mock_oauth_mcp.state.token_requests) == exchanges and (await store.get_tokens("kb")).version == 1


async def test_complete_after_ten_minutes_is_invalid_state(svc, kb, store, clock):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    clock.advance(seconds=601)
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert caught.value.code == "invalid_state" and await store.get_tokens("kb") is None


async def test_complete_with_an_unknown_or_empty_state_is_invalid_state(svc, kb, store):
    for state in ("never-issued", ""):
        with pytest.raises(SignInError) as caught:
            await svc.complete_sign_in(kb, toolset_id="kb", state=state, code="c", error=None)
        assert caught.value.code == "invalid_state"
    assert await store.get_tokens("kb") is None


async def test_complete_for_another_toolset_is_invalid_state(svc, kb, store, mock_oauth_mcp):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    other = make_toolset(mock_oauth_mcp.mcp_url, oauth={}, toolset_id="other")
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(other, toolset_id="other", state=back["state"], code=back["code"], error=None)
    assert (caught.value.code, caught.value.toolset_id) == ("invalid_state", "other")
    with pytest.raises(SignInError) as again:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert again.value.code == "invalid_state", "the state was spent by the misdirected callback"
    assert await store.get_tokens("kb") is None and await store.get_tokens("other") is None


async def test_a_declined_sign_in_is_access_denied(svc, kb, store, mock_oauth_mcp):
    mock_oauth_mcp.state.deny_next_authorize = True
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    assert back["error"] == "access_denied" and "code" not in back
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=None, error="access_denied")
    assert caught.value.code == "access_denied" and await store.get_tokens("kb") is None
    with pytest.raises(SignInError) as replay:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=None, error="access_denied")
    assert replay.value.code == "invalid_state"


async def test_another_provider_error_is_exchange_failed(svc, kb, store):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    state = parse_qs(urlsplit(start.authorize_url).query)["state"][0]
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=state, code=None, error="server_error\nInjected: line")
    assert caught.value.code == "exchange_failed" and await store.get_tokens("kb") is None


async def test_a_callback_without_a_code_is_exchange_failed(svc, kb, store):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    state = parse_qs(urlsplit(start.authorize_url).query)["state"][0]
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=state, code=None, error=None)
    assert caught.value.code == "exchange_failed" and await store.get_tokens("kb") is None


@pytest.mark.parametrize("edit", ["scopes", "url", "oauth_removed", "toolset_deleted"])
async def test_settings_changed_during_sign_in(svc, kb, store, mock_oauth_mcp, edit):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    edited = {
        "scopes": make_toolset(mock_oauth_mcp.mcp_url, oauth={"scopes": ["other"]}),
        "url": make_toolset(mock_oauth_mcp.base_url + "/elsewhere", oauth={}),
        "oauth_removed": make_toolset(mock_oauth_mcp.mcp_url, oauth=None, headers={"Authorization": "Bearer x"}),
        "toolset_deleted": None,
    }[edit]
    exchanges = len(mock_oauth_mcp.state.token_requests)
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(edited, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert caught.value.code == "settings_changed" and await store.get_tokens("kb") is None
    assert len(mock_oauth_mcp.state.token_requests) == exchanges, "the code was never exchanged for the edited toolset"


async def test_a_refused_code_exchange_is_exchange_failed(svc, kb, store, mock_oauth_mcp):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    mock_oauth_mcp.state.token_status_override = 400
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert caught.value.code == "exchange_failed" and await store.get_tokens("kb") is None


async def test_an_unreachable_token_endpoint_is_exchange_failed(svc, kb, store, mock_oauth_mcp):
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    mock_oauth_mcp.state.token_status_override = 503
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert caught.value.code == "exchange_failed" and await store.get_tokens("kb") is None


async def test_reconnect_replaces_the_tokens_and_bumps_the_version(svc, kb, store, clock):
    await connect(svc, kb)
    first = await store.get_tokens("kb")
    clock.advance(minutes=5)
    status = await connect(svc, kb, started_by="admin-2")
    second = await store.get_tokens("kb")
    assert second.version == first.version + 1 and status.connected_at == NOW + timedelta(minutes=5) and second.connected_by == "admin-2"
    assert store.unseal(second.access_token_enc) != store.unseal(first.access_token_enc)
    assert store.unseal(second.refresh_token_enc) != store.unseal(first.refresh_token_enc)


# ---- client credentials --------------------------------------------------------------------------------------------------------------------------------------------------


def _cc_toolset(server, **over):
    return make_toolset(server.mcp_url, oauth={"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "cc-secret"}, **over})


async def test_client_credentials_connect(svc, store, mock_oauth_mcp):
    status = await svc.connect_client_credentials(target_of(_cc_toolset(mock_oauth_mcp)), connected_by="admin")
    assert (status.state, status.mode, status.expires_at, status.redirect_uri) == ("connected", "oauth_client_credentials", NOW + timedelta(hours=1), None)
    row = await store.get_tokens("kb")
    assert (row.grant, row.refresh_token_enc, row.client_source, row.client_id, row.connected_by) == ("client_credentials", None, "static", "cc-client", "admin")
    assert row.token_endpoint == mock_oauth_mcp.issuer + "/token" and row.issuer == mock_oauth_mcp.issuer
    request = mock_oauth_mcp.state.token_requests[-1]
    assert request["grant_type"] == "client_credentials" and request["resource"] == mock_oauth_mcp.mcp_url and request["scope"] == "mcp"
    assert mcp_answers(mock_oauth_mcp.mcp_url, store.unseal(row.access_token_enc)) == 200


async def test_client_credentials_with_a_named_token_endpoint_sends_only_a_configured_resource(svc, store, mock_oauth_mcp):
    token_endpoint = mock_oauth_mcp.issuer + "/token"
    await svc.connect_client_credentials(target_of(_cc_toolset(mock_oauth_mcp, token_endpoint=token_endpoint)), connected_by="admin")
    assert "resource" not in mock_oauth_mcp.state.token_requests[-1], "no discovery, so no resource indicator unless one is configured"
    row = await store.get_tokens("kb")
    assert (row.token_endpoint, row.issuer, row.revocation_endpoint) == (token_endpoint, None, None)
    await svc.connect_client_credentials(target_of(_cc_toolset(mock_oauth_mcp, token_endpoint=token_endpoint, resource_uri=mock_oauth_mcp.mcp_url)),
                                         connected_by="admin")
    assert mock_oauth_mcp.state.token_requests[-1]["resource"] == mock_oauth_mcp.mcp_url


async def test_client_credentials_with_a_wrong_secret_is_refused(svc, store, mock_oauth_mcp):
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "wrong"}})
    with pytest.raises(BadRequestError, match=r"^the token endpoint refused the client credentials \(invalid_client\)$"):
        await svc.connect_client_credentials(target_of(toolset), connected_by="admin")
    assert await store.get_tokens("kb") is None


async def test_client_credentials_connect_needs_a_client_credentials_toolset(svc, target):
    with pytest.raises(BadRequestError, match="client credentials"):
        await svc.connect_client_credentials(target, connected_by="admin")


# ---- disconnect ----------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_disconnect_revokes_and_deletes(svc, kb, store, mock_oauth_mcp):
    await connect(svc, kb)
    row = await store.get_tokens("kb")
    refresh, access = store.unseal(row.refresh_token_enc), store.unseal(row.access_token_enc)
    status = await svc.disconnect(kb)
    assert mock_oauth_mcp.state.revocations == [refresh]
    assert await store.get_tokens("kb") is None and (status.state, status.reason, status.connected_at) == ("needs_sign_in", NOT_CONNECTED, None)
    assert mcp_answers(mock_oauth_mcp.mcp_url, access) == 401, "revoking the refresh token ended its access token too"


async def test_disconnect_revokes_the_access_token_when_there_is_no_refresh_token(svc, store, mock_oauth_mcp):
    toolset = _cc_toolset(mock_oauth_mcp)
    await svc.connect_client_credentials(target_of(toolset), connected_by="admin")
    access = store.unseal((await store.get_tokens("kb")).access_token_enc)
    await svc.disconnect(toolset)
    assert mock_oauth_mcp.state.revocations == [access] and await store.get_tokens("kb") is None


async def test_disconnect_without_a_revocation_endpoint_still_deletes(svc, kb, store, mock_oauth_mcp):
    from primer.storage import raw_generation

    await connect(svc, kb)
    row = await store.get_tokens("kb")
    await store.patch_tokens("kb", {"revocation_endpoint": None}, where={"version": [raw_generation(row, "version")]})
    await svc.disconnect(kb)
    assert mock_oauth_mcp.state.revocations == [] and await store.get_tokens("kb") is None


async def test_disconnect_deletes_whatever_the_revocation_answers(svc, kb, store, mock_oauth_mcp):
    from primer.storage import raw_generation

    await connect(svc, kb)
    row = await store.get_tokens("kb")
    await store.patch_tokens("kb", {"revocation_endpoint": "http://127.0.0.1:9/revoke"}, where={"version": [raw_generation(row, "version")]})
    await svc.disconnect(kb)
    assert await store.get_tokens("kb") is None


async def test_disconnect_of_a_toolset_that_is_not_connected_is_a_no_op(svc, kb, mock_oauth_mcp):
    status = await svc.disconnect(kb)
    assert status.state == "needs_sign_in" and mock_oauth_mcp.state.revocations == []


async def test_a_sign_in_started_before_a_disconnect_cannot_reconnect_after_it(svc, kb, store, mock_oauth_mcp, fake_storage_provider):
    """Audit G4-06: the admin clicks Reconnect (a pending sign-in), abandons the provider's page, then clicks Disconnect. The abandoned consent,
    completed within its ten minutes (by anyone holding the authorize URL), must not connect the toolset again: Disconnect ends the pending
    sign-ins too."""
    assert (await connect(svc, kb)).state == "connected"
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    assert (await svc.disconnect(kb)).state == "needs_sign_in"
    pending = fake_storage_provider.get_storage(McpOAuthPendingSignIn)
    assert await pending.get(pending_key(back["state"])) is None, "the pending sign-in outlived the disconnect"
    exchanges = len(mock_oauth_mcp.state.token_requests)
    with pytest.raises(SignInError) as caught:
        await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    assert caught.value.code == "invalid_state"
    assert await store.get_tokens("kb") is None and len(mock_oauth_mcp.state.token_requests) == exchanges


async def test_disconnect_keeps_the_pending_sign_ins_of_other_toolsets(svc, kb, store, mock_oauth_mcp):
    other = make_toolset(mock_oauth_mcp.mcp_url, oauth={}, toolset_id="other")
    await svc.start_sign_in(target_of(other), redirect_uri=REDIRECT.replace("/kb/", "/other/"), started_by="admin")
    await svc.disconnect(kb)
    assert await store.purge_pending("other", now=NOW + timedelta(days=1)) == 1


async def test_forget_removes_everything_of_one_toolset(svc, kb, store):
    await connect(svc, kb)
    await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    await svc.forget("kb")
    assert await store.get_tokens("kb") is None and await store.get_client("kb") is None
    assert await store.purge_pending("kb", now=NOW + timedelta(days=1)) == 0


# ---- status --------------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["none", "header", "no_row", "fingerprint", "key", "stored_needs_sign_in", "stored_error", "connected", "expired_no_refresh"])
async def test_status_rules(case, svc, store, fake_storage_provider):
    url = "https://m.example/mcp"
    if case in ("none", "header"):
        toolset = make_toolset(url, oauth=None, headers={"Authorization": "Bearer x"} if case == "header" else None)
        status = await svc.status(toolset)
        assert (status.mode, status.state, status.reason) == (case, "not_applicable", None)
        return
    toolset = make_toolset(url, oauth={})
    target = target_of(toolset)
    rows = {
        "fingerprint": lambda: token_row(store, target, config_fingerprint="0" * 64),
        "key": lambda: token_row(other, target),
        "stored_needs_sign_in": lambda: token_row(store, target, access_token_enc=None, refresh_token_enc=None, state="needs_sign_in",
                                                  reason="The authorization server refused the refresh token (invalid_grant)."),
        "stored_error": lambda: token_row(store, target, state="error", reason="The authorization server could not be reached: x"),
        "connected": lambda: token_row(store, target),
        "expired_no_refresh": lambda: token_row(store, target, refresh_token_enc=None, expires_at=NOW),
    }
    other = McpOAuthStore(fake_storage_provider, SecretBox.from_key_material(OTHER_KEY))
    if case != "no_row":
        await (other if case == "key" else store).replace_tokens(rows[case]())
    status = await svc.status(toolset, redirect_uri=REDIRECT)
    expected = {
        "no_row": ("needs_sign_in", NOT_CONNECTED),
        "fingerprint": ("needs_sign_in", SETTINGS_CHANGED),
        "key": ("needs_sign_in", KEY_CHANGED),
        "stored_needs_sign_in": ("needs_sign_in", "The authorization server refused the refresh token (invalid_grant)."),
        "stored_error": ("error", "The authorization server could not be reached: x"),
        "connected": ("connected", None),
        "expired_no_refresh": ("needs_sign_in", EXPIRED_NO_REFRESH),
    }[case]
    assert (status.state, status.reason) == expected and status.mode == "oauth_browser" and status.redirect_uri == REDIRECT
    if case == "no_row":
        assert status.connected_at is None
    else:
        assert status.connected_at == NOW - timedelta(hours=2), "a stored sign-in says when it was connected, so the console offers Reconnect"
    if case == "connected":
        assert (status.expires_at, status.refreshed_at, status.scope) == (NOW + timedelta(hours=1), NOW - timedelta(minutes=5), "mcp")


async def test_the_status_never_carries_a_token(svc, kb, store):
    await connect(svc, kb)
    row = await store.get_tokens("kb")
    served = (await svc.status(kb)).model_dump_json()
    assert store.unseal(row.access_token_enc) not in served and store.unseal(row.refresh_token_enc) not in served


def test_redirect_uri(fake_storage_provider, box):
    public = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=box, public_url="https://primer.example/")
    plain = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=box)
    assert public.redirect_uri("linear", request_base_url=None) == "https://primer.example/v1/toolsets/linear/oauth/callback"
    assert public.redirect_uri("linear", request_base_url="http://10.0.0.5:8000/") == "https://primer.example/v1/toolsets/linear/oauth/callback"
    assert plain.redirect_uri("a b", request_base_url="http://127.0.0.1:8765/") == "http://127.0.0.1:8765/v1/toolsets/a%20b/oauth/callback"
    assert plain.redirect_uri("a/b", request_base_url="http://127.0.0.1:8765") == "http://127.0.0.1:8765/v1/toolsets/a%2Fb/oauth/callback"
    with pytest.raises(ConfigError, match="PRIMER_PUBLIC_URL"):
        plain.redirect_uri("kb", request_base_url=None)


# ---- nothing secret reaches the log --------------------------------------------------------------------------------------------------------------------------------------


async def test_no_token_code_or_verifier_reaches_the_log(caplog, svc, kb, store, mock_oauth_mcp, fake_storage_provider):
    caplog.set_level(logging.DEBUG)
    start = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    back = authorize(start.authorize_url)
    pending = await fake_storage_provider.get_storage(McpOAuthPendingSignIn).get(pending_key(back["state"]))
    verifier = store.unseal(pending.code_verifier_enc)
    await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None)
    row = await store.get_tokens("kb")
    secrets_seen = [back["state"], back["code"], verifier, store.unseal(row.access_token_enc), store.unseal(row.refresh_token_enc)]

    refused = await svc.start_sign_in(target_of(kb), redirect_uri=REDIRECT, started_by="admin")
    refused_back = authorize(refused.authorize_url)
    mock_oauth_mcp.state.token_status_override = 400
    with pytest.raises(SignInError):
        await svc.complete_sign_in(kb, toolset_id="kb", state=refused_back["state"], code=refused_back["code"], error=None)
    mock_oauth_mcp.state.token_status_override = None
    secrets_seen += [refused_back["state"], refused_back["code"]]

    cc = make_toolset(mock_oauth_mcp.mcp_url, oauth={"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "cc-secret"}},
                      toolset_id="cc")
    await svc.connect_client_credentials(target_of(cc), connected_by="admin")
    secrets_seen += ["cc-secret", store.unseal((await store.get_tokens("cc")).access_token_enc)]
    await svc.disconnect(kb)

    assert any(record.name.startswith("primer.toolset.oauth") for record in caplog.records), "the service logged nothing, so the check proves nothing"
    assert any(record.name.startswith("httpcore") for record in caplog.records), "the traces of the service's own HTTP calls are in the checked log"
    for secret in secrets_seen:
        assert secret not in caplog.text
