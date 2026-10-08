"""Session token signing via ``itsdangerous``.

The session cookie's value is a ``itsdangerous.URLSafeTimedSerializer``-
produced string carrying a small JSON payload:

    {"uid": "<user_id>", "username": "<lowercase>", "src": "<auth source>",
     "ep": <session epoch>}

``src`` records how the session was established: ``"local"`` for
password login (the default), or an OIDC provider id (e.g.
``"oidc-provider-1"``) for SSO-minted sessions. Legacy cookies signed
before this field existed have no ``src`` key; ``verify_session``
defaults those to ``"local"`` rather than rejecting them.

``ep`` is the user's :attr:`User.session_epoch` when the cookie was minted
(SEC-05). The auth middleware rejects a cookie whose epoch is not the
user's current one, so bumping the epoch revokes every outstanding cookie.
A legacy cookie with no ``ep`` counts as epoch 0; a present but non-integer
``ep`` is malformed and rejected.

The serializer's HMAC-SHA256 signature is appended; ``verify_session``
re-checks the signature and the max-age (``session_ttl_days``) on read.

We do NOT include the full :class:`User` row in the token: the middleware
re-reads from storage on every request so a deleted/disabled user can't
keep using a still-valid cookie. The cookie just identifies which user
to look up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

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
    epoch: int = 0
    # When the cookie was signed (UTC), from the signature's own timestamp. Not part of what two payloads equal: it says when, not who.
    issued_at: datetime | None = field(default=None, compare=False)


def sign_session(
    *, user_id: str, username: str, secret: str, src: str = "local", epoch: int = 0,
) -> str:
    """Produce a signed cookie value for the given user at session ``epoch``."""
    s = URLSafeTimedSerializer(secret, salt=_SALT)
    return s.dumps({"uid": user_id, "username": username, "src": src, "ep": epoch})


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
        payload, issued_at = s.loads(token, max_age=max_age_seconds, return_timestamp=True)
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
    epoch = payload.get("ep", 0)
    # bool is an int subclass; a signed cookie never carries one, so treat it as malformed.
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        return None
    return SessionPayload(user_id=uid, username=username, src=src, epoch=epoch, issued_at=issued_at)
