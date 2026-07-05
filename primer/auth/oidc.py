"""OIDC client core: discovery, JWKS, id_token validation, PKCE, state.

Security-critical module. :func:`validate_id_token` is the auth boundary
for Layer 2 SSO -- every check it performs guards against a specific
real-world attack:

* **alg pinning** -- ``algorithms`` passed to :func:`jwt.decode` is
  ALWAYS the full set of asymmetric algorithms the provider advertised
  at discovery time (never derived from the token's own header). This
  is the standard defense against the "alg confusion" family of
  attacks (RFC 8725 S2.1): an attacker cannot downgrade to ``alg:
  none`` or to an HMAC algorithm keyed with the provider's *public*
  RSA/EC key. Pinning the full asymmetric subset (rather than a single
  preferred alg) avoids locking out providers that rotate between, or
  simultaneously advertise, more than one asymmetric algorithm (e.g.
  RS256 + ES256).
* **iss** -- exact string match against the discovered issuer.
* **aud / azp** -- ``client_id`` must be a member of ``aud`` (which may
  be a single string or a list); when the token carries more than one
  audience, ``azp`` must equal ``client_id`` (OIDC Core 3.1.3.7 #6),
  so a token minted for a *different* client of the same provider
  cannot be replayed against us.
* **exp** -- checked with ~60s leeway for clock skew.
* **nonce** -- must equal the value the caller generated for this
  login attempt, binding the id_token to a single browser session.

Discovery documents and JWKS are TTL-cached in module-level dicts
keyed by URL so a login flow doesn't round-trip to the provider on
every request; :func:`fetch_jwks` additionally bypasses the cache
when asked to look up a ``kid`` it doesn't recognize, so a provider's
key rotation doesn't lock users out until the TTL happens to expire.
That bypass is itself rate-limited per JWKS URI (see
``_JWKS_REFETCH_COOLDOWN_SECONDS``) so an attacker sending id_tokens
with random ``kid``s can't turn every login attempt into a forced
network round-trip.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from primer.model.except_ import PrimerError
from primer.model.oidc import OidcProvider


_STATE_SALT = "primer.oidc.state.v1"

# RFC 8725 S3.1: never accept a symmetric (HS*) or "none" alg for a
# provider-signed id_token -- both open key-confusion attacks (the
# provider's *public* signing key would be usable as an HMAC secret).
_ASYMMETRIC_ALG_PREFIXES = ("RS", "ES", "PS")

_DISCOVERY_TTL_SECONDS = 3600.0
_JWKS_TTL_SECONDS = 3600.0
_HTTP_TIMEOUT_SECONDS = 10.0

# Anti-abuse bound on fetch_jwks's unknown-kid-triggered refresh: without
# this, an attacker sending id_tokens with random `kid`s could force a
# JWKS network round-trip on every single login attempt (a DoS on us and
# on the provider). At most one forced refetch per URI per cooldown
# window; anything sooner just uses the cache as-is and fails closed,
# same as before this feature existed.
_JWKS_REFETCH_COOLDOWN_SECONDS = 60.0

# ~60s leeway on exp/iat/nbf checks to absorb clock skew between us and
# the provider, per the brief's "exp with ~60s leeway" requirement.
_EXP_LEEWAY_SECONDS = 60


class OidcError(PrimerError):
    """Raised for any discovery / JWKS / id_token-validation failure.

    Callers MUST treat this as a hard auth-bypass-prevention boundary:
    on this exception, the login attempt is rejected outright -- never
    fall back to a partially-validated claim set.
    """


class OidcMetadata(BaseModel):
    """The subset of the OpenID Provider discovery document we rely on."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    id_token_signing_algs: list[str] = Field(
        ...,
        description=(
            "All asymmetric algorithms from the provider's "
            "id_token_signing_alg_values_supported; this is what gets "
            "pinned wholesale as jwt.decode's `algorithms=`."
        ),
    )


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


# Module-level TTL caches, keyed by URL. Deliberately simple (no lock):
# worst case under concurrent misses is a few redundant fetches, not a
# correctness problem, and this module's callers are per-request async
# tasks rather than long-lived threads contending on the dict.
_discovery_cache: dict[str, _CacheEntry] = {}
_jwks_cache: dict[str, _CacheEntry] = {}

# Monotonic timestamp of the last unknown-kid-triggered (forced) JWKS
# refetch per jwks_uri -- NOT updated on ordinary TTL-driven or
# first-ever fetches, only on the bypass path. Backs the cooldown above.
_jwks_forced_refetch_at: dict[str, float] = {}


def _pick_signing_algs(supported: list[str] | None) -> list[str]:
    """Choose the full set of asymmetric algs to pin from the discovery doc.

    Pinning the whole asymmetric subset (rather than a single preferred
    alg) keeps us from locking out a provider that advertises more than
    one asymmetric algorithm (e.g. ``["RS256", "ES256"]``) -- any of
    them is safe to accept since none is symmetric/none.

    Raises :class:`OidcError` if the provider advertises only symmetric
    (HS*) algorithms -- we refuse to configure ourselves into a
    key-confusion hole even before a single id_token is seen.
    """
    algs = supported or ["RS256"]
    asymmetric = [a for a in algs if a.upper().startswith(_ASYMMETRIC_ALG_PREFIXES)]
    if not asymmetric:
        raise OidcError(
            "provider does not advertise any asymmetric id_token signing "
            f"algorithm (id_token_signing_alg_values_supported={algs!r})"
        )
    return asymmetric


