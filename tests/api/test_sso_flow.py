"""HTTP-surface tests for the OIDC SSO login + callback flow.

Mirrors ``tests/auth/test_oidc.py``'s pattern: a locally generated RSA
keypair signs a fake id_token via ``jwt.encode``; all provider network
IO (discovery, JWKS, token exchange) is mocked via ``respx``. Each test
uses a FRESH set of provider URLs (``_IdpFixture`` mints a random
subdomain) because ``primer.auth.oidc`` TTL-caches discovery/JWKS
documents in module-level dicts keyed by URL, shared across the whole
test session.

Focus: the account-resolution security boundary in
``primer.api.routers.sso`` — strict ``(provider_id, sub)`` matching,
JIT provisioning gated on ``sso_jit_enabled``, disabled-account
rejection, and id_token validation failures (tampered / expired /
wrong-nonce) surfacing as 400s.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import respx
from jwt.algorithms import RSAAlgorithm

# Convention: shared API test fixtures (see test_oidc_providers_router.py).
from tests.api.conftest import raw_client as client, app  # noqa: F401

from primer.auth.tokens import sign_session, verify_session
from primer.model.oidc import OidcProvider, UserIdentity
from primer.model.storage import OffsetPage
from primer.model.user import User


CLIENT_ID = "sso-test-client"


@pytest.fixture(scope="module")
def rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def attacker_rsa_keypair():
    """A second, unrelated keypair -- stands in for a forged signature."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _jwk_dict(public_key, kid: str) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _sign(private_key, claims: dict, *, kid: str = "sso-test-key-1") -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _query(location: str) -> dict[str, str]:
    qs = parse_qs(urlsplit(location).query)
    return {k: v[0] for k, v in qs.items()}


class _IdpFixture:
    """Mocks one OIDC provider's discovery/JWKS/token endpoints via respx.

    ``queue_id_token`` / ``queue_raw_token_response`` push responses onto
    a FIFO consumed by successive POSTs to ``token_endpoint`` -- needed
    because a test may drive the login->callback flow more than once
    against the same provider (e.g. the "second callback, same sub"
    race-safety case) and each cycle mints its own nonce/id_token.
    """

    def __init__(self, pub_key, *, kid: str = "sso-test-key-1") -> None:
        host = f"idp-{uuid.uuid4().hex}.example.com"
        self.issuer = f"https://{host}/"
        self.discovery_url = f"https://{host}/.well-known/openid-configuration"
        self.authorization_endpoint = f"https://{host}/authorize"
        self.token_endpoint = f"https://{host}/token"
        self.jwks_uri = f"https://{host}/jwks.json"
        self.kid = kid
        self._pub_key = pub_key
        self._queue: list[httpx.Response] = []

    def register(self) -> None:
        respx.get(self.discovery_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": self.authorization_endpoint,
                    "token_endpoint": self.token_endpoint,
                    "jwks_uri": self.jwks_uri,
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        )
        respx.get(self.jwks_uri).mock(
            return_value=httpx.Response(
                200, json={"keys": [_jwk_dict(self._pub_key, self.kid)]},
            )
        )
        respx.post(self.token_endpoint).mock(side_effect=self._handle_token)

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        return self._queue.pop(0)

    def base_claims(self, *, sub: str, nonce: str, **overrides) -> dict:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
        }
        claims.update(overrides)
        return claims

    def queue_id_token(self, private_key, claims: dict, *, kid: str | None = None) -> None:
        token = _sign(private_key, claims, kid=kid if kid is not None else self.kid)
        self._queue.append(
            httpx.Response(
                200,
                json={"access_token": "at", "id_token": token, "token_type": "Bearer"},
            )
        )


async def _seed_provider(app, idp: _IdpFixture, **overrides) -> OidcProvider:
    provider = OidcProvider(
        name="Test IdP",
        discovery_url=idp.discovery_url,
        client_id=CLIENT_ID,
        client_secret=None,
        **overrides,
    )
    return await app.state.storage_provider.get_storage(OidcProvider).create(provider)


async def _login(client, provider_id: str, **params) -> httpx.Response:
    return await client.get(
        f"/v1/auth/sso/{provider_id}/login", params=params, follow_redirects=False,
    )


