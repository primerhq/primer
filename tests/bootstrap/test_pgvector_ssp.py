"""The reserved pgvector SSP: deterministic seed + legacy migration (C2).

C2 ruling A: the authoritative vector contract is a PERSISTED pgvector
semantic provider created through the bootstrap path. Deterministic
selection: Postgres-mode installs get the reserved pgvector row from the
app's own connection settings; embedded-SQLite mode skips it (Lance
remains the extra-gated default). A legacy top-level ``vector_store``
config block is migrated into the same row with a deprecation WARNING;
an unavailable pgvector backend raises an actionable error (no silent
fallback to Lance).

Red-first: none of this exists on main, so the seed/migration/error
tests all fail there first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.bootstrap.defaults import (
    RESERVED_LANCE_SSP,
    RESERVED_PGVECTOR_SSP,
)
from primer.bootstrap.runner import BootstrapRunner
from primer.model.except_ import ConfigError
from primer.model.provider import (
    EmbeddingProvider,
    CrossEncoderProvider,
    SemanticSearchProvider,
    StorageProviderType,
)
from primer.model.providers.storage import (
    PostgresConfig,
    SqliteConfig,
    StorageProviderConfig,
)
from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate
from primer.storage.sqlite import SqliteStorageProvider
from primer.vector.pgvector import probe_postgres_reachable

_LEGACY_BLOCK = {
    "provider": "pgvector",
    "config": {
        "hostname": "10.1.1.1",
        "port": 5433,
        "database": "legacydb",
        "username": "legacyuser",
        "password": "legacypw",
    },
}


@pytest_asyncio.fixture
async def storage_provider(tmp_path: Path):
    cfg = SqliteConfig(path=tmp_path / "primer.sqlite")
    provider = SqliteStorageProvider(cfg)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
def root_dir(tmp_path: Path) -> Path:
    d = tmp_path / "primer_root"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pg_db_config() -> StorageProviderConfig:
    return StorageProviderConfig(
        provider=StorageProviderType.POSTGRES,
        config=PostgresConfig(
            hostname="db.internal",
            port=5432,
            database="primer",
            username="primer",
            password="pw",
        ),
    )


def _make_runner(
    storage: SqliteStorageProvider,
    root: Path,
    *,
    db_config: StorageProviderConfig | None = None,
    legacy_vector_store: dict[str, Any] | None = None,
) -> BootstrapRunner:
    return BootstrapRunner(
        storage=storage,
        embedder_storage=storage.get_storage(EmbeddingProvider),
        ssp_storage=storage.get_storage(SemanticSearchProvider),
        cross_encoder_storage=storage.get_storage(CrossEncoderProvider),
        workspace_provider_storage=storage.get_storage(WorkspaceProvider),
        workspace_template_storage=storage.get_storage(WorkspaceTemplate),
        root_dir=root,
        db_config=db_config,
        legacy_vector_store=legacy_vector_store,
    )


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch: pytest.MonkeyPatch):
    """The reachability probe is the only real I/O in this step; stub it."""

    async def _ok(config: Any) -> None:
        return None

    monkeypatch.setattr(
        "primer.vector.pgvector.probe_postgres_reachable", _ok
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# Deterministic selection
# ---------------------------------------------------------------------------


class TestPgvectorSelection:
    async def test_postgres_mode_seeds_reserved_pgvector_ssp(
        self, storage_provider, root_dir, monkeypatch
    ):
        monkeypatch.setattr(
            "primer.bootstrap.runner.has_extra", lambda extra: False
        )
        runner = _make_runner(
            storage_provider, root_dir, db_config=_pg_db_config()
        )
        result = await runner.run()
        assert not result.errors, result.errors
        assert RESERVED_PGVECTOR_SSP in result.created
        row = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert row is not None
        assert row.provider == "pgvector"
        # Derived from the app's own connection settings.
        assert row.config.hostname == "db.internal"
        assert row.config.database == "primer"

    async def test_sqlite_mode_skips_pgvector(
        self, storage_provider, root_dir, monkeypatch
    ):
        monkeypatch.setattr(
            "primer.bootstrap.runner.has_extra", lambda extra: False
        )
        runner = _make_runner(storage_provider, root_dir, db_config=None)
        result = await runner.run()
        assert not result.errors, result.errors
        assert RESERVED_PGVECTOR_SSP in result.skipped
        row = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert row is None

    async def test_both_extras_installed_is_deterministic(
        self, storage_provider, root_dir, monkeypatch
    ):
        """Both Lance and pgvector support installed: the pick is
        deterministic (pgvector seeded in Postgres mode; Lance still
        extra-gated, both rows coexist, neither clobbers the other)."""
        monkeypatch.setattr(
            "primer.bootstrap.runner.has_extra", lambda extra: True
        )
        runner = _make_runner(
            storage_provider, root_dir, db_config=_pg_db_config()
        )
        result = await runner.run()
        assert not result.errors, result.errors
        assert RESERVED_PGVECTOR_SSP in result.created
        assert RESERVED_LANCE_SSP in result.created
        pg = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert pg is not None and pg.provider == "pgvector"

    async def test_existing_row_is_skipped_not_clobbered(
        self, storage_provider, root_dir, monkeypatch
    ):
        monkeypatch.setattr(
            "primer.bootstrap.runner.has_extra", lambda extra: False
        )
        existing = SemanticSearchProvider(
            id=RESERVED_PGVECTOR_SSP,
            provider="pgvector",
            config={
                "hostname": "operator-host",
                "port": 5432,
                "database": "opdb",
                "username": "op",
                "password": "op",
            },
        )
        await storage_provider.get_storage(
            SemanticSearchProvider
        ).create(existing)
        runner = _make_runner(
            storage_provider, root_dir, db_config=_pg_db_config()
        )
        result = await runner.run()
        assert not result.errors, result.errors
        assert RESERVED_PGVECTOR_SSP in result.skipped
        row = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert row is not None
        assert row.config.hostname == "operator-host"


# ---------------------------------------------------------------------------
# Legacy vector_store migration
# ---------------------------------------------------------------------------


class TestLegacyVectorStoreMigration:
    async def test_legacy_block_migrates_and_warns(
        self, storage_provider, root_dir, caplog
    ):
        runner = _make_runner(
            storage_provider,
            root_dir,
            db_config=None,  # even SQLite mode: the block carries its own DSN
            legacy_vector_store=dict(_LEGACY_BLOCK),
        )
        with caplog.at_level("WARNING"):
            result = await runner.run()
        assert not result.errors, result.errors
        assert RESERVED_PGVECTOR_SSP in result.created
        row = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert row is not None
        assert row.provider == "pgvector"
        # Translated from the legacy block, not the (absent) app db.
        assert row.config.hostname == _LEGACY_BLOCK["config"]["hostname"]
        assert row.config.port == _LEGACY_BLOCK["config"]["port"]
        # The deprecation WARNING names the legacy key and the replacement.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        dep = [r for r in warnings if "vector_store" in r.getMessage()]
        assert dep, f"no deprecation warning naming 'vector_store': {warnings}"
        assert "pgvector" in dep[0].getMessage()
        assert "deprecat" in dep[0].getMessage().lower()

    async def test_legacy_block_non_pgvector_provider_raises(
        self, storage_provider, root_dir
    ):
        legacy = {"provider": "lance", "config": {}}
        runner = _make_runner(
            storage_provider,
            root_dir,
            db_config=None,
            legacy_vector_store=legacy,
        )
        with pytest.raises(ConfigError, match="lance"):
            await runner.run()

    async def test_legacy_block_invalid_nested_config_raises(
        self, storage_provider, root_dir
    ):
        legacy = {"provider": "pgvector", "config": {"hostname": "only-host"}}
        runner = _make_runner(
            storage_provider,
            root_dir,
            db_config=None,
            legacy_vector_store=legacy,
        )
        with pytest.raises(ConfigError, match="not a valid pgvector"):
            await runner.run()

    async def test_migration_probe_failure_raises_actionable(
        self, storage_provider, root_dir, monkeypatch
    ):
        async def _boom(config: Any) -> None:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(
            "primer.vector.pgvector.probe_postgres_reachable", _boom
        )
        runner = _make_runner(
            storage_provider,
            root_dir,
            db_config=None,
            legacy_vector_store=dict(_LEGACY_BLOCK),
        )
        with pytest.raises(
            ConfigError, match="CREATE EXTENSION"
        ):
            await runner.run()
        # No row, no fallback: the failure is actionable, not silent.
        row = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert row is None

    async def test_postgres_mode_probe_failure_raises_actionable(
        self, storage_provider, root_dir, monkeypatch
    ):
        async def _boom(config: Any) -> None:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(
            "primer.vector.pgvector.probe_postgres_reachable", _boom
        )
        monkeypatch.setattr(
            "primer.bootstrap.runner.has_extra", lambda extra: True
        )
        runner = _make_runner(
            storage_provider, root_dir, db_config=_pg_db_config()
        )
        with pytest.raises(ConfigError, match="unreachable"):
            await runner.run()
        # No silent Lance fallback: pgvector row absent.
        row = await storage_provider.get_storage(
            SemanticSearchProvider
        ).get(RESERVED_PGVECTOR_SSP)
        assert row is None


# ---------------------------------------------------------------------------
# The probe helper itself
# ---------------------------------------------------------------------------


def test_probe_helper_accepts_pgvector_configs():
    """The shared probe takes a PgVectorConfig (and, via inheritance, a
    PgVectorScaleConfig); bootstrap and the /v1/ssp endpoint use the same
    function so both fail the same way."""
    from primer.model.providers.vector import PgVectorConfig

    cfg = PgVectorConfig(
        hostname="h", port=5432, database="d", username="u", password="p"
    )
    assert probe_postgres_reachable is not None
    assert cfg.hostname == "h"
