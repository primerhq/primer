"""primectl stays in parity with the REST surface.

primectl is a typer app, so the commands are exercised through typer's
CliRunner rather than an argparse parser.
"""

from __future__ import annotations

from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()

# The root callback resolves a target before any command runs, so every
# invocation needs a server even when the command never calls out.
SERVER = ["--server", "http://127.0.0.1:9"]


def test_the_toolset_sub_app_is_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "toolset" in result.stdout


def test_the_python_commands_are_registered() -> None:
    result = runner.invoke(app, SERVER + ["toolset", "--help"])
    assert result.exit_code == 0
    for cmd in ("create-python", "update-python-source", "list-python-tools"):
        assert cmd in result.stdout, cmd


def test_create_python_requires_a_source_file() -> None:
    result = runner.invoke(app, SERVER + ["toolset", "create-python", "--id", "ts-1"])
    assert result.exit_code != 0


def test_a_missing_source_file_fails_before_any_request(tmp_path) -> None:
    # Exit 2 (usage) rather than a connection error: the file is checked
    # locally, so a typo does not look like the server being down.
    result = runner.invoke(
        app,
        SERVER + ["toolset", "create-python", "--id", "ts-1",
                  "--source-file", str(tmp_path / "nope.py")],
    )
    assert result.exit_code == 2
    assert "no such file" in result.stdout + str(result.exception or "")


def test_create_python_reads_the_source_file(tmp_path) -> None:
    src = tmp_path / "t.py"
    src.write_text("# tool source\n", encoding="utf-8")
    result = runner.invoke(
        app,
        SERVER + ["toolset", "create-python", "--id", "ts-1",
                  "--source-file", str(src), "--dry-run"],
    )
    assert result.exit_code == 0, result.stdout
    assert "ts-1" in result.stdout


def test_update_python_source_reads_the_source_file(tmp_path) -> None:
    src = tmp_path / "t.py"
    src.write_text("# replacement\n", encoding="utf-8")
    result = runner.invoke(
        app,
        SERVER + ["toolset", "update-python-source", "--id", "ts-1",
                  "--source-file", str(src), "--dry-run"],
    )
    assert result.exit_code == 0, result.stdout
    assert "ts-1" in result.stdout


def test_source_comes_from_a_file_not_an_argument() -> None:
    # A python module is multi-line; quoting one through a shell is how you
    # register something subtly different from what you wrote.
    result = runner.invoke(app, SERVER + ["toolset", "create-python", "--help"])
    assert "--source-file" in result.stdout
    assert "--source " not in result.stdout