async def _callback(client, provider_id: str, *, code: str, state: str | None) -> httpx.Response:
    """``state=None`` OMITS the query param entirely (simulates a provider
    that dropped it), as opposed to passing an empty string."""
    params: dict[str, str] = {"code": code}
    if state is not None:
        params["state"] = state
    return await client.get(
        f"/v1/auth/sso/{provider_id}/callback",
        params=params,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# GET /providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_providers_returns_only_enabled(client, app):
    storage = app.state.storage_provider.get_storage(OidcProvider)
    enabled = await storage.create(
        OidcProvider(
            name="Enabled IdP",
            discovery_url="https://enabled.example.com/.well-known/openid-configuration",
            client_id="c1",
        )
    )
    await storage.create(
        OidcProvider(
            name="Disabled IdP",
            discovery_url="https://disabled.example.com/.well-known/openid-configuration",
            client_id="c2",
            enabled=False,
        )
    )

    r = await client.get("/v1/auth/sso/providers")
    assert r.status_code == 200, r.text
    items = r.json()
    ids = {item["id"] for item in items}
    assert enabled.id in ids
    assert all(item["id"] != "c2" for item in items)
    matched = next(item for item in items if item["id"] == enabled.id)
    assert matched == {"id": enabled.id, "name": "Enabled IdP"}


# ---------------------------------------------------------------------------
# GET /{provider_id}/login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_login_404_for_missing_provider(client, app):
    r = await _login(client, "no-such-provider")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "provider_not_found"


@pytest.mark.asyncio
@respx.mock
async def test_login_404_for_disabled_provider(client, app, rsa_keypair):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp, enabled=False)

    r = await _login(client, provider.id)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "provider_not_found"


@pytest.mark.asyncio
@respx.mock
async def test_login_redirects_with_pkce_state_nonce_and_sets_cookie(client, app, rsa_keypair):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)

    r = await _login(client, provider.id)
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith(idp.authorization_endpoint)

    qs = _query(location)
    assert qs["client_id"] == CLIENT_ID
    assert qs["response_type"] == "code"
    assert qs["redirect_uri"].endswith(f"/v1/auth/sso/{provider.id}/callback")
    assert qs["code_challenge_method"] == "S256"
    assert qs["code_challenge"]
    assert qs["state"]
    assert qs["nonce"]
    assert "openid" in qs["scope"]

    assert "primer_sso_state" in r.cookies
    set_cookie_header = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie_header


# ---------------------------------------------------------------------------
# GET /{provider_id}/callback -- happy path / JIT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_callback_jit_creates_user_sets_session_src(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)
    await app.state.storage_provider.set_sso_default_access("user")

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])

    idp.queue_id_token(
        priv,
        idp.base_claims(
            sub="sub-jit-1", nonce=qs["nonce"], email="alice@example.com", email_verified=True,
        ),
    )
    cb = await _callback(client, provider.id, code="auth-code-1", state=qs["state"])
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/console/"
    assert "primer_session" in cb.cookies
    assert "primer_sso_state" not in cb.cookies or cb.cookies.get("primer_sso_state") == ""

    payload = verify_session(
        token=cb.cookies["primer_session"],
        secret=app.state.session_secret,
        max_age_seconds=7 * 86400,
    )
    assert payload is not None
    assert payload.src == provider.id

    users_page = await app.state.storage_provider.get_storage(User).list(
        OffsetPage(offset=0, length=50)
    )
    matched = [u for u in users_page.items if u.email == "alice@example.com"]
    assert len(matched) == 1
    user = matched[0]
    assert user.id == payload.user_id
    assert user.password_hash is None
    assert user.role == "user"
    assert user.username.startswith("alice")

    identities_page = await app.state.storage_provider.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=50)
    )
    matched_identities = [
        i for i in identities_page.items
        if i.provider_id == provider.id and i.subject == "sub-jit-1"
    ]
    assert len(matched_identities) == 1
    assert matched_identities[0].user_id == user.id


