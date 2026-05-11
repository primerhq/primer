"""Unit tests for matrix.api.config.AppConfig."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from matrix.api.config import AppConfig


class TestEnvVarLoading:
    def test_loads_required_db_fields_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MATRIX_DB_HOST", "db.local")
        monkeypatch.setenv("MATRIX_DB_DATABASE", "matrix")
        monkeypatch.setenv("MATRIX_DB_USER", "matrix")
        monkeypatch.setenv("MATRIX_DB_PASSWORD", "secret")
        config = AppConfig()
        assert config.db_host == "db.local"
        assert config.db_database == "matrix"
        assert config.db_user == "matrix"
        assert config.db_password.get_secret_value() == "secret"

    def test_defaults_for_optional_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MATRIX_DB_HOST", "h")
        monkeypatch.setenv("MATRIX_DB_DATABASE", "d")
        monkeypatch.setenv("MATRIX_DB_USER", "u")
        monkeypatch.setenv("MATRIX_DB_PASSWORD", "p")
        config = AppConfig()
        assert config.db_port == 5432
        assert config.db_min_pool_size == 1
        assert config.db_max_pool_size == 10
        assert config.host == "0.0.0.0"
        assert config.port == 8000
        assert config.log_level == "info"

    def test_missing_required_field_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MATRIX_DB_DATABASE", raising=False)
        monkeypatch.delenv("MATRIX_DB_USER", raising=False)
        monkeypatch.delenv("MATRIX_DB_PASSWORD", raising=False)
        monkeypatch.delenv("MATRIX_CONFIG_PATH", raising=False)
        monkeypatch.setenv("MATRIX_DB_HOST", "h")
        with pytest.raises(ValidationError):
            AppConfig()

    def test_invalid_log_level_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MATRIX_DB_HOST", "h")
        monkeypatch.setenv("MATRIX_DB_DATABASE", "d")
        monkeypatch.setenv("MATRIX_DB_USER", "u")
        monkeypatch.setenv("MATRIX_DB_PASSWORD", "p")
        monkeypatch.setenv("MATRIX_LOG_LEVEL", "verbose")
        with pytest.raises(ValidationError):
            AppConfig()


class TestTomlConfigPath:
    def test_reads_config_from_toml_when_path_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        toml_path = tmp_path / "matrix.toml"
        toml_path.write_text(
            textwrap.dedent(
                """
                db_host = "toml-host"
                db_port = 6543
                db_database = "toml-db"
                db_user = "toml-user"
                db_password = "toml-secret"
                log_level = "debug"
                """
            ),
            encoding="utf-8",
        )
        for var in ("DB_HOST", "DB_DATABASE", "DB_USER", "DB_PASSWORD", "LOG_LEVEL"):
            monkeypatch.delenv(f"MATRIX_{var}", raising=False)
        monkeypatch.setenv("MATRIX_CONFIG_PATH", str(toml_path))
        config = AppConfig()
        assert config.db_host == "toml-host"
        assert config.db_port == 6543
        assert config.db_database == "toml-db"
        assert config.db_user == "toml-user"
        assert config.db_password.get_secret_value() == "toml-secret"
        assert config.log_level == "debug"

    def test_env_var_overrides_toml(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        toml_path = tmp_path / "matrix.toml"
        toml_path.write_text(
            textwrap.dedent(
                """
                db_host = "toml-host"
                db_database = "d"
                db_user = "u"
                db_password = "p"
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("MATRIX_CONFIG_PATH", str(toml_path))
        monkeypatch.setenv("MATRIX_DB_HOST", "env-host")
        config = AppConfig()
        assert config.db_host == "env-host"


def test_app_config_default_runtime_mode_is_api_plus_worker(monkeypatch):
    monkeypatch.setenv("MATRIX_DB_HOST", "localhost")
    monkeypatch.setenv("MATRIX_DB_DATABASE", "matrix")
    monkeypatch.setenv("MATRIX_DB_USER", "u")
    monkeypatch.setenv("MATRIX_DB_PASSWORD", "p")
    from matrix.api.config import AppConfig
    from matrix.model.scheduler import RuntimeMode
    cfg = AppConfig()
    assert cfg.runtime_mode == RuntimeMode.API_PLUS_WORKER
    assert cfg.scheduler is None
    assert cfg.worker.concurrency == 8


def test_app_config_accepts_scheduler_and_worker(monkeypatch):
    monkeypatch.setenv("MATRIX_DB_HOST", "localhost")
    monkeypatch.setenv("MATRIX_DB_DATABASE", "matrix")
    monkeypatch.setenv("MATRIX_DB_USER", "u")
    monkeypatch.setenv("MATRIX_DB_PASSWORD", "p")
    from matrix.api.config import AppConfig
    from matrix.model.scheduler import (
        InMemorySchedulerConfig,
        SchedulerProviderConfig,
        SchedulerProviderType,
        WorkerConfig,
    )
    cfg = AppConfig(
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
        worker=WorkerConfig(concurrency=4),
    )
    assert cfg.scheduler.provider == SchedulerProviderType.IN_MEMORY
    assert cfg.worker.concurrency == 4
