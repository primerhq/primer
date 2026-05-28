"""Unit tests for the Typer CLI in :mod:`primer.cli`.

Covers config loading, runtime-mode forcing, and command wiring.
We do NOT actually start uvicorn — the test app patches the
``_run_uvicorn`` symbol so we capture the constructed AppConfig and
assert on it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import primer.cli as cli_mod
from primer.api.config import AppConfig
from primer.model.provider import StorageProviderType
from primer.model.scheduler import RuntimeMode


# ---------------------------------------------------------------------------
# Shared YAML fixtures — use the NEW nested ``db:`` shape
# ---------------------------------------------------------------------------

_BASE_YAML = """\
db:
  provider: sqlite
  config:
    path: /tmp/test-matrix.sqlite
runtime_mode: api
"""

_BASE_YAML_WITH_SCHEDULER = (
    _BASE_YAML
    + "scheduler:\n"
      "  provider: in_memory\n"
      "  config: {}\n"
)


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
        assert cfg.db is not None
        assert cfg.db.provider == StorageProviderType.SQLITE

    def test_runtime_mode_override_wins(self, base_config_file: Path):
        # YAML says runtime_mode=api — verify the override mechanism keeps it.
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

    def test_empty_file_treated_as_empty_mapping_gives_defaults(
        self, tmp_path: Path,
    ):
        # Empty YAML loads as None; helper treats it as {} and builds
        # AppConfig with all defaults (db=None is valid — zero-config SQLite).
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        cfg = cli_mod._load_config(empty, RuntimeMode.API)
        assert isinstance(cfg, AppConfig)
        assert cfg.db is None  # zero-config default

    # -----------------------------------------------------------------------
    # New: auto-discovery tests
    # -----------------------------------------------------------------------

    def test_load_config_no_path_no_default_file_returns_all_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        # No --config, no ~/.matrix/config.yaml on disk
        cfg = cli_mod._load_config(None, RuntimeMode.API)
        assert cfg.db is None
        assert cfg.runtime_mode == RuntimeMode.API

    def test_load_config_picks_up_home_yaml_when_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        yaml_dir = tmp_path / ".matrix"
        yaml_dir.mkdir()
        (yaml_dir / "config.yaml").write_text(
            textwrap.dedent(
                f"""
                db:
                  provider: sqlite
                  config:
                    path: {tmp_path}/auto-discovered.sqlite
                """
            ),
            encoding="utf-8",
        )
        cfg = cli_mod._load_config(None, RuntimeMode.API)
        assert cfg.db is not None
        assert cfg.db.provider == StorageProviderType.SQLITE

    def test_load_config_explicit_path_overrides_home_yaml(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        home_dir = tmp_path / ".matrix"
        home_dir.mkdir()
        (home_dir / "config.yaml").write_text(
            textwrap.dedent(
                f"""
                db:
                  provider: sqlite
                  config:
                    path: {tmp_path}/home.sqlite
                """
            ),
            encoding="utf-8",
        )
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text(
            textwrap.dedent(
                f"""
                db:
                  provider: sqlite
                  config:
                    path: {tmp_path}/explicit.sqlite
                log_level: debug
                """
            ),
            encoding="utf-8",
        )
        cfg = cli_mod._load_config(explicit, RuntimeMode.API)
        assert cfg.log_level == "debug"
        assert cfg.db.config.path == tmp_path / "explicit.sqlite"  # type: ignore[union-attr]

    def test_load_config_missing_explicit_path_raises(self, tmp_path: Path):
        with pytest.raises(Exception) as ei:
            cli_mod._load_config(tmp_path / "does-not-exist.yaml", RuntimeMode.API)
        assert "not found" in str(ei.value).lower()


# ============================================================================
# `matrix api` — with and without --no-worker
# ============================================================================


class TestApiCommand:
    def test_api_default_forces_runtime_mode_api_plus_worker(
        self, runner: CliRunner, tmp_path: Path, captured: dict,
    ):
        # Default (no flag) starts an in-process worker pool alongside the
        # API — single-process is the friendly default. API_PLUS_WORKER
        # requires a scheduler config; the lifespan auto-defaults to
        # in_memory when none is set, but tests bypass lifespan, so we
        # pass an explicit one to AppConfig.
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(_BASE_YAML_WITH_SCHEDULER, encoding="utf-8")
        result = runner.invoke(
            cli_mod.app, ["api", "--config", str(cfg_path)],
        )
        assert result.exit_code == 0, result.output
        assert captured["config"].runtime_mode == RuntimeMode.API_PLUS_WORKER

    def test_api_no_worker_flag_forces_runtime_mode_api(
        self, runner: CliRunner, base_config_file: Path, captured: dict,
    ):
        result = runner.invoke(
            cli_mod.app,
            ["api", "--config", str(base_config_file), "--no-worker"],
        )
        assert result.exit_code == 0, result.output
        assert captured["config"].runtime_mode == RuntimeMode.API

    def test_api_no_config_arg_uses_defaults(
        self,
        runner: CliRunner,
        captured: dict,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # No ~/.matrix/config.yaml — should succeed with all-defaults
        # AppConfig. Default runtime_mode is now api+worker; the lifespan
        # will auto-resolve the scheduler to in_memory at boot.
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(cli_mod.app, ["api"])
        assert result.exit_code == 0, result.output
        assert captured["config"].db is None
        assert captured["config"].runtime_mode == RuntimeMode.API_PLUS_WORKER


# ============================================================================
# `matrix worker`
# ============================================================================


class TestWorkerCommand:
    def test_worker_forces_runtime_mode_worker(
        self, runner: CliRunner, tmp_path: Path, captured: dict,
    ):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(_BASE_YAML_WITH_SCHEDULER, encoding="utf-8")
        result = runner.invoke(
            cli_mod.app, ["worker", "--config", str(cfg_path)],
        )
        assert result.exit_code == 0, result.output
        assert captured["config"].runtime_mode == RuntimeMode.WORKER

    def test_worker_short_flag(
        self, runner: CliRunner, tmp_path: Path, captured: dict,
    ):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(_BASE_YAML_WITH_SCHEDULER, encoding="utf-8")
        result = runner.invoke(
            cli_mod.app, ["worker", "-c", str(cfg_path)],
        )
        assert result.exit_code == 0, result.output

    def test_worker_no_config_arg_uses_defaults(
        self,
        runner: CliRunner,
        captured: dict,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(cli_mod.app, ["worker"])
        assert result.exit_code == 0, result.output
        assert captured["config"].db is None
        assert captured["config"].runtime_mode == RuntimeMode.WORKER


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
            log_level="debug",
            log_file=tmp_path / "out.log",
            log_json=False,
        )
        cli_mod._apply_logging(cfg)
        import logging as _logging
        assert seen["level"] == _logging.DEBUG
        assert seen["json_format"] is False
        assert seen["file_path"] == tmp_path / "out.log"
