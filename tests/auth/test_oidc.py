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
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

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


@pytest.fixture(scope="module")
def ec_keypair():
    """EC (P-256) keypair -- used to prove the full asymmetric-alg-subset
    pin actually accepts a second asymmetric algorithm (ES256), not just
    the single preferred RS256."""
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def _jwk_dict(public_key, kid: str, *, alg: str = "RS256") -> dict:
    to_jwk = ECAlgorithm.to_jwk if alg.upper().startswith("ES") else RSAAlgorithm.to_jwk
    jwk = json.loads(to_jwk(public_key))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = alg
    return jwk


def _jwks(public_key, kid: str = KID, *, alg: str = "RS256") -> dict:
    return {"keys": [_jwk_dict(public_key, kid, alg=alg)]}


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
        "id_token_signing_algs": ["RS256"],
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


def _claims_without(*keys: str, **kwargs) -> dict:
    """``_claims()`` with the given claim(s) removed entirely -- for
    proving `options={"require": [...]}` actually rejects an *absent*
    claim (as opposed to one present-but-falsy, which "require" does not
    catch)."""
    claims = _claims(**kwargs)
    for key in keys:
        claims.pop(key, None)
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
        # HS256 is symmetric and must never be pinned, even alongside an
        # asymmetric alg the provider also advertises.
        assert metadata.id_token_signing_algs == ["RS256"]

    @respx.mock
    async def test_parses_full_asymmetric_alg_subset(self):
        """A provider advertising more than one asymmetric alg must have
        ALL of them pinned -- not just a single "preferred" one -- so we
        don't lock out a provider that (e.g.) uses RS256 for some keys
        and ES256 for others."""
        url = f"https://idp-{uuid.uuid4().hex}.example.com/.well-known/openid-configuration"
        doc = {
            "issuer": "https://idp.example.com/",
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256", "ES256", "HS256"],
        }
        respx.get(url).mock(return_value=httpx.Response(200, json=doc))

        metadata = await oidc.discover(url)

        assert metadata.id_token_signing_algs == ["RS256", "ES256"]

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

    async def test_rejects_missing_sub(self, rsa_keypair):
        priv, pub = rsa_keypair
        # `sub` is the account-matching key -- a token without one must
        # never reach account-matching logic downstream.
        token = _sign(priv, _claims_without("sub"))

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="expected-nonce",
            )

    async def test_rejects_empty_expected_nonce_even_if_token_omits_nonce(
        self, rsa_keypair
    ):
        """An empty/falsy expected nonce must never be treated as
        satisfied. A caller that (by bug) generates/stores an
        empty-string nonce, paired with a token that also has no nonce
        claim, must still be rejected -- a blank nonce provides no
        session-binding at all."""
        priv, pub = rsa_keypair
        token = _sign(priv, _claims_without("nonce"))

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(),
                jwks=_jwks(pub),
                nonce="",
            )

    async def test_accepts_es256_when_provider_advertises_multiple_asymmetric_algs(
        self, ec_keypair
    ):
        """Proves the full-asymmetric-alg-subset pin: a provider that
        advertises both RS256 and ES256 must have an ES256-signed token
        accepted, not just RS256 -- pinning a single "preferred" alg
        would otherwise lock this provider's ES256-signed tokens out."""
        priv, pub = ec_keypair
        token = _sign(priv, _claims(), alg="ES256")

        claims = await oidc.validate_id_token(
            token,
            provider=_provider(),
            metadata=_metadata(id_token_signing_algs=["RS256", "ES256"]),
            jwks=_jwks(pub, alg="ES256"),
            nonce="expected-nonce",
        )

        assert claims["sub"] == "user-123"

    async def test_multi_alg_provider_still_rejects_alg_none(self, ec_keypair):
        _, pub = ec_keypair
        token = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": KID})

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(id_token_signing_algs=["RS256", "ES256"]),
                jwks=_jwks(pub, alg="ES256"),
                nonce="expected-nonce",
            )

    async def test_multi_alg_provider_still_rejects_hs256(self, ec_keypair):
        _, pub = ec_keypair
        # HS256 is symmetric -- even though it's not in the pinned list,
        # this proves an HS256-signed token is rejected outright rather
        # than accidentally verified against the EC public key bytes.
        token = jwt.encode(
            _claims(),
            "some-hmac-secret-that-is-at-least-32-bytes-long",
            algorithm="HS256",
            headers={"kid": KID},
        )

        with pytest.raises(oidc.OidcError):
            await oidc.validate_id_token(
                token,
                provider=_provider(),
                metadata=_metadata(id_token_signing_algs=["RS256", "ES256"]),
                jwks=_jwks(pub, alg="ES256"),
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
    async def test_confidential_client_sends_basic_auth(self):
        """A provider WITH a client_secret authenticates the token-exchange
        request via HTTP Basic (RFC 6749 S2.3.1) rather than relying on
        client_id alone in the body -- deferred from Task 4 because the
        real exercise of this path only shows up once Task 6 wires a
        provider that actually carries a client_secret."""
        token_url = f"https://idp-{uuid.uuid4().hex}.example.com/token"
        route = respx.post(token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "at", "id_token": "it", "token_type": "Bearer"}
            )
        )
        provider = OidcProvider(
            name="Confidential IdP",
            discovery_url="https://idp.example.com/.well-known/openid-configuration",
            client_id=CLIENT_ID,
            client_secret="shh-its-a-secret",
        )

        result = await oidc.exchange_code(
            metadata=_metadata(token_endpoint=token_url),
            provider=provider,
            code="auth-code",
            code_verifier="verifier-value",
            redirect_uri="https://app.example.com/callback",
        )

        assert result["access_token"] == "at"
        sent = route.calls.last.request
        import base64 as _b64

        auth_header = sent.headers.get("authorization")
        assert auth_header is not None and auth_header.startswith("Basic ")
        decoded = _b64.b64decode(auth_header.removeprefix("Basic ")).decode()
        assert decoded == f"{CLIENT_ID}:shh-its-a-secret"

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
