"""Cookie-based session authentication for primer.

Single-user in v1. See the design discussion in
``docs/superpowers/specs/`` for the broader auth surface roadmap
(SSO, API keys, role-based access).
"""

from primer.auth.passwords import hash_password, verify_password
from primer.auth.tokens import sign_session, verify_session, SessionPayload

__all__ = [
    "hash_password",
    "verify_password",
    "sign_session",
    "verify_session",
    "SessionPayload",
]
