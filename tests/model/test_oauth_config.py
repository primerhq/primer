"""The OAuth block of an MCP toolset: the sign-in grant and the client-credentials token endpoint (plan 07, Task 3; design 12.2).

The block DESCRIBES how a server signs in; it signs nothing in by itself. These tests pin what the description may say: a browser sign-in is the default, a
client-credentials sign-in needs a client id and secret, only that grant takes a token endpoint, and a row written before the revamp still loads.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import HttpConfig, OAuthConfig


def test_defaults():
    c = OAuthConfig()
    assert (c.grant, c.scopes, c.token_endpoint, c.static_client, c.resource_uri, c.client_name) == ("authorization_code", [], None, None, None, "primer")
    assert c.redirect_uri is None


def test_the_redirect_uri_is_optional_and_kept_when_given():
    assert str(OAuthConfig(redirect_uri="https://p.example/cb").redirect_uri) == "https://p.example/cb"


def test_the_grant_is_one_of_the_two_sign_ins():
    with pytest.raises(ValidationError):
        OAuthConfig(grant="password")


def test_client_credentials_needs_a_client_id_and_secret():
    with pytest.raises(ValidationError, match="static_client"):
        OAuthConfig(grant="client_credentials")
    with pytest.raises(ValidationError, match="client_secret"):
        OAuthConfig(grant="client_credentials", static_client={"client_id": "c"})
    with pytest.raises(ValidationError, match="client_secret"):
        OAuthConfig(grant="client_credentials", static_client={"client_id": "c", "client_secret": ""})
    ok = OAuthConfig(grant="client_credentials", static_client={"client_id": "c", "client_secret": "s"})
    assert ok.grant == "client_credentials" and ok.static_client.client_secret.get_secret_value() == "s"


def test_a_browser_sign_in_needs_no_client():
    assert OAuthConfig(grant="authorization_code").static_client is None
    assert OAuthConfig(static_client={"client_id": "pre-registered"}).static_client.client_secret is None


def test_token_endpoint_only_for_client_credentials():
    with pytest.raises(ValidationError, match="token_endpoint applies to client credentials only"):
        OAuthConfig(token_endpoint="https://as.example/token")
    with pytest.raises(ValidationError, match="token_endpoint applies to client credentials only"):
        OAuthConfig(grant="authorization_code", token_endpoint="https://as.example/token")
    ok = OAuthConfig(grant="client_credentials", static_client={"client_id": "c", "client_secret": "s"}, token_endpoint="https://as.example/token")
    assert ok.token_endpoint == "https://as.example/token"


@pytest.mark.parametrize("bad", ["ftp://x", "as.example/token", "/token", "https://", "https:///token", "http://as.example/to ken", "", "https://user:pw@as.example/token", "javascript:alert(1)"])
def test_token_endpoint_must_be_an_http_url(bad):
    with pytest.raises(ValidationError, match="token_endpoint"):
        OAuthConfig(grant="client_credentials", static_client={"client_id": "c", "client_secret": "s"}, token_endpoint=bad)


@pytest.mark.parametrize("good", ["https://as.example/token", "http://127.0.0.1:9000/oauth/token", "HTTPS://AS.Example/token?tenant=1"])
def test_token_endpoint_accepts_http_and_https_urls(good):
    assert OAuthConfig(grant="client_credentials", static_client={"client_id": "c", "client_secret": "s"}, token_endpoint=good).token_endpoint == good


def test_the_served_block_carries_the_grant_and_no_secret():
    c = OAuthConfig(grant="client_credentials", static_client={"client_id": "c", "client_secret": "shh"}, token_endpoint="https://as.example/token")
    dumped = c.model_dump(mode="json")
    assert dumped["grant"] == "client_credentials" and dumped["token_endpoint"] == "https://as.example/token"
    assert dumped["static_client"]["client_secret"] == "**********"


def test_an_old_row_still_loads():
    h = HttpConfig.model_validate({"url": "https://m.example/mcp", "oauth": {"redirect_uri": "https://p.example/cb", "spec_version": "2025-06-18", "scopes": ["a"]}})
    assert h.oauth.grant == "authorization_code" and h.oauth.scopes == ["a"]


def test_a_row_with_the_old_required_redirect_uri_round_trips_unchanged():
    row = {"url": "https://m.example/mcp", "oauth": {"redirect_uri": "https://p.example/cb", "scopes": [], "client_name": "primer-x"}}
    h = HttpConfig.model_validate(row)
    again = HttpConfig.model_validate(h.model_dump(mode="json"))
    assert str(again.oauth.redirect_uri) == "https://p.example/cb" and again.oauth.client_name == "primer-x"
