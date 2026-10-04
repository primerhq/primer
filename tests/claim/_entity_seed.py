"""Seed entity rows for the live-Postgres claim-engine tests.

``PostgresClaimEngine.claim_due`` INNER JOINs each registered adapter's
entity table (``JOIN <entity> e ON e.id = l.entity_id``, primer/claim/sql.py),
so a lease whose entity row does not exist is never claimable. A synthetic
adapter whose eligibility SQL "only touches the lease alias" does NOT avoid
that join: the tests that assumed it did claimed nothing.

The table shape below is the one ``PostgresClaimEngine._ensure_entity_tables``
creates, so seeding works before the engine's first claim.
"""

from __future__ import annotations

from collections.abc import Iterable


class EntitySeeder:
    """Inserts minimal ``(id, data)`` rows and removes them again on cleanup."""

    def __init__(self, storage) -> None:
        self._storage = storage
        self._seeded: list[tuple[str, str]] = []

    def _qualified(self, table: str) -> str:
        schema = self._storage.schema
        return f'"{schema}"."{table}"' if schema else f'"{table}"'

    async def seed(self, table: str, entity_ids: Iterable[str]) -> None:
        qualified = self._qualified(table)
        async with self._storage.pool.acquire() as conn:
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {qualified} ("
                "id text PRIMARY KEY, "
                "data jsonb NOT NULL, "
                "created_at timestamptz NOT NULL DEFAULT now(), "
                "updated_at timestamptz NOT NULL DEFAULT now()"
                ")"
            )
            for entity_id in entity_ids:
                await conn.execute(
                    f"INSERT INTO {qualified} (id, data) "
                    "VALUES ($1, '{}'::jsonb) ON CONFLICT (id) DO NOTHING",
                    entity_id,
                )
                self._seeded.append((table, entity_id))

    async def cleanup(self) -> None:
        async with self._storage.pool.acquire() as conn:
            for table, entity_id in self._seeded:
                await conn.execute(
                    f"DELETE FROM {self._qualified(table)} WHERE id = $1",
                    entity_id,
                )
        self._seeded.clear()
