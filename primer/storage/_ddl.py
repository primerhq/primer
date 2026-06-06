"""Shared helper for tolerating concurrent DDL-creation races on Postgres.

When several primer processes (API + workers) boot against a fresh schema
at the same time, two concurrent ``CREATE TABLE IF NOT EXISTS`` (or
``CREATE INDEX IF NOT EXISTS``) statements can both pass the existence
check and then race on the underlying system catalogs -- ``pg_type`` for
the table's implicit composite row type, ``pg_class`` for the relation
name. One statement then fails with a unique violation (or a
duplicate-table / duplicate-object error) even though ``IF NOT EXISTS``
was specified. ``IF NOT EXISTS`` guards against a *committed* duplicate,
not against a concurrent in-flight creation.

The losing process can safely treat the object as already created: the
winner created it (and, where they share a transaction, its indexes) and
will commit momentarily. Catch :data:`CONCURRENT_CREATE_RACE` around DDL
and continue.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

CONCURRENT_CREATE_RACE: tuple[type[Exception], ...] = (
    asyncpg.DuplicateTableError,
    asyncpg.DuplicateObjectError,
    asyncpg.UniqueViolationError,
)


async def execute_create_idempotent(conn: Any, sql: str) -> None:
    """Run a ``CREATE TABLE/INDEX IF NOT EXISTS`` tolerating the cold-start race.

    Each call must be its own autocommit statement (no enclosing
    ``conn.transaction()``), so a race on one statement does not abort
    sibling statements. On :data:`CONCURRENT_CREATE_RACE` the object was
    created by a peer process and is treated as already present.
    """
    try:
        await conn.execute(sql)
    except CONCURRENT_CREATE_RACE as exc:
        logger.debug(
            "DDL create race (%s); object created by a peer process",
            type(exc).__name__,
        )


__all__ = ["CONCURRENT_CREATE_RACE", "execute_create_idempotent"]
