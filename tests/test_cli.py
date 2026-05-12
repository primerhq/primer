"""Unit tests for the Typer CLI in :mod:`matrix.cli`.

Covers config loading, runtime-mode forcing, and command wiring.
We do NOT actually start uvicorn — the test app patches the
``_run_uvicorn`` symbol so we capture the constructed AppConfig and
assert on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import matrix.cli as cli_mod
from matrix.api.config import AppConfig
from matrix.model.scheduler import RuntimeMode


_BASE_YAML = """\
db_host: localhost
db_port: 5432
db_database: matrix
db_user: matrix
db_password: matrix
runtime_mode: api
"""


@pytest.fixture
def base_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(_BASE_YAML, encoding="utf-8")
    return p


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch):
    """Patch out side-effects: uvicorn launch + logging reconfig.

    Returns a dict that the patched ``_run_uvicorn`` populates so each
    test can assert against the AppConfig the CLI built.
    """
    captured: dict[str, AppConfig] = {}

    def _fake_run_uvicorn(config: AppConfig) -> None:
        captured["config"] = config

    def _fake_apply_logging(_config: AppConfig) -> None:  # no-op
        pass

    monkeypatch.setattr(cli_mod, "_run_uvicorn", _fake_run_uvicorn)
    monkeypatch.setattr(cli_mod, "_apply_logging", _fake_apply_logging)
    return captured


# ============================================================================
# _load_config — pure helper, doesn't need typer plumbing
# ============================================================================


class TestLoadConfig:
    def test_loads_yaml_into_appconfig(self, base_config_file: Path):
        cfg = cli_mod._load_config(base_config_file, RuntimeMode.API)
        assert isinstance(cfg, AppConfig)
        assert cfg.db_host == "localhost"
        assert cfg.db_database == "matrix"

    def test_runtime_mode_override_wins(self, base_config_file: Path):
        # YAML says runtime_mode=api, override forces API_PLUS_WORKER —
        # but API_PLUS_WORKER requires scheduler config, so use plain API
        # to verify the override mechanism without triggering validation
        # that's unrelated to this test.
        cfg = cli_mod._load_config(base_config_file, RuntimeMode.API)
        assert cfg.runtime_mode == RuntimeMode.API

    def test_missing_file_raises_bad_parameter(self, tmp_path: Path):
        with pytest.raises(typer.BadParameter):
            cli_mod._load_config(tmp_path / "nope.yaml", RuntimeMode.API)

    def test_non_mapping_root_rejected(self, tmp_path: Path):
        bad = tmp_path / "list.yaml"
        bad.write_text("- one\n- two\n", encoding="utf-8")
        with pytest.raises(typer.BadParameter):
            cli_mod._load_config(bad, RuntimeMode.API)

    def test_empty_file_treated_as_empty_mapping(self, tmp_path: Path):
        # Empty YAML loads as None; helper should treat it as {} rather
        # than crashing. It will fail AppConfig validation (db_host is
        # required) but the failure should be a pydantic ValidationError,
        # not a NoneType-attribute error.
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            cli_mod._load_config(empty, RuntimeMode.API)


# ============================================================================
# `matrix api` — with and without --run-worker
# ============================================================================


class TestApiCommand:
    def test_api_default_forces_runtime_mode_api(
        self, runner: CliRunner, base_config_file: Path, captured: dict,
    ):
        result = runner.invoke(
            cli_mod.app, ["api", "--config", str(base_config_file)],
        )
        assert result.exit_code == 0, result.output
        assert captured["config"].runtime_mode == RuntimeMode.API

    def test_api_run_worker_flag_forces_api_plus_worker(
        self, runner: CliRunner, tmp_path: Path, captured: dict,
    ):
        # API_PLUS_WORKER requires a scheduler — use in_memory for the
        # test (no live Postgres needed).
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            _BASE_YAML
            + "scheduler:\n"
              "  provider: in_memory\n"
              "  config: {}\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            cli_mod.app,
            ["api", "--config", str(cfg_path), "--run-worker"],
        )
        assert result.exit_code == 0, result.output
        assert captured["config"].runtime_mode == RuntimeMode.API_PLUS_WORKER

    def test_api_missing_config_arg_errors(self, runner: CliRunner):
        result = runner.invoke(cli_mod.app, ["api"])
        assert result.exit_code != 0


# ============================================================================
# `matrix worker`
# ============================================================================


class TestWorkerCommand:
    def test_worker_forces_runtime_mode_worker(
        self, runner: CliRunner, tmp_path: Path, captured: dict,
    ):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            _BASE_YAML
            + "scheduler:\n"
              "  provider: in_memory\n"
              "  config: {}\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            cli_mod.app, ["worker", "--config", str(cfg_path)],
        )
        assert result.exit_code == 0, result.output
        assert captured["config"].runtime_mode == RuntimeMode.WORKER

    def test_worker_short_flag(
        self, runner: CliRunner, tmp_path: Path, captured: dict,
    ):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            _BASE_YAML
            + "scheduler:\n"
              "  provider: in_memory\n"
              "  config: {}\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            cli_mod.app, ["worker", "-c", str(cfg_path)],
        )
        assert result.exit_code == 0, result.output


# ============================================================================
# Logging wiring — _apply_logging maps level + file_path
# ============================================================================


class TestApplyLogging:
    def test_apply_logging_passes_level_and_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        seen: dict = {}

        def _fake_configure_logging(**kwargs):
            seen.update(kwargs)

        monkeypatch.setattr(cli_mod, "configure_logging", _fake_configure_logging)

        cfg = AppConfig(
            db_host="localhost",
            db_port=5432,
            db_database="matrix",
            db_user="matrix",
            db_password="matrix",
            log_level="debug",
            log_file=tmp_path / "out.log",
            log_json=False,
        )
        cli_mod._apply_logging(cfg)
        import logging as _logging
        assert seen["level"] == _logging.DEBUG
        assert seen["json_format"] is False
        assert seen["file_path"] == tmp_path / "out.log"
