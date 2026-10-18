"""The system default_workspace_id (pivot P2 Task 1).

Where "which workspace is the default (prime)" is persisted. The ensure pass
records it (a later slice); this task only makes it storable so the delete
guard, ``is_default`` on workspace rows and the console have one place to read.

Exercised against sqlite because that is the backend a test can build in a
tmpdir. The round trip in ``test_system_state.py`` runs against both when the
Postgres gate is set, and the one Postgres-gated test here covers what that
round trip cannot: the ``ADD COLUMN IF NOT EXISTS`` upgrade of an old table.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import aiosqlite
import asyncpg
import pytest

from primer.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url


async def test_a_database_predating_the_column_is_upgraded(tmp_path):
    """The ALTER guard, which is what lets existing installs upgrade without a
    migration step. The old table is built exactly as the ``default_agent_id``
    test builds it, plus the ``default_agent_id`` column that shipped after it."""
    path = tmp_path / "old.sqlite"
    async with aiosqlite.connect(str(path)) as conn:
        await conn.execute(
            "CREATE TABLE system_state ("
            "  id                     TEXT PRIMARY KEY DEFAULT 'singleton',"
            "  bootstrap_completed_at TEXT,"
            "  schema_version         INTEGER NOT NULL DEFAULT 1,"
            "  last_migration_at      TEXT,"
            "  session_secret         TEXT,"
            "  sso_jit_enabled        INTEGER NOT NULL DEFAULT 0,"
            "  sso_default_access     TEXT,"
            "  default_agent_id       TEXT"
            ")"
        )
        await conn.execute(
            "INSERT INTO system_state (id, default_agent_id) VALUES ('singleton', 'operator')"
        )
        await conn.commit()

    provider = SqliteStorageProvider(SqliteConfig(path=path))
    await provider.initialize()
    try:
        state = await provider.get_system_state()
        assert state.default_workspace_id is None
        assert state.default_agent_id == "operator", "the upgrade must not disturb the neighbouring column"
        await provider.set_default_workspace_id("primer")
        assert (await provider.get_system_state()).default_workspace_id == "primer"
    finally:
        await provider.aclose()


async def test_reopening_an_upgraded_database_keeps_the_recorded_id(tmp_path):
    """The ALTER guard only adds a MISSING column: a second open must not reset a recorded id."""
    path = tmp_path / "again.sqlite"
    first = SqliteStorageProvider(SqliteConfig(path=path))
    await first.initialize()
    try:
        await first.set_default_workspace_id("primer")
    finally:
        await first.aclose()

    second = SqliteStorageProvider(SqliteConfig(path=path))
    await second.initialize()
    try:
        assert (await second.get_system_state()).default_workspace_id == "primer"
    finally:
        await second.aclose()


async def test_the_setter_rolls_back_with_an_enclosing_transaction(tmp_path):
    """The SQLite setter goes through ``_write_guard()``: inside ``transaction()``
    it must ride that transaction's commit, so a rollback undoes it. Same shape as
    ``test_write_guard_coverage.py::test_system_state_setter_inside_a_rolled_back_transaction_is_undone``."""
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "txn.sqlite"))
    await provider.initialize()
    try:
        with pytest.raises(RuntimeError):
            async with provider.transaction():
                await provider.set_default_workspace_id("doomed")
                raise RuntimeError("roll back after the setter")
        assert (await provider.get_system_state()).default_workspace_id is None

        await provider.set_default_workspace_id("after")
        assert (await provider.get_system_state()).default_workspace_id == "after"
    finally:
        await provider.aclose()


async def test_setting_the_id_leaves_the_default_agent_alone(tmp_path):
    """The two settings are separate columns: writing one must not touch the other."""
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "cols.sqlite"))
    await provider.initialize()
    try:
        await provider.set_default_agent_id("operator")
        await provider.set_default_workspace_id("prime")
        state = await provider.get_system_state()
        assert (state.default_agent_id, state.default_workspace_id) == ("operator", "prime")
        await provider.set_default_workspace_id(None)
        state = await provider.get_system_state()
        assert (state.default_agent_id, state.default_workspace_id) == ("operator", None)
    finally:
        await provider.aclose()


# ---- Postgres (gated): the ADD COLUMN IF NOT EXISTS upgrade ---------------------------------------------------------------------------


def _config(url: str, schema: str) -> PostgresConfig:
    parsed = urlparse(url)
    return PostgresConfig(
        hostname=parsed.hostname or "localhost",
        port=explicit_port(parsed),
        username=parsed.username or "postgres",
        password=parsed.password or "",  # type: ignore[arg-type]
        database=(parsed.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=2),
    )


async def _drop_schema(cfg: PostgresConfig, schema: str) -> None:
    conn = await asyncpg.connect(
        host=cfg.hostname, port=cfg.port, user=cfg.username, password=cfg.password.get_secret_value(), database=cfg.database,
    )
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()


@needs_postgres("the Postgres system_state upgrade test")
async def test_a_postgres_database_predating_the_column_is_upgraded():
    """A scratch schema gets the current table; dropping the new column from it makes it the table an old install has. A provider opened on
    that schema must add the column back, leave the neighbouring column alone, and keep a recorded id across a further open."""
    cfg = _config(require_postgres_url("the Postgres system_state upgrade test"), f"dwid_{uuid.uuid4().hex[:12]}")
    try:
        first = PostgresStorageProvider(cfg)
        await first.initialize()
        try:
            await first.set_default_agent_id("operator")
            async with first.pool.acquire() as conn:
                await conn.execute(f"ALTER TABLE {first.system_state_table} DROP COLUMN default_workspace_id")
        finally:
            await first.aclose()

        upgraded = PostgresStorageProvider(cfg)
        await upgraded.initialize()
        try:
            state = await upgraded.get_system_state()
            assert state.default_workspace_id is None
            assert state.default_agent_id == "operator", "the upgrade must not disturb the neighbouring column"
            await upgraded.set_default_workspace_id("primer")
            assert (await upgraded.get_system_state()).default_workspace_id == "primer"
        finally:
            await upgraded.aclose()

        reopened = PostgresStorageProvider(cfg)
        await reopened.initialize()
        try:
            assert (await reopened.get_system_state()).default_workspace_id == "primer"
        finally:
            await reopened.aclose()
    finally:
        await _drop_schema(cfg, cfg.db_schema)