@pytest.mark.asyncio
@respx.mock
async def test_second_callback_same_sub_reuses_same_user(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    # First login/callback -- JIT-creates the account.
    login1 = await _login(client, provider.id)
    qs1 = _query(login1.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-repeat", nonce=qs1["nonce"]))
    cb1 = await _callback(client, provider.id, code="code-1", state=qs1["state"])
    assert cb1.status_code == 302, cb1.text
    payload1 = verify_session(
        token=cb1.cookies["primer_session"],
        secret=app.state.session_secret,
        max_age_seconds=7 * 86400,
    )

    # Second login/callback for the SAME (provider_id, sub) -- must log
    # into the SAME user, not create a duplicate.
    login2 = await _login(client, provider.id)
    qs2 = _query(login2.headers["location"])
    assert qs2["nonce"] != qs1["nonce"]
    idp.queue_id_token(priv, idp.base_claims(sub="sub-repeat", nonce=qs2["nonce"]))
    cb2 = await _callback(client, provider.id, code="code-2", state=qs2["state"])
    assert cb2.status_code == 302, cb2.text
    payload2 = verify_session(
        token=cb2.cookies["primer_session"],
        secret=app.state.session_secret,
        max_age_seconds=7 * 86400,
    )

    assert payload1.user_id == payload2.user_id

    identities_page = await app.state.storage_provider.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=50)
    )
    matched_identities = [
        i for i in identities_page.items
        if i.provider_id == provider.id and i.subject == "sub-repeat"
    ]
    assert len(matched_identities) == 1

    users_page = await app.state.storage_provider.get_storage(User).list(
        OffsetPage(offset=0, length=50)
    )
    assert sum(1 for u in users_page.items if u.id == payload1.user_id) == 1


@pytest.mark.asyncio
@respx.mock
async def test_jit_role_clamped_to_safe_set(client, app, rsa_keypair):
    """``sso_default_access`` is unconstrained free text in ``system_state``
    -- the JIT path must clamp it to {"restricted", "user"} so a
    misconfigured (or malicious) "admin" value can never auto-provision an
    admin account. Only the exact strings "restricted"/"user" pass through;
    anything else, including unset/None, falls back to "restricted".
    """
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    cases = [
        ("admin", "sub-clamp-admin", "restricted"),
        ("user", "sub-clamp-user", "user"),
        (None, "sub-clamp-none", "restricted"),
    ]
    for configured_access, sub, expected_role in cases:
        await app.state.storage_provider.set_sso_default_access(configured_access)

        login_resp = await _login(client, provider.id)
        qs = _query(login_resp.headers["location"])
        idp.queue_id_token(priv, idp.base_claims(sub=sub, nonce=qs["nonce"]))
        cb = await _callback(client, provider.id, code=f"code-{sub}", state=qs["state"])
        assert cb.status_code == 302, cb.text

        payload = verify_session(
            token=cb.cookies["primer_session"],
            secret=app.state.session_secret,
            max_age_seconds=7 * 86400,
        )
        assert payload is not None
        user = await app.state.storage_provider.get_storage(User).get(payload.user_id)
        assert user is not None
        assert user.role == expected_role, (configured_access, sub, user.role)


# ---------------------------------------------------------------------------
# OAuth `state` query-param binding -- defense-in-depth on top of the
# signed state cookie + PKCE + id_token nonce.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_callback_state_param_round_tripped_happy_path(client, app, rsa_keypair):
    """The ``state`` value minted at /login is echoed by the (fake) IdP and
    must match what's bound inside the signed cookie -- happy path."""
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    assert qs["state"]

    idp.queue_id_token(priv, idp.base_claims(sub="sub-state-happy", nonce=qs["nonce"]))
    cb = await _callback(client, provider.id, code="code-state-happy", state=qs["state"])
    assert cb.status_code == 302, cb.text
    assert "primer_session" in cb.cookies


@pytest.mark.asyncio
@respx.mock
async def test_callback_mismatched_state_param_rejected(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-state-mismatch", nonce=qs["nonce"]))

    cb = await _callback(
        client, provider.id, code="code-state-mismatch", state="not-the-real-state",
    )
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"]["error"] == "invalid_state"
    assert "primer_session" not in cb.cookies


@pytest.mark.asyncio
@respx.mock
async def test_callback_missing_state_param_rejected(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-state-missing", nonce=qs["nonce"]))

    cb = await _callback(client, provider.id, code="code-state-missing", state=None)
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"]["error"] == "invalid_state"
    assert "primer_session" not in cb.cookies


# ---------------------------------------------------------------------------
# Rejections -- the security boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_jit_disabled_unknown_sub_rejected(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    # sso_jit_enabled is False by default -- never toggled in this test.

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-unknown", nonce=qs["nonce"]))

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"]["error"] == "sso_jit_disabled"
    assert "primer_session" not in cb.cookies

    identities_page = await app.state.storage_provider.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=50)
    )
    assert not any(i.subject == "sub-unknown" for i in identities_page.items)


