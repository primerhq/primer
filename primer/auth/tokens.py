"""Session token signing via ``itsdangerous``.

The session cookie's value is a ``itsdangerous.URLSafeTimedSerializer``-
produced string carrying a small JSON payload:

    {"uid": "<user_id>", "username": "<lowercase>", "src": "<auth source>"}

``src`` records how the session was established: ``"local"`` for
password login (the default), or an OIDC provider id (e.g.
``"oidc-provider-1"``) for SSO-minted sessions. Legacy cookies signed
before this field existed have no ``src`` key; ``verify_session``
defaults those to ``"local"`` rather than rejecting them.

The serializer's HMAC-SHA256 signature is appended; ``verify_session``
re-checks the signature and the max-age (``session_ttl_days``) on read.

We do NOT include the full :class:`User` row in the token: the middleware
re-reads from storage on every request so a deleted/disabled user can't
keep using a still-valid cookie. The cookie just identifies which user
to look up.
"""

from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeTimedSerializer,
)


_SALT = "primer.session.v1"


@dataclass(frozen=True)
class SessionPayload:
    """Decoded session cookie contents."""

    user_id: str
    username: str
    src: str = "local"


def sign_session(
    *, user_id: str, username: str, secret: str, src: str = "local",
) -> str:
    """Produce a signed cookie value for the given user."""
    s = URLSafeTimedSerializer(secret, salt=_SALT)
    return s.dumps({"uid": user_id, "username": username, "src": src})


def verify_session(
    *, token: str, secret: str, max_age_seconds: int,
) -> SessionPayload | None:
    """Verify the signature + age of a cookie value.

    Returns ``None`` for any failure (missing/expired/forged/malformed).
    Callers only need the truthy / falsy distinction; logging of the
    exact reason happens in the middleware if needed.
    """
    if not token:
        return None
    s = URLSafeTimedSerializer(secret, salt=_SALT)
    try:
        payload = s.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    username = payload.get("username")
    src = payload.get("src", "local")
    if not isinstance(uid, str) or not isinstance(username, str):
        return None
    if not isinstance(src, str):
        src = "local"
    return SessionPayload(user_id=uid, username=username, src=src)
