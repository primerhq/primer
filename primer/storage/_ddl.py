"""Shared helpers for tolerating concurrent DDL-creation races on Postgres.

When several primer processes (API + workers) boot against a fresh schema
at the same time, two concurrent ``CREATE TABLE IF NOT EXISTS`` (or
``CREATE INDEX IF NOT EXISTS``) statements can both pass the existence
check and then race on the underlying system catalogs -- ``pg_type`` for
the table's implicit composite row type, ``pg_class`` for the relation
name. One statement then fails with a unique violation (or a
duplicate-table / duplicate-object error) even though ``IF NOT EXISTS``
was specified. ``IF NOT EXISTS`` guards against a *committed* duplicate,
not against a concurrent in-flight creation. The losing process can
safely treat the object as already created: the winner created it (and,
where they share a transaction, its indexes) and will commit
momentarily. Catch :data:`CONCURRENT_CREATE_RACE` around DDL and
continue.

Idempotent DDL can also deadlock against a peer's background
``CREATE INDEX CONCURRENTLY`` on the same table: the CIC build holds
SHARE UPDATE EXCLUSIVE on the table while its final wait blocks on every
older virtual transaction that still advertises a snapshot, while this
statement's own table-lock request queues behind the CIC's lock. Postgres
kills one side with ``DeadlockDetectedError``. The killed DDL unit can be
re-run safely -- it is idempotent and its transaction is gone -- so
:func:`execute_idempotent_ddl` retries the whole unit in a fresh
transaction, bounded, with short jittered backoff.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

CONCURRENT_CREATE_RACE: tuple[type[Exception], ...] = (
    asyncpg.DuplicateTableError,
    asyncpg.DuplicateObjectError,
    asyncpg.UniqueViolationError,
)

# Bounded deadlock retry: the deadlock clears as soon as the peer's
# CONCURRENTLY build finishes, so a short jittered backoff covers it.
_DEADLOCK_ATTEMPTS = 5


def _deadlock_backoff(attempt: int) -> float:
    """Seconds before retry *attempt* (1-based): short, growing, jittered."""
    return random.uniform(0.01, 0.1) * attempt


async def execute_idempotent_ddl(unit: Callable[[], Awaitable[None]], *, attempts: int = _DEADLOCK_ATTEMPTS) -> None:
    """Run the idempotent DDL unit, retrying on DeadlockDetectedError in a fresh transaction.

    *unit* is a zero-arg async callable that performs the whole unit in one
    transaction: a single autocommit ``CREATE ... IF NOT EXISTS`` statement,
    or a ``conn.transaction()`` block (e.g. a table and its indexes). A
    deadlock aborts that transaction, so each retry re-runs the unit as a
    whole; the unit supplies its own fresh connection and transaction per
    attempt. After *attempts* the original deadlock error is raised.
    """
    for attempt in range(attempts):
        try:
            await unit()
            return
        except asyncpg.DeadlockDetectedError:
            if attempt + 1 >= attempts:
                raise
            await asyncio.sleep(_deadlock_backoff(attempt + 1))


async def execute_create_idempotent(conn: Any, sql: str) -> None:
    """Run a ``CREATE TABLE/INDEX IF NOT EXISTS`` tolerating the cold-start race.

    Each call must be its own autocommit statement (no enclosing
    ``conn.transaction()``), so a race or deadlock on one statement does not
    abort sibling statements. On :data:`CONCURRENT_CREATE_RACE` the object
    was created by a peer process and is treated as already present. On
    ``DeadlockDetectedError`` (a peer's ``CREATE INDEX CONCURRENTLY`` holds
    the table lock while waiting on this statement's transaction) the
    statement is retried in a fresh implicit transaction via
    :func:`execute_idempotent_ddl`.
    """
    async def _unit() -> None:
        try:
            await conn.execute(sql)
        except CONCURRENT_CREATE_RACE as exc:
            logger.debug(
                "DDL create race (%s); object created by a peer process",
                type(exc).__name__,
            )

    await execute_idempotent_ddl(_unit)


__all__ = ["CONCURRENT_CREATE_RACE", "execute_create_idempotent", "execute_idempotent_ddl"]