async def discover(discovery_url: str) -> OidcMetadata:
    """Fetch + TTL-cache the OpenID Provider discovery document."""
    now = time.monotonic()
    cached = _discovery_cache.get(discovery_url)
    if cached is not None and cached.expires_at > now:
        return cached.value

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.get(discovery_url)
        except httpx.HTTPError as exc:
            raise OidcError(
                f"discovery request to {discovery_url!r} failed: {exc}", cause=exc
            ) from exc

    if resp.status_code != 200:
        raise OidcError(
            f"discovery endpoint {discovery_url!r} returned HTTP {resp.status_code}"
        )
    try:
        doc = resp.json()
    except ValueError as exc:
        raise OidcError(
            f"discovery document at {discovery_url!r} is not valid JSON"
        ) from exc
    if not isinstance(doc, dict):
        raise OidcError(f"discovery document at {discovery_url!r} is not a JSON object")

    try:
        metadata = OidcMetadata(
            issuer=doc["issuer"],
            authorization_endpoint=doc["authorization_endpoint"],
            token_endpoint=doc["token_endpoint"],
            jwks_uri=doc["jwks_uri"],
            id_token_signing_algs=_pick_signing_algs(
                doc.get("id_token_signing_alg_values_supported")
            ),
        )
    except KeyError as exc:
        raise OidcError(
            f"discovery document at {discovery_url!r} missing required field: {exc}"
        ) from exc

    _discovery_cache[discovery_url] = _CacheEntry(
        value=metadata, expires_at=now + _DISCOVERY_TTL_SECONDS
    )
    return metadata


def _has_kid(jwks: dict, kid: str) -> bool:
    return any(k.get("kid") == kid for k in jwks.get("keys", []))


def unverified_kid(id_token: str) -> str | None:
    """Best-effort read of the ``kid`` header claim, WITHOUT verifying
    the token's signature.

    This is purely a hint for :func:`fetch_jwks` -- callers should pass
    its result as ``fetch_jwks(..., kid=unverified_kid(id_token))``
    *before* calling :func:`validate_id_token`, so a ``kid`` the cache
    doesn't recognize (e.g. because the provider just rotated its
    signing key) triggers a refresh instead of failing closed until the
    TTL expires. The header is completely attacker-controlled at this
    point, so this must never be used for anything security-relevant --
    :func:`validate_id_token` re-parses the header itself and is the
    only thing that actually enforces anything. Returns ``None`` on any
    malformed input; a malformed token is still correctly rejected
    downstream by :func:`validate_id_token`.
    """
    try:
        return jwt.get_unverified_header(id_token).get("kid")
    except jwt.PyJWTError:
        return None


