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
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError


_hasher = PasswordHasher()

#: How many password hashes or verifications run at once, and how many more may wait for one (review of gaps-security, GS-15). An argon2id hash costs
#: 64 MiB and about 50 ms of CPU: unbounded, an anonymous login flood with fresh usernames ran hundreds at once and queued the rest on the shared
#: default thread pool, starving every other ``to_thread`` user of the process. The hash PARAMETERS are untouched.
HASH_CONCURRENCY = 4
HASH_MAX_WAITING = 16

# Hashing has its own small pool: it never competes with file, git and sqlite helpers for the default executor.
_POOL = ThreadPoolExecutor(max_workers=HASH_CONCURRENCY, thread_name_prefix="argon2")


class HashBusy(Exception):
    """Every hash slot is taken and the wait queue is full: the request is refused (503) before anything is hashed."""


class HashGate:
    """``HASH_CONCURRENCY`` slots and a bounded wait queue for the password work of the sign-in routes. One per app (it holds an event-loop bound
    semaphore). ``slot()`` waits for a slot, or raises :class:`HashBusy` at once when ``HASH_MAX_WAITING`` requests already wait."""

    def __init__(self, concurrency: int = HASH_CONCURRENCY, max_waiting: int = HASH_MAX_WAITING) -> None:
        self._slots = asyncio.Semaphore(concurrency)
        self._max_waiting = max_waiting
        self._waiting = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        # No await between the check and the increment: on one event loop it is atomic.
        if self._slots.locked() and self._waiting >= self._max_waiting:
            raise HashBusy
        self._waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._slots.release()


async def hash_password(plaintext: str) -> str:
    """Hash a plaintext password. Off-loads to the hashing pool because argon2
    is intentionally CPU-expensive (~50ms) and we don't want to block
    the event loop on the login endpoint.

    Returns the full PHC string (e.g. ``$argon2id$v=19$m=65536,t=3,...``).
    """
    return await asyncio.get_running_loop().run_in_executor(_POOL, _hasher.hash, plaintext)


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

    return await asyncio.get_running_loop().run_in_executor(_POOL, _verify)
