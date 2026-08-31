"""The system default_agent_id (S1 P5 Task 27).

Where "which agent answers when nobody named one" is persisted. S5
later stamps it during bootstrap; this task only makes it storable, so
a binding-less session create has something to resolve.

Exercised against sqlite because that is the backend a test can build
in a tmpdir. The postgres implementation mirrors it line for line.
"""

from __future__ import annotations

import aiosqlite
import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def store(tmp_path):
    provider = SqliteStorageProvider(
        SqliteConfig(path=tmp_path / "primer.sqlite")
    )
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_fresh_store_has_no_default_agent(store):
    assert (await store.get_system_state()).default_agent_id is None


async def test_set_and_round_trip(store):
    await store.set_default_agent_id("operator")
    assert (await store.get_system_state()).default_agent_id == "operator"


async def test_setting_none_clears_it(store):
    await store.set_default_agent_id("operator")
    await store.set_default_agent_id(None)
    assert (await store.get_system_state()).default_agent_id is None


async def test_overwrite_replaces_rather_than_appends(store):
    await store.set_default_agent_id("operator")
    await store.set_default_agent_id("builder")
    assert (await store.get_system_state()).default_agent_id == "builder"


async def test_a_database_predating_the_column_is_upgraded(tmp_path):
    """The ALTER guard, which is what lets existing installs upgrade
    without a migration step."""
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
            "  sso_default_access     TEXT"
            ")"
        )
        await conn.execute("INSERT INTO system_state (id) VALUES ('singleton')")
        await conn.commit()

    provider = SqliteStorageProvider(SqliteConfig(path=path))
    await provider.initialize()
    try:
        assert (await provider.get_system_state()).default_agent_id is None
        await provider.set_default_agent_id("operator")
        assert (
            await provider.get_system_state()
        ).default_agent_id == "operator"
    finally:
        await provider.aclose()
