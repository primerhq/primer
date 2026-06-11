from pathlib import Path

from typer.testing import CliRunner

from primectl.main import app
from primectl.config import load_config

runner = CliRunner()


def test_set_and_use_context(tmp_path: Path, monkeypatch):
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setenv("PRIMECTL_CONFIG", str(cfgfile))
    r1 = runner.invoke(app, ["config", "set-context", "dogfood", "--server", "http://localhost:9000"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["config", "use-context", "dogfood"])
    assert r2.exit_code == 0, r2.output
    cfg = load_config(cfgfile)
    assert cfg.current_context == "dogfood"
    assert cfg.contexts["dogfood"].server == "http://localhost:9000"


def test_get_contexts_lists(tmp_path: Path, monkeypatch):
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setenv("PRIMECTL_CONFIG", str(cfgfile))
    runner.invoke(app, ["config", "set-context", "a", "--server", "http://a"])
    result = runner.invoke(app, ["config", "get-contexts"])
    assert result.exit_code == 0
    assert "a" in result.output


def test_view_redacts_token(tmp_path: Path, monkeypatch):
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setenv("PRIMECTL_CONFIG", str(cfgfile))
    runner.invoke(app, ["config", "set-context", "a", "--server", "http://a", "--token", "supersecret"])
    result = runner.invoke(app, ["config", "view"])
    assert result.exit_code == 0
    assert "supersecret" not in result.output
    assert "REDACTED" in result.output
