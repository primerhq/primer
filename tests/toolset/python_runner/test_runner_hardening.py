"""The python tool shim's hardening (Part 3 Task 105).

Inverted from the coverage-gap probes (``review-gaps-security.md`` GS-10, GS-11, GS-35, GS-36).
"""

from __future__ import annotations

import ctypes.util
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from primer.toolset.python_runner._shim import SHIM_SOURCE
from primer.toolset.python_runner.protocol import build_request
from primer.toolset.python_runner.runners import IsolationLevel, LocalHardenedRunner

SECCOMP_ONLY = pytest.mark.skipif(
    not ctypes.util.find_library("seccomp"), reason="libseccomp not present on this host"
)


def _req(module: str, args: dict | None = None) -> dict:
    req = build_request(module=module, fn="run", phase="call", args=args or {}, ctx={}, cpu_seconds=10,
                        address_space_bytes=512 * 1024 * 1024)
    req["allow_network"] = False
    return req


def _shim(module: str) -> dict:
    """The shim run directly, NOT as a session leader (a leader's setsid fails whatever the filter says)."""
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", SHIM_SOURCE], input=json.dumps(_req(module)), capture_output=True,
        text=True, timeout=60, env={"PATH": "/usr/bin:/bin"},
    )
    return json.loads(proc.stdout)


@pytest.fixture
def runner() -> LocalHardenedRunner:
    r = LocalHardenedRunner(env={})
    if r.isolation_level is not IsolationLevel.SECCOMP:
        pytest.skip("needs libseccomp on the host")
    return r


# --------------------------------------------------------------------------- GS-10: fork


async def test_a_tool_cannot_fork_so_no_child_outlives_the_call(runner, tmp_path) -> None:
    pidfile = tmp_path / "child.pid"
    module = (
        "import os, time\n"
        "def run(pidfile):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os.setsid()\n"
        "        open(pidfile, 'w').write(str(os.getpid()))\n"
        "        time.sleep(60)\n"
        "        os._exit(0)\n"
        "    time.sleep(30)\n"
    )
    resp = await runner.run(_req(module, {"pidfile": str(pidfile)}), timeout_seconds=2)
    time.sleep(0.5)
    if pidfile.exists():
        os.kill(int(pidfile.read_text()), signal.SIGKILL)
    assert not pidfile.exists(), "a forked child of the tool ran"
    assert resp.ok is False and resp.error["type"] == "PermissionError"


@SECCOMP_ONLY
@pytest.mark.parametrize("call", ["os.setsid()", "os.setpgid(0, 0)"])
def test_a_tool_cannot_leave_its_process_group(call: str) -> None:
    module = (
        "import os\n"
        "def run():\n"
        "    try:\n"
        f"        {call}\n"
        "        return 'left'\n"
        "    except PermissionError:\n"
        "        return 'refused'\n"
    )
    assert _shim(module) == {"ok": True, "value": "refused"}
