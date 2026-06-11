from typer.testing import CliRunner

from primectl import __version__
from primectl.main import app

runner = CliRunner()


def test_version_runs():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_app():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "primectl" in result.stdout.lower()