@pytest.mark.asyncio
@respx.mock
async def test_disabled_matched_user_rejected(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)

    # Pre-seed a disabled user already linked via UserIdentity.
    disabled_user = await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-disabled-sso",
            username="disabledsso",
            password_hash=None,
            created_at=datetime.now(timezone.utc),
            role="user",
            disabled=True,
        )
    )
    await app.state.storage_provider.get_storage(UserIdentity).create(
        UserIdentity(
            user_id=disabled_user.id,
            provider_id=provider.id,
            subject="sub-disabled",
            created_at=datetime.now(timezone.utc),
        )
    )

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-disabled", nonce=qs["nonce"]))

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 403, cb.text
    assert cb.json()["detail"]["error"] == "account_disabled"
    assert "primer_session" not in cb.cookies


@pytest.mark.asyncio
@respx.mock
async def test_tampered_signature_rejected(client, app, rsa_keypair, attacker_rsa_keypair):
    _, pub = rsa_keypair
    attacker_priv, _ = attacker_rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    # Signed by an unrelated key while the JWKS advertises the real one.
    idp.queue_id_token(attacker_priv, idp.base_claims(sub="sub-tampered", nonce=qs["nonce"]))

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"]["error"] == "sso_validation_failed"
    assert "primer_session" not in cb.cookies


