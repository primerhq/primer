"""The real-account fixture must verify an existing operator before spending quota."""

import httpx
import pytest

from primer.auth.api_tokens import mint_plaintext
from tests.e2e.test_codex_broker_real_workspace import _authenticate_operator, _operator_origin


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://[::1]:8765", "https://primer.example"])
def test_accepts_direct_operator_origin(origin):
    assert _operator_origin(origin) == httpx.URL(origin)


@pytest.mark.parametrize("origin", [
    "http://primer.example", "http://localhost", "https://user:password@primer.example",
    "https://primer.example/api", "https://primer.example/?token=example", "https://primer.example/#example",
])
def test_refuses_unsafe_operator_origin(origin):
    with pytest.raises(pytest.fail.Exception):
        _operator_origin(origin)


_SIGNED_OUT = {"has_user": True, "authenticated": False, "principal": None, "sign_in_method": None}
_ADMIN = {"authenticated": True, "sign_in_method": "api_token", "role": "admin",
          "principal": {"type": "api_token", "id": "token-row-id"}}
_DISABLED = {**_ADMIN, "has_user": True, "sign_in_method": "auth_disabled", "principal": {"type": "system"}}


@pytest.mark.parametrize(("before", "after", "valid_token_format", "succeeds"), [
    pytest.param({**_SIGNED_OUT, "has_user": False}, None, True, False, id="fresh-server"),
    pytest.param(_DISABLED, None, True, False, id="auth-disabled"),
    pytest.param(_SIGNED_OUT, None, False, False, id="wrong-credential-type"),
    pytest.param(_SIGNED_OUT, _SIGNED_OUT, True, False, id="invalid-token"),
    pytest.param(_SIGNED_OUT, _DISABLED, True, False, id="auth-disabled-between-requests"),
    pytest.param(_SIGNED_OUT, {**_ADMIN, "role": "user"}, True, False, id="non-admin"),
    pytest.param(_SIGNED_OUT, {**_ADMIN, "sign_in_method": "password", "principal": {"type": "user"}},
                 True, False, id="cookie-principal"),
    pytest.param(_SIGNED_OUT, _ADMIN, True, True, id="existing-admin-token"),
])
async def test_operator_uses_only_status_and_checks_bearer_identity(monkeypatch, before, after, valid_token_format, succeeds):
    token = mint_plaintext() if valid_token_format else "other-credential"
    monkeypatch.setenv("PRIMER_E2E_CODEX_OPERATOR_TOKEN", token)
    authenticated = []

    def status(request):
        assert (request.method, request.url.path) == ("GET", "/v1/auth/status")
        bearer = request.headers.get("Authorization")
        if bearer is not None:
            assert bearer == f"Bearer {token}"
        authenticated.append(bearer is not None)
        return httpx.Response(200, json=before if len(authenticated) == 1 else after)

    async with httpx.AsyncClient(transport=httpx.MockTransport(status), base_url="https://primer.example") as client:
        if succeeds:
            assert await _authenticate_operator(client) == "token-row-id"
        else:
            with pytest.raises(pytest.fail.Exception):
                await _authenticate_operator(client)
    assert authenticated == ([False] if after is None else [False, True])
