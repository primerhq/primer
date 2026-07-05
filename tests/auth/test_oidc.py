"""Tests for the OIDC client core: discovery, JWKS, id_token validation,
PKCE, and state sign/verify.

All provider interaction is mocked via ``respx`` -- no real network IO.
id_tokens are signed with a locally generated RSA keypair via ``jwt.encode``
so ``validate_id_token`` is exercised against real RS256 signatures rather
than hand-rolled crypto.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import httpx
import jwt
import pytest
import respx
from jwt.algorithms import RSAAlgorithm

from primer.auth import oidc
from primer.model.oidc import OidcProvider


CLIENT_ID = "test-client"
ISSUER = "https://idp.example.com/"
KID = "test-signing-key-1"


# --- shared crypto fixtures -------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def other_rsa_keypair():
    """A second, unrelated keypair -- stands in for an attacker's key."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _jwk_dict(public_key, kid: str) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _jwks(public_key, kid: str = KID) -> dict:
    return {"keys": [_jwk_dict(public_key, kid)]}


def _provider(client_id: str = CLIENT_ID) -> OidcProvider:
    return OidcProvider(
        name="Test IdP",
        discovery_url="https://idp.example.com/.well-known/openid-configuration",
        client_id=client_id,
        client_secret=None,
    )


def _metadata(**overrides) -> oidc.OidcMetadata:
    fields = {
        "issuer": ISSUER,
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/jwks.json",
        "id_token_signing_alg": "RS256",
    }
    fields.update(overrides)
    return oidc.OidcMetadata(**fields)


def _claims(*, nonce: str = "expected-nonce", aud=None, iss=None, exp_delta: int = 300, **overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": iss if iss is not None else ISSUER,
        "aud": aud if aud is not None else CLIENT_ID,
        "sub": "user-123",
        "iat": now,
        "exp": now + exp_delta,
        "nonce": nonce,
    }
    claims.update(overrides)
    return claims


def _sign(private_key, claims: dict, *, kid: str | None = KID, alg: str = "RS256") -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, private_key, algorithm=alg, headers=headers)


# --- PKCE --------------------------------------------------------------


class TestMakePkce:
    def test_returns_s256_challenge(self):
        verifier, challenge = oidc.make_pkce()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert challenge == expected
        # Unpadded base64url per RFC 7636 -- no '=' padding, no '+'/'/'.
        assert "=" not in verifier
        assert "=" not in challenge
        assert 43 <= len(verifier) <= 128

    def test_generates_fresh_pair_each_call(self):
        v1, c1 = oidc.make_pkce()
        v2, c2 = oidc.make_pkce()
        assert v1 != v2
        assert c1 != c2


# --- state / nonce -------------------------------------------------------


class TestState:
    def test_round_trip(self):
        secret = "s" * 32
        token = oidc.sign_state({"toolset": "x", "n": 1}, secret)
        assert oidc.verify_state(token, secret, 60) == {"toolset": "x", "n": 1}

    def test_wrong_secret_rejects(self):
        token = oidc.sign_state({"a": 1}, "secret-A" * 4)
        assert oidc.verify_state(token, "secret-B" * 4, 60) is None

    def test_tampered_token_rejects(self):
        secret = "s" * 32
        token = oidc.sign_state({"a": 1}, secret)
        # Flip the last character (part of the HMAC signature suffix).
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        assert oidc.verify_state(tampered, secret, 60) is None

    def test_expired_token_rejects(self):
        secret = "s" * 32
        token = oidc.sign_state({"a": 1}, secret)
        time.sleep(2.05)  # itsdangerous timestamps are second-resolution
        assert oidc.verify_state(token, secret, 1) is None

    def test_empty_token_rejects(self):
        assert oidc.verify_state("", "s" * 32, 60) is None

    def test_garbage_token_rejects(self):
        assert oidc.verify_state("not-a-token", "s" * 32, 60) is None


def test_gen_state_and_nonce_are_unique_and_nonempty():
    assert oidc.gen_state() and oidc.gen_nonce()
    assert oidc.gen_state() != oidc.gen_state()
    assert oidc.gen_nonce() != oidc.gen_nonce()


# --- discovery -----------------------------------------------------------