@pytest.mark.asyncio
@respx.mock
async def test_expired_id_token_rejected(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    now = int(time.time())
    idp.queue_id_token(
        priv,
        idp.base_claims(sub="sub-expired", nonce=qs["nonce"], iat=now - 1000, exp=now - 120),
    )

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"]["error"] == "sso_validation_failed"


@pytest.mark.asyncio
@respx.mock
async def test_wrong_nonce_rejected(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(
        priv, idp.base_claims(sub="sub-wrong-nonce", nonce="not-the-real-nonce"),
    )

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 400, cb.text
    assert cb.json()["detail"]["error"] == "sso_validation_failed"


# ---------------------------------------------------------------------------
# email_verified gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_email_set_only_when_verified(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(
        priv,
        idp.base_claims(
            sub="sub-verified", nonce=qs["nonce"], email="bob@example.com", email_verified=True,
        ),
    )
    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 302, cb.text
    payload = verify_session(
        token=cb.cookies["primer_session"],
        secret=app.state.session_secret,
        max_age_seconds=7 * 86400,
    )
    user = await app.state.storage_provider.get_storage(User).get(payload.user_id)
    assert user.email == "bob@example.com"


@pytest.mark.asyncio
@respx.mock
async def test_email_not_set_when_unverified(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id)
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(
        priv,
        idp.base_claims(
            sub="sub-unverified", nonce=qs["nonce"], email="carol@example.com",
            email_verified=False,
        ),
    )
    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 302, cb.text
    payload = verify_session(
        token=cb.cookies["primer_session"],
        secret=app.state.session_secret,
        max_age_seconds=7 * 86400,
    )
    user = await app.state.storage_provider.get_storage(User).get(payload.user_id)
    assert user.email is None


# ---------------------------------------------------------------------------
# Open-redirect guard on return_to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_return_to_same_origin_path_is_honoured(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id, return_to="/console/workspaces/1")
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-return-to", nonce=qs["nonce"]))

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/console/workspaces/1"


@pytest.mark.asyncio
@respx.mock
async def test_return_to_external_url_is_clamped(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await app.state.storage_provider.set_sso_jit_enabled(True)

    login_resp = await _login(client, provider.id, return_to="https://evil.example.com/steal")
    qs = _query(login_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-return-to-evil", nonce=qs["nonce"]))

    cb = await _callback(client, provider.id, code="code", state=qs["state"])
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/console/"


# ---------------------------------------------------------------------------
# Authenticated account linking -- GET /{provider_id}/link + shared callback
# ---------------------------------------------------------------------------


async def _login_as(
    client_: httpx.AsyncClient, app, *, user_id: str, username: str, role: str = "user",
) -> User:
    """Seed *user_id* directly and stamp *client_* with a valid session cookie.

    Bypasses ``POST /auth/register`` (locked to the FIRST user ever,
    single-user v1) and ``POST /auth/login`` (needs a real password
    hash) -- tests that need a SECOND already-logged-in account forge
    the cookie the same way the real login flow would, using the exact
    same signing helper (``sign_session``) the auth router uses.
    """
    sp = app.state.storage_provider
    user = await sp.get_storage(User).create(
        User(
            id=user_id, username=username, password_hash=None,
            created_at=datetime.now(timezone.utc), role=role,
        )
    )
    token = sign_session(
        user_id=user.id, username=user.username, secret=app.state.session_secret,
    )
    client_.cookies.set(app.state.config.auth.cookie_name, token)
    return user


async def _link(client_: httpx.AsyncClient, provider_id: str, **params) -> httpx.Response:
    return await client_.get(
        f"/v1/auth/sso/{provider_id}/link", params=params, follow_redirects=False,
    )


def _second_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
@respx.mock
async def test_link_requires_session(client, app, rsa_keypair):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)

    r = await _link(client, provider.id)
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
@respx.mock
async def test_link_callback_attaches_identity_to_logged_in_user(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    alice = await _login_as(client, app, user_id="user-alice", username="alice")

    users_before = await app.state.storage_provider.get_storage(User).list(
        OffsetPage(offset=0, length=50)
    )

    link_resp = await _link(client, provider.id)
    assert link_resp.status_code == 302, link_resp.text
    qs = _query(link_resp.headers["location"])

    idp.queue_id_token(priv, idp.base_claims(sub="sub-link-1", nonce=qs["nonce"]))
    cb = await _callback(client, provider.id, code="link-code-1", state=qs["state"])
    assert cb.status_code == 302, cb.text
    assert cb.headers["location"] == "/console/"
    # No new session minted -- the caller was already logged in as alice.
    assert "primer_session" not in cb.cookies

    users_after = await app.state.storage_provider.get_storage(User).list(
        OffsetPage(offset=0, length=50)
    )
    assert len(users_after.items) == len(users_before.items)

    identities = await app.state.storage_provider.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=50)
    )
    matched = [
        i for i in identities.items
        if i.provider_id == provider.id and i.subject == "sub-link-1"
    ]
    assert len(matched) == 1
    assert matched[0].user_id == alice.id


@pytest.mark.asyncio
@respx.mock
async def test_link_conflict_when_identity_owned_by_another_user(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)

    bob = await _login_as(client, app, user_id="user-bob", username="bob")
    # bob already owns this (provider_id, sub).
    await app.state.storage_provider.get_storage(UserIdentity).create(
        UserIdentity(
            user_id=bob.id, provider_id=provider.id, subject="sub-taken",
            created_at=datetime.now(timezone.utc),
        )
    )

    async with _second_client(app) as client2:
        await _login_as(client2, app, user_id="user-carol", username="carol")

        link_resp = await _link(client2, provider.id)
        assert link_resp.status_code == 302, link_resp.text
        qs = _query(link_resp.headers["location"])
        idp.queue_id_token(priv, idp.base_claims(sub="sub-taken", nonce=qs["nonce"]))
        cb = await _callback(client2, provider.id, code="link-code-conflict", state=qs["state"])
        assert cb.status_code == 409, cb.text

    identities = await app.state.storage_provider.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=50)
    )
    matched = [
        i for i in identities.items
        if i.provider_id == provider.id and i.subject == "sub-taken"
    ]
    assert len(matched) == 1
    assert matched[0].user_id == bob.id  # unchanged -- never reassigned


@pytest.mark.asyncio
@respx.mock
async def test_link_relinking_same_identity_is_idempotent(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    dave = await _login_as(client, app, user_id="user-dave", username="dave")
    await app.state.storage_provider.get_storage(UserIdentity).create(
        UserIdentity(
            user_id=dave.id, provider_id=provider.id, subject="sub-relink",
            created_at=datetime.now(timezone.utc),
        )
    )

    link_resp = await _link(client, provider.id)
    qs = _query(link_resp.headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub="sub-relink", nonce=qs["nonce"]))
    cb = await _callback(client, provider.id, code="link-code-relink", state=qs["state"])
    assert cb.status_code == 302, cb.text  # idempotent -- no 500 on the unique index

    identities = await app.state.storage_provider.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=50)
    )
    matched = [
        i for i in identities.items
        if i.provider_id == provider.id and i.subject == "sub-relink"
    ]
    assert len(matched) == 1
    assert matched[0].user_id == dave.id


