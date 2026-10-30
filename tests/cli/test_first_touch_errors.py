"""G15-05 (MED): the ``primer api`` first-touch failure paths exit with a clean one-line error, not a raw traceback.

Four classes, all verified live at baseline (main 8179ccd63; outputs in
/tmp/opencode/g15/out_*.txt, ledger /var/tmp/revamp/audit/G15.md scope 4):

- (a) bad YAML syntax: ``yaml.safe_load`` had no YAMLError handler -> a
  full rich ScannerError traceback.
- (b) bad schema (``port: notanumber``): ``AppConfig(**data)`` had no
  ValidationError handler -> a full pydantic traceback with the useful
  line buried.
- (c) port in use: a bare uvicorn ``ERROR: [Errno 98] ... address
  already in use``, no hint at primer's port knob.
- (d) Postgres unreachable: a raw asyncpg traceback through the app
  lifespan, nothing pointing at the config file's ``db.config`` block.

The fix extends the typer.BadParameter pattern the CLI already uses for
the other two config failures (file not found, root not a mapping): each
class renders as the clean one-line ``Error: ...`` (exit 2) that names
the config file, the offending knob, and the next step. The tests drive
the real CLI entry (typer.testing.CliRunner) against scratch configs
under /tmp/opencode/g15b/ on dynamically allocated private ports (never
9000/8767/55432/5432) and reap every dummy socket. Polarity: RED at the
base commit, GREEN once the fix lands.
"""

from __future__ import annotations

import socket
import textwrap
from pathlib import Path

from typer.testing import CliRunner

import primer.cli as cli_mod


SCRATCH = Path("/tmp/opencode/g15b")


def _scratch_config(name: str, body: str) -> Path:
    """Write a scratch config under /tmp/opencode/g15b/ (fresh each run)."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _free_port() -> int:
    """Bind port 0, hand back the kernel-chosen private port, close the
    socket. The kernel hands out a fresh ephemeral port, so this is never
    9000/8767/55432/5432."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_bad_yaml_syntax_is_a_clean_one_liner():
    cfg = _scratch_config(
        "bad_yaml.yaml",
        """\
        db:
          provider: postgres
           config: bad-indent
        """,
    )
    result = CliRunner().invoke(cli_mod.app, ["api", "--config", str(cfg)])
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output, result.output
    # Actionable: the config file, that it is not valid YAML, and the
    # location from the yaml mark.
    assert "is not valid YAML" in result.output, result.output
    assert str(cfg) in result.output, result.output


def test_bad_schema_is_a_clean_one_liner():
    cfg = _scratch_config("bad_schema.yaml", "port: notanumber\n")
    result = CliRunner().invoke(cli_mod.app, ["api", "--config", str(cfg)])
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output, result.output
    # Actionable: the config file + the first field error (port).
    assert "failed to validate" in result.output, result.output
    assert "port" in result.output, result.output
    assert str(cfg) in result.output, result.output


def test_port_in_use_is_a_clean_one_liner():
    # Hold the scratch port with a dummy socket the whole time the CLI
    # runs, then reap it.
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        cfg = _scratch_config(
            "port_in_use.yaml",
            f"""\
            db:
              provider: sqlite
              config:
                path: {SCRATCH / "port_in_use.sqlite"}
            port: {port}
            log_level: warning
            """,
        )
        result = CliRunner().invoke(cli_mod.app, ["api", "--config", str(cfg)])
    finally:
        holder.close()
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output, result.output
    # Actionable: the port, that it is in use, and primer's port knob.
    assert f"port {port}" in result.output, result.output
    assert "already in use" in result.output, result.output
    assert "port:" in result.output, result.output


def test_postgres_unreachable_is_a_clean_one_liner():
    # A port that _free_port just released: nothing is listening on it,
    # so the pre-flight connect is refused.
    dead_port = _free_port()
    api_port = _free_port()
    cfg = _scratch_config(
        "pg_unreachable.yaml",
        f"""\
        db:
          provider: postgres
          config:
            hostname: 127.0.0.1
            port: {dead_port}
            username: nobody
            password: nothing
            database: primernope
        port: {api_port}
        log_level: warning
        """,
    )
    result = CliRunner().invoke(cli_mod.app, ["api", "--config", str(cfg)])
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output, result.output
    # Actionable: the host:port, that Postgres was not reachable, and
    # the config file's db.config block.
    assert f"127.0.0.1:{dead_port}" in result.output, result.output
    assert "not reachable" in result.output, result.output
    assert "db.config" in result.output, result.output
    assert str(cfg) in result.output, result.output
