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


def _plain(text: str) -> str:
    """Strip ANSI and soft line breaks from typer's rich help output."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text).replace("\n", " ")


def test_the_toolset_sub_app_is_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "toolset" in _plain(result.stdout)


def test_the_python_commands_are_registered() -> None:
    result = runner.invoke(app, SERVER + ["toolset", "--help"])
    assert result.exit_code == 0
    plain = _plain(result.stdout)
    for cmd in ("create-python", "update-python-source", "list-python-tools"):
        assert cmd in plain, cmd


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
    #
    # Asserted against the declared parameters rather than --help: typer
    # renders help in a rich box with ANSI codes, wrapped to the terminal
    # width, so a flag name can be split across lines.
    import inspect

    from primectl.commands.toolset import create_python, update_python_source

    for fn in (create_python, update_python_source):
        params = inspect.signature(fn).parameters
        assert "source_file" in params, fn.__name__
        assert "source" not in params, fn.__name__
