"""Tests for the ordered data-migration runner.

The runner operates through ``Storage[T]`` handles rather than raw SQL, so
these tests exercise it against the SQLite backend and the behaviour holds
for Postgres by construction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.provider import SqliteConfig
from primer.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    run_migrations,
)
from primer.storage.sqlite import SqliteStorageProvider

# asyncio_mode = "auto" in pyproject.toml, so async tests need no marker.


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "migrations.sqlite"))
    )
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_versions_are_contiguous_from_one(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        assert versions == list(range(1, len(MIGRATIONS) + 1))

    def test_versions_are_unique(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        assert len(set(versions)) == len(versions)

    def test_latest_version_matches_registry(self) -> None:
        assert LATEST_VERSION == len(MIGRATIONS)

    def test_every_migration_has_a_description(self) -> None:
        assert all(m.description.strip() for m in MIGRATIONS)


# ---------------------------------------------------------------------------
# Runner behaviour
# ---------------------------------------------------------------------------


class TestRunMigrations:
    async def test_fresh_install_baselines_without_applying(
        self, sp: StorageProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh install has nothing to transform, so it baselines."""
        applied: list[int] = []
        for migration in MIGRATIONS:
            monkeypatch.setattr(
                migration,
                "apply",
                _recording_apply(migration.version, applied),
                raising=False,
            )

        result = await run_migrations(sp, is_fresh_install=True)

        assert result == LATEST_VERSION
        assert applied == []
        state = await sp.get_system_state()
        assert state.schema_version == LATEST_VERSION
        assert state.last_migration_at is not None

    async def test_existing_install_converges_on_latest(
        self, sp: StorageProvider
    ) -> None:
        result = await run_migrations(sp, is_fresh_install=False)

        assert result == LATEST_VERSION
        state = await sp.get_system_state()
        assert state.schema_version == LATEST_VERSION

    async def test_pending_migration_runs_and_stamps(
        self, sp: StorageProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A migration above the stored version runs and advances it.

        Driven with a synthetic registry rather than the real one so the
        test keeps meaning as migrations are added: with schema_version
        defaulting to 1, whether any SHIPPED migration is pending depends
        on how many exist.
        """
        applied: list[int] = []
        fake = _FakeMigration(version=2, sink=applied)
        monkeypatch.setattr(
            "primer.storage.migrations.MIGRATIONS", (fake,),
        )
        monkeypatch.setattr("primer.storage.migrations.LATEST_VERSION", 2)

        result = await run_migrations(sp, is_fresh_install=False)

        assert applied == [2]
        assert result == 2
        state = await sp.get_system_state()
        assert state.schema_version == 2
        assert state.last_migration_at is not None

    async def test_already_applied_migrations_are_skipped(
        self, sp: StorageProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """schema_version = N means migrations 1..N have already run."""
        await sp.set_schema_version(LATEST_VERSION)
        applied: list[int] = []
        for migration in MIGRATIONS:
            monkeypatch.setattr(
                migration,
                "apply",
                _recording_apply(migration.version, applied),
                raising=False,
            )

        await run_migrations(sp, is_fresh_install=False)

        assert applied == []

    async def test_double_apply_is_idempotent(self, sp: StorageProvider) -> None:
        await run_migrations(sp, is_fresh_install=False)
        first = await sp.get_system_state()
        await run_migrations(sp, is_fresh_install=False)
        second = await sp.get_system_state()

        assert second.schema_version == first.schema_version == LATEST_VERSION

    async def test_future_version_warns_and_does_not_downgrade(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Older code against a newer DB must refuse rather than guess."""
        future = LATEST_VERSION + 5
        await sp.set_schema_version(future)

        with caplog.at_level(logging.WARNING):
            result = await run_migrations(sp, is_fresh_install=False)

        assert result == future
        state = await sp.get_system_state()
        assert state.schema_version == future
        assert any(
            "newer than this build" in record.getMessage()
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# set_schema_version storage contract
# ---------------------------------------------------------------------------


class TestSetSchemaVersion:
    async def test_stamps_version_and_timestamp(self, sp: StorageProvider) -> None:
        await sp.set_schema_version(7)
        state = await sp.get_system_state()
        assert state.schema_version == 7
        assert state.last_migration_at is not None

    async def test_overwrites_previous_value(self, sp: StorageProvider) -> None:
        await sp.set_schema_version(3)
        await sp.set_schema_version(4)
        state = await sp.get_system_state()
        assert state.schema_version == 4


def _recording_apply(version: int, sink: list[int]):
    async def _apply(_sp: StorageProvider) -> None:
        sink.append(version)

    return _apply


class _FakeMigration:
    """Stand-in migration so runner tests do not depend on the real chain."""

    def __init__(self, *, version: int, sink: list[int]) -> None:
        self.version = version
        self.description = f"fake migration {version}"
        self._sink = sink

    async def apply(self, _sp: StorageProvider) -> None:
        self._sink.append(self.version)
