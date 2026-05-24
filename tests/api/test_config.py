"""Unit tests for matrix.api.config.AppConfig (post-SQLite reshape)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from matrix.api.config import AppConfig
from matrix.model.provider import (
    PostgresConfig,
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from matrix.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
    WorkerConfig,
)


class TestZeroConfigDefaults:
    def test_all_defaults_construct_without_any_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No MATRIX_* env vars, no TOML, no init args.
        for var in (
            "MATRIX_DB__PROVIDER", "MATRIX_CONFIG_PATH",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = AppConfig()
        assert cfg.db is None
        assert cfg.scheduler is None
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8000
        assert cfg.runtime_mode == RuntimeMode.API_PLUS_WORKER


class TestDbField:
    def test_db_accepts_sqlite(self, tmp_path: Path) -> None:
        cfg = AppConfig(
            db=StorageProviderConfig(
                provider=StorageProviderType.SQLITE,
                config=SqliteConfig(path=tmp_path / "data.sqlite"),
            )
        )
        assert cfg.db is not None
        assert cfg.db.provider == StorageProviderType.SQLITE

    def test_db_accepts_postgres(self) -> None:
        from matrix.model.provider import PoolConfig

        cfg = AppConfig(
            db=StorageProviderConfig(
                provider=StorageProviderType.POSTGRES,
                config=PostgresConfig(
                    hostname="h", username="u", password="p", database="d",
                    pool=PoolConfig(),
                ),
            )
        )
        assert cfg.db is not None
        assert cfg.db.provider == StorageProviderType.POSTGRES

    def test_db_from_nested_env_vars(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MATRIX_DB__PROVIDER", "sqlite")
        monkeypatch.setenv(
            "MATRIX_DB__CONFIG__PATH", str(tmp_path / "x.sqlite"),
        )
        cfg = AppConfig()
        assert cfg.db is not None
        assert cfg.db.provider == StorageProviderType.SQLITE
        assert cfg.db.config.path == tmp_path / "x.sqlite"  # type: ignore[union-attr]


class TestRuntimeModeAndScheduler:
    def test_default_runtime_mode_is_api_plus_worker(self) -> None:
        cfg = AppConfig()
        assert cfg.runtime_mode == RuntimeMode.API_PLUS_WORKER

    def test_scheduler_default_is_none(self) -> None:
        cfg = AppConfig()
        assert cfg.scheduler is None

    def test_can_set_scheduler_explicitly(self) -> None:
        cfg = AppConfig(
            scheduler=SchedulerProviderConfig(
                provider=SchedulerProviderType.IN_MEMORY,
                config=InMemorySchedulerConfig(),
            ),
            worker=WorkerConfig(concurrency=4),
        )
        assert cfg.scheduler is not None
        assert cfg.scheduler.provider == SchedulerProviderType.IN_MEMORY
        assert cfg.worker.concurrency == 4


class TestTomlConfigPath:
    def test_toml_db_overrides_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        sqlite_path = tmp_path / "from-toml.sqlite"
        toml_path = tmp_path / "matrix.toml"
        toml_path.write_text(
            textwrap.dedent(
                f"""
                log_level = "debug"

                [db]
                provider = "sqlite"

                [db.config]
                path = "{sqlite_path}"
                """
            ),
            encoding="utf-8",
        )
        for var in ("MATRIX_DB__PROVIDER",):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MATRIX_CONFIG_PATH", str(toml_path))
        cfg = AppConfig()
        assert cfg.log_level == "debug"
        assert cfg.db is not None
        assert cfg.db.provider == StorageProviderType.SQLITE

    def test_init_args_override_toml(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        toml_path = tmp_path / "matrix.toml"
        toml_path.write_text(
            'log_level = "debug"\n', encoding="utf-8",
        )
        monkeypatch.setenv("MATRIX_CONFIG_PATH", str(toml_path))
        cfg = AppConfig(log_level="info")
        assert cfg.log_level == "info"
