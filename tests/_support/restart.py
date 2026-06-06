"""Restart the live e2e server in place (same data dir + Postgres schema).

Mirrors the launch in scripts/e2e/bringup.sh: it kills the pid recorded in
tests/.e2e/server.pid and relaunches `uv run primer api --config <config>`,
then polls /v1/health. Used by the persistence (FND-08) and vector-backfill
(KNW-09) tests. Skips cleanly when not running under bringup.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

_REPO = Path(__file__).resolve().parents[2]
_E2E_DIR = _REPO / "tests" / ".e2e"
_PID_FILE = _E2E_DIR / "server.pid"
_CONFIG = _E2E_DIR / "config.yaml"
_STDOUT = _E2E_DIR / "server.stdout"


def under_bringup() -> bool:
    return _PID_FILE.exists() and _CONFIG.exists()


def restart_server(base_url: str, *, timeout: float = 60.0) -> None:
    """Stop and relaunch the e2e server, preserving its data dir."""
    import pytest

    if not under_bringup():
        pytest.skip("server restart requires the scripts/e2e bringup environment")

    old_pid = int(_PID_FILE.read_text().strip())
    try:
        os.kill(old_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(100):
        try:
            os.kill(old_pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break

    with open(_STDOUT, "ab") as out:
        proc = subprocess.Popen(
            ["uv", "run", "primer", "api", "--config", str(_CONFIG)],
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO),
        )
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + timeout
    health = base_url.rstrip("/") + "/v1/health"
    while time.time() < deadline:
        try:
            if httpx.get(health, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy after restart")
