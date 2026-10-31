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

from primer.toolset.python_runner import runners as runners_mod
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


# --------------------------------------------------------------------------- GS-11: output cap


async def test_stdout_past_the_cap_kills_the_tool_and_is_never_buffered_whole(runner, monkeypatch) -> None:
    seen: list[int] = []
    real = runners_mod.parse_response
    monkeypatch.setattr(runners_mod, "parse_response", lambda raw: seen.append(len(raw)) or real(raw))
    module = (
        "import os\n"
        "def run():\n"
        "    chunk = b'x' * (1 << 20)\n"
        "    for _ in range(300):\n"
        "        os.write(1, chunk)\n"
        "    return 1\n"
    )
    resp = await runner.run(_req(module), timeout_seconds=30)
    assert seen == []
    assert resp.ok is False
    assert resp.error == {
        "type": "OutputTooLargeError",
        "message": "the tool wrote more than 16 MiB of output and was killed",
        "traceback": "",
    }


async def test_a_large_stderr_is_drained_not_fatal() -> None:
    module = (
        "import os\n"
        "def run():\n"
        "    chunk = b'e' * (1 << 20)\n"
        "    for _ in range(40):\n"
        "        os.write(2, chunk)\n"
        "    return 'done'\n"
    )
    resp = await LocalHardenedRunner().run(_req(module), timeout_seconds=30)
    assert (resp.ok, resp.value) == (True, "done")


async def test_a_crashed_runner_still_reports_the_tail_of_stderr() -> None:
    module = "import os, sys\ndef run():\n    os.write(2, b'a' * 100000 + b'LAST')\n    os._exit(3)\n"
    resp = await LocalHardenedRunner().run(_req(module), timeout_seconds=30)
    assert resp.ok is False and resp.error["type"] == "RunnerError"
    assert resp.error["message"] == "the runner exited with code 3"
    assert resp.error["traceback"] == ("a" * 100000 + "LAST")[-2000:]
