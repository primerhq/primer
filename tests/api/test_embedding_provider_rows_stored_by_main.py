"""The embedding routes answer for rows main stored (review of #645, B1).

``EmbeddingProvider.config`` is a union and, before #645, pydantic picked the member that fitted the dict best whatever ``provider`` said. The provider-keyed validator
refuses three of the shapes that left in storage, and ``Storage._from_row`` validates uncaught: with ONE such row ``GET /v1/embedding_providers`` (the whole list) and get, put
and delete by id all answered 500, so the operator could not even delete it. Migration 8 repairs the rows at boot; this runs the routes over a REAL SQLite storage (the in-memory
fakes never decode a row) holding main's JSON, before and after.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.storage.test_m008_embedding_provider_repair import BROKEN, HEALTHY, KEY, TOKEN, _seed


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "embedding-rows.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def client(sp: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:  # noqa: BLE001 - auth may be off in the test app
            pass
        yield c


def _ids(body) -> set[str]:
    items = body["items"] if isinstance(body, dict) else body
    return {item["id"] for item in items}


async def _migrate(sp: SqliteStorageProvider) -> None:
    from primer.storage.migrations import run_migrations

    await sp.set_schema_version(7)
    await run_migrations(sp, is_fresh_install=False)


# ---- the bug, end to end (a guard that the seeding is main's JSON) ------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_the_migration_one_such_row_takes_the_whole_list_down(sp: SqliteStorageProvider, client: httpx.AsyncClient) -> None:
    await _seed(sp)

    assert (await client.get("/v1/embedding_providers")).status_code == 500
    assert (await client.get("/v1/embedding_providers/openai-no-url")).status_code == 500
    assert (await client.delete("/v1/embedding_providers/openai-no-url")).status_code == 500, "the operator cannot even delete it"


# ---- after migration 8 -------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_the_migration_the_list_answers_200_with_every_row(sp: SqliteStorageProvider, client: httpx.AsyncClient) -> None:
    await _seed(sp)
    await _migrate(sp)

    r = await client.get("/v1/embedding_providers")

    assert r.status_code == 200, r.text
    assert _ids(r.json()) == set(BROKEN) | set(HEALTHY)
    assert KEY not in r.text and TOKEN not in r.text, "the keys stay masked on the wire"


@pytest.mark.asyncio
@pytest.mark.parametrize("row_id", sorted(set(BROKEN) | set(HEALTHY)))
async def test_after_the_migration_each_row_can_be_read_by_id(sp: SqliteStorageProvider, client: httpx.AsyncClient, row_id: str) -> None:
    await _seed(sp)
    await _migrate(sp)

    r = await client.get(f"/v1/embedding_providers/{row_id}")

    assert r.status_code == 200, r.text
    assert r.json()["id"] == row_id


@pytest.mark.asyncio
async def test_after_the_migration_a_repaired_row_can_be_deleted(sp: SqliteStorageProvider, client: httpx.AsyncClient) -> None:
    await _seed(sp)
    await _migrate(sp)

    deleted = await client.delete("/v1/embedding_providers/openai-no-url")

    assert deleted.status_code in (200, 204), deleted.text
    assert (await client.get("/v1/embedding_providers/openai-no-url")).status_code == 404
    assert "openai-no-url" not in _ids((await client.get("/v1/embedding_providers")).json())


@pytest.mark.asyncio
async def test_after_the_migration_a_repaired_row_can_be_given_its_real_url(sp: SqliteStorageProvider, client: httpx.AsyncClient) -> None:
    await _seed(sp)
    await _migrate(sp)
    row = (await client.get("/v1/embedding_providers/openai-no-url")).json()
    row["config"] = {"url": "http://emb.local:1234/v1", "api_key": KEY}

    r = await client.put("/v1/embedding_providers/openai-no-url", json=row)

    assert r.status_code == 200, r.text
    assert (await client.get("/v1/embedding_providers/openai-no-url")).json()["config"]["url"].startswith("http://emb.local:1234/v1")
