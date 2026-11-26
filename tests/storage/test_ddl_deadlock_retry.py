"""Unit tests for the bounded DeadlockDetectedError retry in primer.storage._ddl
(ruling N3-1): an idempotent DDL unit that deadlocks is re-run in a fresh
transaction; after the bound the original error is raised. Round 3b: the
inline UNIQUE hot-index build (PostgresStorage._build_unique_indexes) is
routed through the same helper. No database is needed: the connection is a
stand-in that replays scripted outcomes for ``execute`` (the only contract
the helper uses; see FIX-BRIEF rule 6).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from primer.model.api_token import ApiToken
from primer.model.except_ import ProviderError
from primer.storage._ddl import (
    execute_create_idempotent,
    execute_idempotent_ddl,
)
from primer.storage.postgres import PostgresStorage


class _ScriptedConn:
    """asyncpg connection stand-in: ``execute`` replays a scripted outcome
    (a string is returned, an exception instance is raised) and counts calls."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def execute(self, sql: str) -> str:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_deadlocked_ddl_unit_is_retried_until_it_succeeds() -> None:
    conn = _ScriptedConn([asyncpg.DeadlockDetectedError(), asyncpg.DeadlockDetectedError(), "OK"])
    await execute_idempotent_ddl(lambda: conn.execute("CREATE INDEX IF NOT EXISTS i (x)"))
    assert conn.calls == 3


@pytest.mark.asyncio
async def test_deadlocked_ddl_unit_gives_up_after_the_bound_with_the_original_error() -> None:
    original = asyncpg.DeadlockDetectedError()
    conn = _ScriptedConn([original] * 8)
    with pytest.raises(asyncpg.DeadlockDetectedError) as exc_info:
        await execute_idempotent_ddl(lambda: conn.execute("CREATE INDEX IF NOT EXISTS i (x)"))
    assert conn.calls == 5
    assert exc_info.value is original


@pytest.mark.asyncio
async def test_non_deadlock_ddl_errors_are_not_retried() -> None:
    boom = asyncpg.UniqueViolationError()
    conn = _ScriptedConn([boom, "OK"])
    with pytest.raises(asyncpg.UniqueViolationError):
        await execute_idempotent_ddl(lambda: conn.execute("CREATE INDEX IF NOT EXISTS i (x)"))
    assert conn.calls == 1


@pytest.mark.asyncio
async def test_create_idempotent_retries_deadlock_and_still_swallows_peer_races() -> None:
    conn = _ScriptedConn([asyncpg.DeadlockDetectedError(), asyncpg.DuplicateObjectError(), "OK"])
    await execute_create_idempotent(conn, "CREATE TABLE IF NOT EXISTS t (id text)")
    # Attempt 1 deadlocks and is retried; attempt 2 is a peer cold-start race
    # and is swallowed: the object counts as created.
    assert conn.calls == 2


# --- Round 3b: the inline UNIQUE hot-index build goes through the helper ----


class _BuildConn:
    """asyncpg connection stand-in for the build pass: ``execute`` replays a
    scripted outcome (accepting the ``timeout=`` kwarg the build passes) and
    ``fetch`` replays the scripted ``_index_state`` rows."""

    def __init__(self, execute_outcomes: list[Any], fetch_results: list[Any]) -> None:
        self._execute_outcomes = execute_outcomes
        self._fetch_results = fetch_results
        self.executed: list[str] = []

    async def execute(self, sql: str, timeout: float | None = None) -> str:
        self.executed.append(sql)
        outcome = self._execute_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._fetch_results.pop(0) if len(self._fetch_results) > 1 else self._fetch_results[0]


class _Acquired:
    def __init__(self, conn: _BuildConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _BuildConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    """asyncpg pool stand-in: every acquire yields a FRESH scripted connection,
    the contract under test (a deadlock must not pin a connection across the
    helper's backoff sleep)."""

    def __init__(self, execute_outcomes: list[Any], fetch_results: list[Any]) -> None:
        self._execute_outcomes = execute_outcomes
        self._fetch_results = fetch_results
        self.conns: list[_BuildConn] = []
        self.acquires = 0

    def acquire(self, timeout: float | None = None) -> _Acquired:
        self.acquires += 1
        conn = _BuildConn(self._execute_outcomes, self._fetch_results)
        self.conns.append(conn)
        return _Acquired(conn)


def _storage_with_pool(pool: _FakePool) -> PostgresStorage:
    provider = SimpleNamespace(schema="public", acquire_timeout=5.0, pool=pool)
    return PostgresStorage(provider, ApiToken)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_inline_unique_index_build_retries_a_deadlock_in_a_fresh_connection() -> None:
    # Attempt 1 deadlocks against a peer's CONCURRENTLY build on the same
    # table; attempt 2, on a fresh connection, succeeds.
    pool = _FakePool([asyncpg.DeadlockDetectedError(), "OK"], [])
    storage = _storage_with_pool(pool)
    await storage._build_unique_indexes({})
    assert pool.acquires == 2, "the retry must take a fresh connection (none held across the backoff)"
    built = pool.conns[0].executed + pool.conns[1].executed
    assert built and all(
        sql.startswith('CREATE UNIQUE INDEX IF NOT EXISTS "apitoken_token_hash_uniq"') for sql in built
    ), f"the retried unit must re-run the same idempotent build: {built}"


@pytest.mark.asyncio
async def test_inline_unique_index_build_does_not_retry_a_unique_violation() -> None:
    # Round 1 semantics: a UniqueViolation with the index ABSENT is duplicate
    # data, not a transient race. The index is strict (not in
    # _DUP_TOLERANT_UNIQUE_INDEX_NAMES): ProviderError, and it is NOT retried
    # as if it were a deadlock.
    pool = _FakePool([asyncpg.UniqueViolationError()], [[]])  # fetch: the pg_index re-check, index absent
    storage = _storage_with_pool(pool)
    with pytest.raises(ProviderError, match="duplicate rows already exist"):
        await storage._build_unique_indexes({})
    assert pool.acquires == 1, "a data duplicate must not be retried as if it were a deadlock"
