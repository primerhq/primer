"""Advisory write-lock table for the workspace runtime.

Serializes mutating operations so concurrent writers to the same file
(Tier A) or the same directory subtree (Tier B, exec) never interleave.
All locks are ``asyncio.Lock`` so acquisition parks the coroutine and
never blocks the single runtime event loop.

Self-contained: this module must not import ``primer.*`` (the runtime
package is a standalone image). The local backend keeps an identical
copy at ``primer/workspace/_locks.py``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager


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


__all__ = ["WorkspaceLockTable"]