# ---------------------------------------------------------------------------
# GET /identities + DELETE /identities/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identities_requires_session(client, app):
    r = await client.get("/v1/auth/sso/identities")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_list_identities_returns_only_callers_and_tolerates_deleted_provider(client, app):
    provider = await app.state.storage_provider.get_storage(OidcProvider).create(
        OidcProvider(
            name="Test IdP",
            discovery_url="https://idp-identities.example.com/.well-known/openid-configuration",
            client_id=CLIENT_ID,
        )
    )
    erin = await _login_as(client, app, user_id="user-erin", username="erin")
    now = datetime.now(timezone.utc)
    identity_storage = app.state.storage_provider.get_storage(UserIdentity)
    mine = await identity_storage.create(
        UserIdentity(
            user_id=erin.id, provider_id=provider.id, subject="sub-erin",
            email="erin@example.com", created_at=now,
        )
    )
    # Points at a provider that's since been deleted -- must not 500.
    orphaned = await identity_storage.create(
        UserIdentity(
            user_id=erin.id, provider_id="oidc-provider-deleted", subject="sub-erin-2",
            created_at=now,
        )
    )

    async with _second_client(app) as client2:
        frank = await _login_as(client2, app, user_id="user-frank", username="frank")
        await identity_storage.create(
            UserIdentity(
                user_id=frank.id, provider_id=provider.id, subject="sub-frank", created_at=now,
            )
        )

    r = await client.get("/v1/auth/sso/identities")
    assert r.status_code == 200, r.text
    items = r.json()
    ids = {item["id"] for item in items}
    assert ids == {mine.id, orphaned.id}

    matched = next(item for item in items if item["id"] == mine.id)
    assert matched["provider_id"] == provider.id
    assert matched["provider_name"] == "Test IdP"
    assert matched["subject"] == "sub-erin"
    assert matched["email"] == "erin@example.com"

    orphan_item = next(item for item in items if item["id"] == orphaned.id)
    assert orphan_item["provider_id"] == "oidc-provider-deleted"
    assert orphan_item["provider_name"]  # tolerate deleted provider -- no 500


@pytest.mark.asyncio
async def test_delete_identity_unlinks(client, app):
    provider = await app.state.storage_provider.get_storage(OidcProvider).create(
        OidcProvider(
            name="Test IdP",
            discovery_url="https://idp-del.example.com/.well-known/openid-configuration",
            client_id=CLIENT_ID,
        )
    )
    grace = await _login_as(client, app, user_id="user-grace", username="grace")
    identity_storage = app.state.storage_provider.get_storage(UserIdentity)
    row = await identity_storage.create(
        UserIdentity(
            user_id=grace.id, provider_id=provider.id, subject="sub-grace",
            created_at=datetime.now(timezone.utc),
        )
    )

    r = await client.delete(f"/v1/auth/sso/identities/{row.id}")
    assert r.status_code == 204, r.text
    assert await identity_storage.get(row.id) is None


@pytest.mark.asyncio
async def test_delete_identity_masked_404_for_other_users_identity(client, app):
    provider = await app.state.storage_provider.get_storage(OidcProvider).create(
        OidcProvider(
            name="Test IdP",
            discovery_url="https://idp-del2.example.com/.well-known/openid-configuration",
            client_id=CLIENT_ID,
        )
    )
    identity_storage = app.state.storage_provider.get_storage(UserIdentity)

    async with _second_client(app) as client2:
        heidi = await _login_as(client2, app, user_id="user-heidi", username="heidi")
        row = await identity_storage.create(
            UserIdentity(
                user_id=heidi.id, provider_id=provider.id, subject="sub-heidi",
                created_at=datetime.now(timezone.utc),
            )
        )

    await _login_as(client, app, user_id="user-ivan", username="ivan")

    r = await client.delete(f"/v1/auth/sso/identities/{row.id}")
    assert r.status_code == 404, r.text
    assert await identity_storage.get(row.id) is not None


@pytest.mark.asyncio
async def test_delete_identity_404_for_unknown_id(client, app):
    await _login_as(client, app, user_id="user-jack", username="jack")
    r = await client.delete("/v1/auth/sso/identities/does-not-exist")
    assert r.status_code == 404, r.text
