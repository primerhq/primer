"""Argon2id password hashing + verification.

argon2-cffi is the OWASP-recommended modern password hash. We use
the ``PasswordHasher`` interface with the library's default parameters,
which target ~50ms per hash on commodity hardware — slow enough to
resist offline attacks, fast enough to not block the event loop
noticeably on a single login.

The hasher's ``hash()`` returns a PHC-format string (``$argon2id$...``)
that encodes the algorithm, parameters, salt, and digest. ``verify()``
reads those parameters back from the stored hash, so we can rotate the
``PasswordHasher`` construction parameters without invalidating
existing hashes.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError


_hasher = PasswordHasher()


async def hash_password(plaintext: str) -> str:
    """Hash a plaintext password. Off-loads to a thread because argon2
    is intentionally CPU-expensive (~50ms) and we don't want to block
    the event loop on the login endpoint.

    Returns the full PHC string (e.g. ``$argon2id$v=19$m=65536,t=3,...``).
    """
    return await asyncio.to_thread(_hasher.hash, plaintext)


async def verify_password(plaintext: str, stored_hash: str | None) -> bool:
    """Constant-time-ish password check.

    Returns ``True`` on match, ``False`` on mismatch or malformed hash.
    Never raises on bad inputs — wrap argon2's exception taxonomy in
    a boolean so callers don't need to learn it. A ``None`` or empty
    ``stored_hash`` — e.g. an account provisioned without a password —
    returns ``False`` immediately without touching argon2, so a
    password-less row can never be authenticated.
    """
    if not stored_hash:
        return False

    def _verify() -> bool:
        try:
            return _hasher.verify(stored_hash, plaintext)
        except VerifyMismatchError:
            return False
        except InvalidHashError:
            return False

    return await asyncio.to_thread(_verify)
