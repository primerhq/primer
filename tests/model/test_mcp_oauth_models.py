"""The stored shapes of the shared MCP sign-in, and the error that says it is gone (plan 07, Task 5; design 12.5 and 12.6).

Three rows hold a toolset's sign-in: the token set (one per toolset), the registered client (one per toolset) and the pending sign-ins (one per started
browser sign-in). Every secret in them is ciphertext (``*_enc``, sealed by ``SecretBox``) held in a ``SecretStr``, so a dump for the API, a log line or a
``repr`` shows a mask even if a route ever served one. None of the three is a CDC kind (no event is written for a token), and no route serves them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from primer.events.registry import kind_for_model
from primer.model.common import dump_for_storage
from primer.model.except_ import PrimerError
from primer.model.mcp_oauth import (
    McpOAuthClient,
    McpOAuthPendingSignIn,
    McpOAuthTokenSet,
    McpSignInRequiredError,
)

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


def _token_set(**over) -> McpOAuthTokenSet:
    base = {
        "id": "kb",
        "grant": "authorization_code",
        "access_token_enc": SecretStr("v1.cipher-access"),
        "refresh_token_enc": SecretStr("v1.cipher-refresh"),
        "token_endpoint": "https://as.example/token",
        "client_id": "client-1",
        "client_source": "registered",
        "config_fingerprint": "f" * 64,
        "connected_at": NOW,
    }
    base.update(over)
    return McpOAuthTokenSet(**base)


def _client(**over) -> McpOAuthClient:
    base = {"id": "kb", "issuer": "https://as.example", "redirect_uri": "https://p.example/cb", "client_id": "client-1", "registered_at": NOW}
    base.update(over)
    return McpOAuthClient(**base)


def _pending(**over) -> McpOAuthPendingSignIn:
    base = {
        "id": "state-value",
        "toolset_id": "kb",
        "code_verifier_enc": SecretStr("v1.cipher-verifier"),
        "redirect_uri": "https://p.example/cb",
        "issuer": "https://as.example",
        "token_endpoint": "https://as.example/token",
        "client_id": "client-1",
        "client_source": "registered",
        "config_fingerprint": "f" * 64,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    base.update(over)
    return McpOAuthPendingSignIn(**base)


def test_the_token_set_never_dumps_its_ciphertext():
    row = _token_set()
    dumped = row.model_dump(mode="json")
    assert dumped["access_token_enc"] == "**********"
    assert dumped["refresh_token_enc"] == "**********"
    for surface in (json.dumps(dumped), row.model_dump_json(), repr(row), str(row)):
        assert "cipher" not in surface


def test_the_other_rows_never_dump_their_ciphertext_either():
    client = _client(client_secret_enc=SecretStr("v1.cipher-secret"))
    pending = _pending()
    assert client.model_dump(mode="json")["client_secret_enc"] == "**********"
    assert pending.model_dump(mode="json")["code_verifier_enc"] == "**********"
    for surface in (client.model_dump_json(), repr(client), pending.model_dump_json(), repr(pending)):
        assert "cipher" not in surface


def test_the_storage_dump_holds_the_ciphertext_so_a_row_survives_its_round_trip():
    row = _token_set(expires_at=NOW + timedelta(hours=1), refresh_lease_until=NOW + timedelta(seconds=30), refresh_lease_owner="pod-a", version=4)
    stored = dump_for_storage(row)
    assert stored["access_token_enc"] == "v1.cipher-access" and stored["refresh_token_enc"] == "v1.cipher-refresh"
    assert McpOAuthTokenSet.model_validate(stored) == row
    for model, row in ((McpOAuthClient, _client(client_secret_enc=SecretStr("v1.c"))), (McpOAuthPendingSignIn, _pending())):
        assert model.model_validate(dump_for_storage(row)) == row


def test_the_token_set_defaults():
    row = _token_set()
    assert (row.token_type, row.state, row.version, row.reason) == ("Bearer", "connected", 1, None)
    assert (row.expires_at, row.scope, row.issuer, row.revocation_endpoint, row.resource) == (None, None, None, None, None)
    assert (row.refresh_lease_until, row.refresh_lease_owner, row.connected_by, row.refreshed_at) == (None, None, None, None)


def test_a_signed_out_token_set_has_no_tokens():
    row = _token_set(access_token_enc=None, refresh_token_enc=None, state="needs_sign_in", reason="Not connected.")
    assert row.access_token_enc is None and row.refresh_token_enc is None
    assert dump_for_storage(row)["access_token_enc"] is None


def test_the_access_token_key_is_required_even_when_it_is_null():
    with pytest.raises(ValidationError):
        McpOAuthTokenSet(id="kb", grant="authorization_code", token_endpoint="https://as.example/token", client_id="c", client_source="static",
                         config_fingerprint="f", connected_at=NOW)


@pytest.mark.parametrize("field,bad", [("grant", "password"), ("state", "gone"), ("client_source", "dynamic")])
def test_the_closed_vocabularies_are_closed(field, bad):
    with pytest.raises(ValidationError):
        _token_set(**{field: bad})


@pytest.mark.parametrize("make", [_token_set, _client, _pending])
def test_a_row_needs_its_id(make):
    """The id is the toolset id (or the state value): nothing generates one, so a row without it is refused instead of stored under an invented name."""
    with pytest.raises(ValidationError):
        make(id=None)


def test_the_pending_defaults():
    row = _pending()
    assert (row.consumed, row.started_by, row.revocation_endpoint, row.resource, row.token_endpoint_auth_method) == (False, None, None, None, None)


def test_the_client_authentication_method_is_remembered_and_optional():
    """A refresh authenticates the client the way the sign-in did, without a new discovery; a row from before the field loads with None."""
    for make in (_token_set, _pending):
        assert make().token_endpoint_auth_method is None
        row = make(token_endpoint_auth_method="client_secret_post")
        assert type(row).model_validate(dump_for_storage(row)).token_endpoint_auth_method == "client_secret_post"
        with pytest.raises(ValidationError):
            make(token_endpoint_auth_method="private_key_jwt")


def test_the_client_defaults():
    assert _client().client_secret_enc is None


def test_none_of_the_rows_is_a_cdc_kind():
    for model in (McpOAuthTokenSet, McpOAuthClient, McpOAuthPendingSignIn):
        assert kind_for_model(model) is None, f"{model.__name__} would write an event row for every token change"


def test_sign_in_required_error_text():
    e = McpSignInRequiredError("linear", reason="Not connected.")
    assert str(e) == "the MCP server linear needs an operator to reconnect it"
    assert e.problem_extensions == {"toolset_id": "linear", "reason": "Not connected."}


def test_sign_in_required_error_without_a_reason():
    e = McpSignInRequiredError("linear")
    assert str(e) == "the MCP server linear needs an operator to reconnect it"
    assert e.problem_extensions == {"toolset_id": "linear"}
    assert (e.toolset_id, e.reason) == ("linear", None)


def test_sign_in_required_error_is_a_primer_error_with_no_code_or_status():
    """No code and no status, so ``str()`` is exactly the message a tool answers with, and the API maps the class itself to its own problem type."""
    e = McpSignInRequiredError("linear", reason="x")
    assert isinstance(e, PrimerError) and (e.code, e.status_code, e.message) == (None, None, str(e))
