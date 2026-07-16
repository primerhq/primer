"""Advisory write-lock table for the LOCAL workspace backend.

Identical shared API to runtime/primer_runtime/locks.py (kept as a
deliberate duplicate so primer_runtime stays free of primer.* imports),
plus a cross-process flock helper for multi-process local deployments.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows dev
    fcntl = None


class WorkspaceLockTable:
    """Per-path (Tier A) and per-scope (Tier B) advisory write locks.

    Fixed global acquisition order prevents deadlock: a writer that needs
    both a scope lock and a path lock always takes the scope lock FIRST
    (``hold_write``); multi-path acquisition (``hold_paths``) sorts by
    string. Scope locks and path locks live in separate maps, so an
    identical string key yields two distinct locks (``scope[D]`` != ``path[D]``).

    Accepted residual (v1): ``_path_locks`` / ``_scope_locks`` are never
    pruned - one permanent ``asyncio.Lock`` per distinct path/workdir ever
    touched, in a long-lived per-workspace runtime. Accepted for v1 (a
    ``Lock`` is ~tens of bytes; a workspace touches a bounded set of paths
    in practice). A refcounted prune is deliberately out of scope.
    """

    def __init__(self) -> None:
        self._path_locks: dict[str, asyncio.Lock] = {}
        self._scope_locks: dict[str, asyncio.Lock] = {}
        # Guards lazy creation of the per-key locks above so two coroutines
        # racing on a brand-new key observe the SAME Lock object.
        self._table_lock = asyncio.Lock()

    async def _get(self, mapping: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
        async with self._table_lock:
            lock = mapping.get(key)
            if lock is None:
                lock = asyncio.Lock()
                mapping[key] = lock
            return lock

    @asynccontextmanager
    async def hold_path(self, path: str) -> AsyncIterator[None]:
        """Hold the Tier-A exclusive lock for a single resolved path."""
        lock = await self._get(self._path_locks, path)
        async with lock:
            yield

    @asynccontextmanager
    async def hold_scope(self, scope: str) -> AsyncIterator[None]:
        """Hold the Tier-B exclusive lock for a resolved scope (workdir/root)."""
        lock = await self._get(self._scope_locks, scope)
        async with lock:
            yield

    @asynccontextmanager
    async def hold_write(self, scope: str, path: str) -> AsyncIterator[None]:
        """Tier-A tool write: acquire scope lock THEN path lock (fixed order)."""
        scope_lock = await self._get(self._scope_locks, scope)
        path_lock = await self._get(self._path_locks, path)
        async with scope_lock:
            async with path_lock:
                yield

    @asynccontextmanager
    async def hold_paths(self, paths: list[str]) -> AsyncIterator[None]:
        """Tier-B exec with declared writes: sorted path locks (deadlock-free)."""
        ordered = sorted(set(paths))
        locks = [await self._get(self._path_locks, p) for p in ordered]
        async with AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            yield

    @asynccontextmanager
    async def hold_multi(
        self, scopes: list[str], paths: list[str]
    ) -> AsyncIterator[None]:
        """Multi-scope Tier-A writer (e.g. a move across two dirs).

        Acquires ALL scope locks (sorted) THEN all path locks (sorted), in
        the same global order as ``hold_write`` (scopes before paths) so it
        can never deadlock against a single-target ``hold_write`` or a
        Tier-B ``hold_scope``/``hold_paths``.
        """
        scope_keys = sorted(set(scopes))
        path_keys = sorted(set(paths))
        scope_locks = [await self._get(self._scope_locks, s) for s in scope_keys]
        path_locks = [await self._get(self._path_locks, p) for p in path_keys]
        async with AsyncExitStack() as stack:
            for lock in scope_locks:
                await stack.enter_async_context(lock)
            for lock in path_locks:
                await stack.enter_async_context(lock)
            yield

    @asynccontextmanager
    async def hold_flock(self, lock_dir: Path, key: str) -> AsyncIterator[None]:
        """Cross-process exclusive lock on a file under <lock_dir>.

        Runs flock(LOCK_EX) via asyncio.to_thread so it never blocks the
        event loop. Degrades to a no-op (in-process asyncio.Lock only,
        already held by the caller) with a WARNING if the lock file cannot
        be created (spec section 7) - never hard-fail a write.
        """
        fd = None
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(key.encode()).hexdigest()
            lock_path = lock_dir / f"{digest}.lock"
            fd = await asyncio.to_thread(_open_and_lock, lock_path)
        except OSError as exc:
            log.warning("flock unavailable (%s); using in-process lock only", exc)
            yield
            return
        try:
            yield
        finally:
            if fd is not None:
                await asyncio.to_thread(_unlock_and_close, fd)


def _open_and_lock(lock_path: Path) -> int:
    import os
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock_and_close(fd: int) -> None:
    import os
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = ["WorkspaceLockTable"]
