"""``EntitySeeder`` (the live-Postgres claim tests' entity rows): what seeding the same id twice does.

``seed(..., data=...)`` used to be ``ON CONFLICT DO NOTHING``, so a test that seeded an id a previous test (or an
earlier call) had already created silently kept the OLD data and asserted against the wrong row. Given ``data`` the
row ends up with that data; omitting it leaves an existing row alone.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.claim._entity_seed import EntitySeeder
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url

_needs_pg = needs_postgres("entity seeder tests")


def _config(url: str) -> PostgresConfig:
    p = urlparse(url)
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=parse_qs(p.query).get("schema", ["public"])[0],
        pool=PoolConfig(min_size=1, max_size=2),
    )


@pytest_asyncio.fixture
async def pg_storage() -> AsyncIterator[PostgresStorageProvider]:
    sp = PostgresStorageProvider(_config(require_postgres_url("entity seeder tests")))
    await sp.initialize()
    try:
        yield sp
    finally:
        await sp.aclose()


@pytest_asyncio.fixture
async def seeder(pg_storage: PostgresStorageProvider) -> AsyncIterator[EntitySeeder]:
    seeder = EntitySeeder(pg_storage)
    try:
        yield seeder
    finally:
        await seeder.cleanup()


async def _data(pg_storage, table: str, entity_id: str):
    async with pg_storage.pool.acquire() as conn:
        raw = await conn.fetchval(f'SELECT data FROM "{pg_storage.schema}"."{table}" WHERE id = $1', entity_id)
    return None if raw is None else (json.loads(raw) if isinstance(raw, str) else dict(raw))


@_needs_pg
@pytest.mark.asyncio
async def test_seeding_an_existing_id_with_data_replaces_its_data(pg_storage, seeder):
    await seeder.seed("toolcalltask", ["seed-a", "seed-b"], data={"state": "queued"})
    await seeder.seed("toolcalltask", ["seed-a"], data={"state": "done"})

    assert await _data(pg_storage, "toolcalltask", "seed-a") == {"state": "done"}
    assert await _data(pg_storage, "toolcalltask", "seed-b") == {"state": "queued"}, "only the named id is touched"


@_needs_pg
@pytest.mark.asyncio
async def test_seeding_an_existing_id_without_data_leaves_it_alone(pg_storage, seeder):
    await seeder.seed("toolcalltask", ["seed-a"], data={"state": "failed"})
    await seeder.seed("toolcalltask", ["seed-a", "seed-fresh"])

    assert await _data(pg_storage, "toolcalltask", "seed-a") == {"state": "failed"}, "seeding without data wiped the row"
    assert await _data(pg_storage, "toolcalltask", "seed-fresh") == {}
