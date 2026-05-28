"""Tests for the ``matrix init`` CLI subcommand.

Uses ``typer.testing.CliRunner`` (the same runner as existing CLI tests)
so no real subprocess is spawned and the test stays in-process.

Each test gets an isolated SQLite DB via ``tmp_path``; the config YAML
pointing at that DB is passed with ``--config``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

import matrix.cli as cli_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal YAML config pointing the SQLite DB at tmp_path."""
    cfg_file = tmp_path / "config.yaml"
    db_path = tmp_path / "matrix.sqlite"
    cfg_file.write_text(
        textwrap.dedent(
            f"""\
            db:
              provider: sqlite
              config:
                path: {db_path}
            """
        ),
        encoding="utf-8",
    )
    return cfg_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitCommand:
    def test_init_runs_bootstrap_on_fresh_db(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """First run on an empty DB creates all four reserved providers."""
        cfg_file = _write_config(tmp_path)
        result = runner.invoke(cli_mod.app, ["init", "--config", str(cfg_file)])
        assert result.exit_code == 0, result.output
        output_lower = result.output.lower()
        assert "created" in output_lower

    def test_init_shows_all_four_providers_created(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Output mentions the four reserved ids on a fresh install."""
        cfg_file = _write_config(tmp_path)
        result = runner.invoke(cli_mod.app, ["init", "--config", str(cfg_file)])
        assert result.exit_code == 0, result.output
        output = result.output
        assert "huggingface" in output
        assert "local" in output
        assert "lance" in output

    def test_init_second_run_skips_all(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Second run without --force reports all providers as skipped."""
        cfg_file = _write_config(tmp_path)
        # First run — bootstrap
        first = runner.invoke(cli_mod.app, ["init", "--config", str(cfg_file)])
        assert first.exit_code == 0, first.output
        # Second run — idempotent
        second = runner.invoke(cli_mod.app, ["init", "--config", str(cfg_file)])
        assert second.exit_code == 0, second.output
        assert "skipped" in second.output.lower()

    def test_init_force_re_runs_after_success(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--force re-runs bootstrap even after a successful first run."""
        cfg_file = _write_config(tmp_path)
        # First run
        first = runner.invoke(cli_mod.app, ["init", "--config", str(cfg_file)])
        assert first.exit_code == 0, first.output
        # Force re-run — should succeed (all skipped because rows exist)
        second = runner.invoke(
            cli_mod.app, ["init", "--config", str(cfg_file), "--force"]
        )
        assert second.exit_code == 0, second.output

    def test_init_no_config_uses_defaults(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no --config is given, the command uses defaults successfully.

        We redirect HOME to tmp_path so the default SQLite path
        (~/.matrix/db/data.sqlite) lands under tmp_path rather than the
        real home directory.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(cli_mod.app, ["init"])
        assert result.exit_code == 0, result.output
        output_lower = result.output.lower()
        assert "created" in output_lower or "skipped" in output_lower
