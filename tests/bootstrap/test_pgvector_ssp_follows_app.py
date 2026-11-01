"""GI-1: the reserved pgvector SSP is seeded from ALL of the app's Postgres settings (schema and pool too) and follows them afterwards.

Two installs sharing one database (one schema each) must not share vector tables; a rotated app password must reach the reserved row; a row an operator
pointed somewhere else, and the operator's tuning, are left alone; the schema moves only while the old schema's collection catalogue is empty.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from primer.bootstrap.defaults import RESERVED_PGVECTOR_SSP
from primer.bootstrap.runner import BootstrapRunner
from primer.model.provider import CrossEncoderProvider, EmbeddingProvider, SemanticSearchProvider, StorageProviderType
from primer.model.providers.storage import PoolConfig, PostgresConfig, SqliteConfig, StorageProviderConfig
from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_io(monkeypatch):
    async def _ok(config: Any) -> None:
        return None

    monkeypatch.setattr("primer.vector.pgvector.probe_postgres_reachable", _ok)
    monkeypatch.setattr("primer.bootstrap.runner.has_extra", lambda extra: False)
    monkeypatch.setattr("primer.vector.pgvector.catalogue_has_collections", _catalogue(False))


def _catalogue(has_collections: bool):
    async def check(config: Any) -> bool:
        return has_collections

    return check


def _db(*, schema: str = "public", password: str = "pw", host: str = "db", pool: PoolConfig | None = None) -> StorageProviderConfig:
    return StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(hostname=host, port=5432, database="shared", username="u", password=password, db_schema=schema,
                              pool=pool or PoolConfig()),
    )


async def _provider(tmp_path: Path, name: str = "p.sqlite") -> SqliteStorageProvider:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / name))
    await sp.initialize()
    return sp


async def _run(sp: SqliteStorageProvider, tmp_path: Path, db: StorageProviderConfig):
    runner = BootstrapRunner(
        storage=sp, embedder_storage=sp.get_storage(EmbeddingProvider), ssp_storage=sp.get_storage(SemanticSearchProvider),
        cross_encoder_storage=sp.get_storage(CrossEncoderProvider), workspace_provider_storage=sp.get_storage(WorkspaceProvider),
        workspace_template_storage=sp.get_storage(WorkspaceTemplate), root_dir=tmp_path / "root", db_config=db,
    )
    result = await runner.run()
    assert not result.errors, result.errors
    return await sp.get_storage(SemanticSearchProvider).get(RESERVED_PGVECTOR_SSP)


async def test_each_install_gets_its_own_schema_and_the_app_pool(tmp_path):
    pool = PoolConfig(min_size=2, max_size=7)
    rows = {}
    for schema in ("tenant_a", "tenant_b"):
        sp = await _provider(tmp_path, f"{schema}.sqlite")
        try:
            rows[schema] = await _run(sp, tmp_path, _db(schema=schema, pool=pool))
        finally:
            await sp.aclose()
    assert rows["tenant_a"].config.db_schema == "tenant_a" and rows["tenant_b"].config.db_schema == "tenant_b"
    assert rows["tenant_a"].config.pool == pool


async def test_a_public_schema_install_keeps_its_reserved_row_unchanged(tmp_path):
    sp = await _provider(tmp_path)
    try:
        first = await _run(sp, tmp_path, _db())
        second = await _run(sp, tmp_path, _db())
        assert first.config.db_schema == "public" and second.config == first.config and second.access.public
    finally:
        await sp.aclose()


async def test_a_rotated_app_password_reaches_the_row_and_the_operators_tuning_survives(tmp_path):
    sp = await _provider(tmp_path)
    try:
        await _run(sp, tmp_path, _db(password="old"))
        store = sp.get_storage(SemanticSearchProvider)
        row = await store.get(RESERVED_PGVECTOR_SSP)
        await store.update(row.model_copy(update={"config": row.config.model_copy(update={"hnsw_m": 48})}))

        moved = await _run(sp, tmp_path, _db(password="new"))
        assert moved.config.password.get_secret_value() == "new"
        assert moved.config.hnsw_m == 48
    finally:
        await sp.aclose()


async def test_a_row_pointed_at_another_server_is_left_alone(tmp_path):
    sp = await _provider(tmp_path)
    try:
        await _run(sp, tmp_path, _db(host="db"))
        store = sp.get_storage(SemanticSearchProvider)
        row = await store.get(RESERVED_PGVECTOR_SSP)
        await store.update(row.model_copy(update={"config": row.config.model_copy(update={"hostname": "vectors.internal"})}))

        kept = await _run(sp, tmp_path, _db(host="db", password="new"))
        assert kept.config.hostname == "vectors.internal" and kept.config.password.get_secret_value() == "pw"
    finally:
        await sp.aclose()


async def test_the_schema_moves_off_public_only_while_the_catalogue_is_empty(tmp_path, monkeypatch, caplog):
    sp = await _provider(tmp_path)
    try:
        await _run(sp, tmp_path, _db(schema="public"))                       # an install seeded before the fix: public
        monkeypatch.setattr("primer.vector.pgvector.catalogue_has_collections", _catalogue(True))
        with caplog.at_level(logging.WARNING):
            kept = await _run(sp, tmp_path, _db(schema="tenant_a"))
        assert kept.config.db_schema == "public"                              # vector tables live there: never orphaned
        assert "public" in caplog.text and "tenant_a" in caplog.text

        monkeypatch.setattr("primer.vector.pgvector.catalogue_has_collections", _catalogue(False))
        moved = await _run(sp, tmp_path, _db(schema="tenant_a"))
        assert moved.config.db_schema == "tenant_a"
    finally:
        await sp.aclose()


async def test_an_unreadable_catalogue_keeps_the_schema(tmp_path, monkeypatch):
    sp = await _provider(tmp_path)
    try:
        await _run(sp, tmp_path, _db(schema="public"))

        async def boom(config: Any) -> bool:
            raise OSError("no route to host")

        monkeypatch.setattr("primer.vector.pgvector.catalogue_has_collections", boom)
        assert (await _run(sp, tmp_path, _db(schema="tenant_a"))).config.db_schema == "public"
    finally:
        await sp.aclose()