class TestDiscover:
    @respx.mock
    async def test_parses_metadata(self):
        url = f"https://idp-{uuid.uuid4().hex}.example.com/.well-known/openid-configuration"
        doc = {
            "issuer": "https://idp.example.com/",
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256", "HS256"],
        }
        respx.get(url).mock(return_value=httpx.Response(200, json=doc))

        metadata = await oidc.discover(url)

        assert metadata.issuer == doc["issuer"]
        assert metadata.authorization_endpoint == doc["authorization_endpoint"]
        assert metadata.token_endpoint == doc["token_endpoint"]
        assert metadata.jwks_uri == doc["jwks_uri"]
        # RS256 preferred over HS256 -- HS256 is symmetric and must never
        # be selected even when the provider advertises it as a fallback.
        assert metadata.id_token_signing_alg == "RS256"

    @respx.mock
    async def test_caches_repeat_calls(self):
        url = f"https://idp-{uuid.uuid4().hex}.example.com/.well-known/openid-configuration"
        doc = {
            "issuer": "https://idp.example.com/",
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        route = respx.get(url).mock(return_value=httpx.Response(200, json=doc))

        await oidc.discover(url)
        await oidc.discover(url)

        assert route.call_count == 1

    @respx.mock
    async def test_rejects_symmetric_only_alg(self):
        url = f"https://idp-{uuid.uuid4().hex}.example.com/.well-known/openid-configuration"
        doc = {
            "issuer": "https://idp.example.com/",
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks.json",
            "id_token_signing_alg_values_supported": ["HS256"],
        }
        respx.get(url).mock(return_value=httpx.Response(200, json=doc))

        with pytest.raises(oidc.OidcError):
            await oidc.discover(url)

    @respx.mock
    async def test_http_error_raises_oidc_error(self):
        url = f"https://idp-{uuid.uuid4().hex}.example.com/.well-known/openid-configuration"
        respx.get(url).mock(return_value=httpx.Response(500))

        with pytest.raises(oidc.OidcError):
            await oidc.discover(url)


# --- JWKS ------------------------------------------------------------------


class TestFetchJwks:
    @respx.mock
    async def test_caches_and_refetches_on_unknown_kid(self, rsa_keypair, other_rsa_keypair):
        _, pub1 = rsa_keypair
        _, pub2 = other_rsa_keypair
        url = f"https://idp-{uuid.uuid4().hex}.example.com/jwks.json"

        route = respx.get(url).mock(
            side_effect=[
                httpx.Response(200, json=_jwks(pub1, kid="key-1")),
                httpx.Response(200, json=_jwks(pub2, kid="key-2")),
            ]
        )

        first = await oidc.fetch_jwks(url)
        assert _has_kid(first, "key-1")

        # Still within TTL and key-1 present -- second call for a known
        # kid must be served from cache, not hit the network again.
        cached = await oidc.fetch_jwks(url, kid="key-1")
        assert route.call_count == 1
        assert cached == first

        # Asking for a kid absent from the cached JWKS forces a re-fetch
        # (models a provider key rotation) rather than waiting for TTL.
        refreshed = await oidc.fetch_jwks(url, kid="key-2")
        assert route.call_count == 2
        assert _has_kid(refreshed, "key-2")


def _has_kid(jwks: dict, kid: str) -> bool:
    return any(k.get("kid") == kid for k in jwks["keys"])


# --- id_token validation ---------------------------------------------------


class TestValidateIdToken:
    async def test_accepts_well_formed_token(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(priv, _claims())

        claims = await oidc.validate_id_token(
            token,
            provider=_provider(),
            metadata=_metadata(),
            jwks=_jwks(pub),
            nonce="expected-nonce",
        )

        assert claims["sub"] == "user-123"
        assert claims["iss"] == ISSUER

    async def test_rejects_alg_none(self, rsa_keypair):
        _, pub = rsa_keypair
        # jwt.encode with algorithm="none" produces an unsigned token --
        # simulates an attacker stripping the signature and downgrading alg.
        token = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": KID})

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_wrong_issuer(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(priv, _claims(iss="https://evil.example.com/"))

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_aud_missing_client_id(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(priv, _claims(aud="some-other-client"))

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_multi_aud_without_matching_azp(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(
            priv,
            _claims(aud=[CLIENT_ID, "some-other-client"], azp="some-other-client"),
        )

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_accepts_multi_aud_with_matching_azp(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(
            priv,
            _claims(aud=[CLIENT_ID, "some-other-client"], azp=CLIENT_ID),
        )

        claims = await oidc.validate_id_token(
            token,
            provider=_provider(),
            metadata=_metadata(),
            jwks=_jwks(pub),
            nonce="expected-nonce",
        )
        assert claims["azp"] == CLIENT_ID

    async def test_rejects_expired_token(self, rsa_keypair):
        priv, pub = rsa_keypair
        # Beyond the ~60s leeway, so this is unambiguously expired.
        token = _sign(priv, _claims(exp_delta=-120))

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_wrong_nonce(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(priv, _claims(nonce="some-other-nonce"))

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_bad_signature(self, rsa_keypair, other_rsa_keypair):
        _, pub = rsa_keypair
        attacker_priv, _ = other_rsa_keypair
        # Signed by an unrelated key but claiming the legitimate kid --
        # signature verification against the real public key must fail.
        token = _sign(attacker_priv, _claims())

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_unknown_kid(self, rsa_keypair):
        priv, pub = rsa_keypair
        token = _sign(priv, _claims(), kid="kid-not-in-jwks")

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )


# --- code exchange -----------------------------------------------------


class TestExchangeCode:
    @respx.mock
    async def test_posts_to_token_endpoint(self):
        token_url = f"https://idp-{uuid.uuid4().hex}.example.com/token"
        route = respx.post(token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "at", "id_token": "it", "token_type": "Bearer"}
            )
        )

        result = await oidc.exchange_code(
            metadata=_metadata(token_endpoint=token_url),
            provider=_provider(),
            code="auth-code",
            code_verifier="verifier-value",
            redirect_uri="https://app.example.com/callback",
        )

        assert result["access_token"] == "at"
        sent = route.calls.last.request
        body = sent.content.decode()
        assert "code=auth-code" in body
        assert "code_verifier=verifier-value" in body

    @respx.mock
    async def test_error_response_raises_oidc_error(self):
        token_url = f"https://idp-{uuid.uuid4().hex}.example.com/token"
        respx.post(token_url).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )

        with pytest.raises(oidc.OidcError):
            await oidc.exchange_code(
                metadata=_metadata(token_endpoint=token_url),
                provider=_provider(),
                code="bad-code",
                code_verifier="verifier-value",
                redirect_uri="https://app.example.com/callback",
            )
