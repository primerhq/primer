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
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from primer.api.config import AppConfig

import primer.cli as cli_mod
from primer.cli import _preflight_server


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


def _can_bind(port: int, reuseaddr: bool) -> bool:
    """Whether a fresh socket can bind 127.0.0.1:port with (or without)
    SO_REUSEADDR - the kernel's own oracle for what a (re)launch's
    listener can do on that port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuseaddr:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def test_time_wait_residue_does_not_trip_the_port_refusal():
    """The e2e restart shape: the old server process is dead but the
    kernel keeps the port's TIME_WAIT sockets for ~60 s. The pre-flight
    probe must mirror uvicorn's bind (SO_REUSEADDR on), so residue-only
    is not a live listener and the relaunch may still bind.

    Deterministic, no fixed sleeps: a real server subprocess handles one
    full request (its end then closes first, so the dead server's side
    is the one left in the closing states - the e2e state; killing mid-
    accept would let the kernel RST the queued connection and leave no
    residue at all), then is KILLed while the client still holds its
    end. The kernel's own oracles - a plain bind (the pre-fix probe's
    view) and a SO_REUSEADDR bind (uvicorn's listener, the post-fix
    probe) - are polled until the residue-only state holds; the bound
    fails the test instead of flaking if it never arrives."""
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import http.server
            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'ok')
                def log_message(self, *a):
                    pass
            http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()
        """)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = None
    try:
        # The server subprocess needs a moment to reach its accept loop;
        # retry until it listens (bounded state wait, not a fixed sleep).
        deadline = time.monotonic() + 5
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.001)
        # One full request: the handler closes its end afterwards, so the
        # server side of the connection starts closing before the kill.
        client.sendall(
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        while client.recv(1024):
            pass
        server.kill()
        server.wait(timeout=10)
        # Wait for the kernel to finish the teardown into the residue-only
        # state: a plain bind (the pre-fix probe) still refuses the port,
        # a SO_REUSEADDR bind (uvicorn's listener) no longer does.
        deadline = time.monotonic() + 5
        residue_only = False
        while True:
            residue_only = (not _can_bind(port, reuseaddr=False)) and _can_bind(port, reuseaddr=True)
            if residue_only or time.monotonic() > deadline:
                break
            time.sleep(0.001)
        assert residue_only, "the port of the dead server never reached the residue-only state"
        cfg = AppConfig(port=port, host="127.0.0.1", runtime_mode="api")
        _preflight_server(cfg, None)  # must NOT raise: no live listener

        # A live listener on the same port still trips the refusal
        # one-liner. The holder mirrors uvicorn's listener (REUSEADDR so
        # it can bind over the residue; listen() so it is a real
        # conflict - a REUSEADDR bound-but-not-listening socket is
        # shareable and would not be one).
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        try:
            with pytest.raises(typer.BadParameter, match="already in use"):
                _preflight_server(cfg, None)
        finally:
            holder.close()
    finally:
        if client is not None:
            client.close()
        if server.poll() is None:
            server.kill()
            server.wait(timeout=5)