async def fetch_jwks(jwks_uri: str, *, kid: str | None = None) -> dict:
    """Fetch + TTL-cache the JWKS document at ``jwks_uri``.

    If ``kid`` is given and is not present among the currently cached
    keys, the cache is treated as stale (the provider likely rotated
    its signing key) and a fresh copy is fetched immediately, bypassing
    the TTL -- this keeps a rotation from stranding logins until the
    next TTL expiry.

    That bypass is rate-limited to at most one forced refetch per
    ``jwks_uri`` per :data:`_JWKS_REFETCH_COOLDOWN_SECONDS`: a ``kid``
    that's unknown while a forced refetch for this URI happened more
    recently than the cooldown just falls through to the still-cached
    value (and the caller fails closed), instead of hitting the network
    again. This is what keeps an unknown ``kid`` from being usable as a
    DoS lever (e.g. an attacker submitting id_tokens with random
    ``kid``s to force a JWKS round-trip on every request).
    """
    now = time.monotonic()
    cached = _jwks_cache.get(jwks_uri)
    if cached is not None and cached.expires_at > now:
        if kid is None or _has_kid(cached.value, kid):
            return cached.value
        last_forced = _jwks_forced_refetch_at.get(jwks_uri)
        if last_forced is not None and now - last_forced < _JWKS_REFETCH_COOLDOWN_SECONDS:
            return cached.value
        _jwks_forced_refetch_at[jwks_uri] = now

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.get(jwks_uri)
        except httpx.HTTPError as exc:
            raise OidcError(
                f"JWKS request to {jwks_uri!r} failed: {exc}", cause=exc
            ) from exc

    if resp.status_code != 200:
        raise OidcError(f"JWKS endpoint {jwks_uri!r} returned HTTP {resp.status_code}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise OidcError(f"JWKS document at {jwks_uri!r} is not valid JSON") from exc
    if not isinstance(doc, dict) or "keys" not in doc:
        raise OidcError(f"JWKS document at {jwks_uri!r} is missing a 'keys' array")

    _jwks_cache[jwks_uri] = _CacheEntry(value=doc, expires_at=now + _JWKS_TTL_SECONDS)
    return doc


def make_pkce() -> tuple[str, str]:
    """Generate a fresh PKCE (verifier, challenge) pair using S256.

    Verifier is 32 random bytes, base64url-encoded (43 chars, no
    padding). Challenge is base64url(sha256(verifier)), also unpadded.
    """
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def gen_state() -> str:
    """Fresh high-entropy value for the OAuth ``state`` parameter."""
    return secrets.token_urlsafe(32)


def gen_nonce() -> str:
    """Fresh high-entropy value for the OIDC ``nonce`` parameter."""
    return secrets.token_urlsafe(32)


def sign_state(payload: dict, secret: str) -> str:
    """Sign a state payload (dict) for embedding as the ``state`` param."""
    s = URLSafeTimedSerializer(secret, salt=_STATE_SALT)
    return s.dumps(payload)


def verify_state(token: str, secret: str, max_age: int) -> dict | None:
    """Verify signature + age of a ``state`` token.

    Returns ``None`` for any failure (missing/expired/forged/malformed)
    -- callers only need the truthy/falsy distinction, mirroring
    :func:`primer.auth.tokens.verify_session`.
    """
    if not token:
        return None
    s = URLSafeTimedSerializer(secret, salt=_STATE_SALT)
    try:
        payload = s.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


async def validate_id_token(
    id_token: str,
    *,
    provider: OidcProvider,
    metadata: OidcMetadata,
    jwks: dict,
    nonce: str,
) -> dict:
    """Validate an id_token per OIDC Core 3.1.3.7, returning verified claims.

    Raises :class:`OidcError` on any failure. See the module docstring
    for the full list of checks and the attack each one closes.
    """
    algs = metadata.id_token_signing_algs
    if not algs or any(not a.upper().startswith(_ASYMMETRIC_ALG_PREFIXES) for a in algs):
        # Defense in depth: even if `metadata` was hand-built rather than
        # produced by discover(), never verify with an empty alg set or
        # one containing a symmetric/none alg.
        raise OidcError(
            f"refusing to validate id_token with non-asymmetric alg set {algs!r}"
        )

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError(f"malformed id_token header: {exc}") from exc

    try:
        jwk_set = jwt.PyJWKSet.from_dict(jwks)
    except jwt.PyJWTError as exc:
        raise OidcError(f"invalid JWKS document: {exc}") from exc

    kid = header.get("kid")
    if kid is not None:
        try:
            signing_key = jwk_set[kid]
        except KeyError as exc:
            raise OidcError(f"no JWKS key found for kid={kid!r}") from exc
    elif len(jwk_set.keys) == 1:
        signing_key = jwk_set.keys[0]
    else:
        raise OidcError(
            "id_token header has no 'kid' and JWKS has multiple keys; "
            "cannot unambiguously select a signing key"
        )

    try:
        claims = jwt.decode(
            id_token,
            key=signing_key.key,
            # Pinned from discovery metadata, NEVER from the token's own
            # header -- this is what makes alg-confusion attacks
            # (alg:none, HS256-with-public-RSA-key) impossible.
            algorithms=algs,
            audience=provider.client_id,
            issuer=metadata.issuer,
            leeway=_EXP_LEEWAY_SECONDS,
            # `sub` is required -- it's the key used to match the token
            # to a local UserIdentity row; a token without one must never
            # reach account-matching logic.
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"id_token signature/claims validation failed: {exc}") from exc

    # jwt.decode's `audience=` check only proves client_id is a MEMBER of
    # aud (str or list) -- it does not reject a token also aimed at other
    # clients. OIDC Core 3.1.3.7 #6 closes that gap: when aud names more
    # than one client, azp must identify us specifically.
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if len(aud_list) > 1 and claims.get("azp") != provider.client_id:
        raise OidcError(
            "id_token has multiple audiences but azp does not equal client_id"
        )

    # `not nonce` closes the vacuous-empty case: if the caller passes an
    # empty/falsy expected nonce and the token also omits (or blanks)
    # its nonce claim, `claims.get("nonce") != nonce` alone would be
    # `"" != ""` -> False and wrongly "pass". A blank nonce provides no
    # session-binding at all, so it must always be rejected.
    if not nonce or claims.get("nonce") != nonce:
        raise OidcError("id_token nonce does not match the expected value")

    return claims


async def exchange_code(
    *,
    metadata: OidcMetadata,
    provider: OidcProvider,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict:
    """Trade an authorization code for tokens at the provider's token_endpoint."""
    body: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "client_id": provider.client_id,
    }
    auth: tuple[str, str] | None = None
    if provider.client_secret is not None:
        auth = (provider.client_id, provider.client_secret.get_secret_value())

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(
                metadata.token_endpoint,
                data=body,
                auth=auth,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise OidcError(
                f"token exchange request failed: {exc}", cause=exc
            ) from exc

    if resp.status_code >= 400:
        raise OidcError(
            f"token endpoint returned HTTP {resp.status_code}: {resp.text}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise OidcError("token endpoint response is not valid JSON") from exc
