"""Per-session serialization of lifecycle transitions.

``resume`` / ``pause`` / ``cancel`` each do a non-atomic
read-modify-write of the session row (a full-blob ``Storage.update``)
*and* an independent claim-lease mutation (``engine.upsert`` /
``engine.delete_lease``). Run concurrently against the same session they
lost-update each other: e.g. a ``resume`` racing a ``cancel`` on a
freshly-created (``auto_start=False``) session can land the row on
``RUNNING`` (resume's write wins) while the lease is dropped (cancel's
``delete_lease`` wins). The in-memory claim engine only ever claims rows
that have a lease (``claim_due`` walks ``self._leases``), so a
``RUNNING`` row with no lease is invisible to every worker forever — it
never converges to a terminal state. That is the T0432 stuck-RUNNING
fingerprint (``status=running, turn_no=0, cancel_requested=False``).

The fix is mutual exclusion: serialize the conflicting transitions per
session so whichever lands first is fully observed by the next. The API,
the in-memory claim engine, and the worker pool all run inside one
process (see :mod:`primer.api._app_lifespan`), so a process-local
``asyncio`` lock is a sufficient and complete serialization point for
this race. The Postgres/distributed lane does not share this engine and
gates claims through its query-driven eligibility instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

__all__ = ["KeyedLock", "session_lifecycle_lock"]


class KeyedLock:
    """Async locks keyed by string, with reference-counted cleanup.

    Coroutines sharing a ``key`` are serialized; distinct keys never
    contend. The lock object is created on first use and dropped once its
    last holder releases, so a long-lived process does not leak one lock
    per key ever seen.

    Single-event-loop only (the concurrency model inside a uvicorn
    worker). The dict bookkeeping runs synchronously between ``await``
    points, so no guard lock is needed; the per-key ``asyncio.Lock`` binds
    lazily to the running loop on first ``await`` and is gone before the
    next loop could touch it.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        # Bump the refcount BEFORE awaiting the lock so a holder that is
        # mid-release does not drop the shared lock object out from under a
        # coroutine that is about to wait on it. Both bookkeeping spans run
        # with no intervening await, so they are atomic on this loop.
        self._refs[key] = self._refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._refs[key] -= 1
            if self._refs[key] == 0:
                self._refs.pop(key, None)
                self._locks.pop(key, None)


# Process-wide lock guarding session lifecycle transitions. resume / pause /
# cancel acquire it on the session id before reading-and-writing the row +
# its claim lease, so two concurrent control-plane calls cannot interleave
# into a stuck RUNNING-without-lease orphan. See T0432 / the module docstring.
_SESSION_LIFECYCLE_LOCK = KeyedLock()


def session_lifecycle_lock() -> KeyedLock:
    """Return the process-wide session-lifecycle :class:`KeyedLock`."""
    return _SESSION_LIFECYCLE_LOCK
