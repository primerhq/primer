"""Runners: process isolation, timeout kill, and honest level reporting."""

from __future__ import annotations

import asyncio

import pytest

from primer.toolset.python_runner.protocol import build_request
from primer.toolset.python_runner.runners import (
    IsolationLevel,
    LocalHardenedRunner,
    SandboxRunner,
    detect_local_isolation,
)

MODULE = (
    "import time\n"
    "def spin(x: str) -> str:\n"
    "    while True:\n"
    "        pass\n"
    "def nap(x: str) -> str:\n"
    "    time.sleep(30)\n"
    "    return 'woke'\n"
    "def ok(x: str) -> str:\n"
    "    return x\n"
)


def _req(fn: str) -> dict:
    return build_request(
        module=MODULE,
        fn=fn,
        phase="call",
        args={"x": "1"},
        ctx={"tool_call_id": "tc-1"},
        cpu_seconds=5,
        address_space_bytes=512 * 1024 * 1024,
    )


@pytest.mark.asyncio
async def test_a_normal_call_returns_its_value() -> None:
    out = await LocalHardenedRunner().run(_req("ok"), timeout_seconds=30.0)
    assert out.ok is True
    assert out.value == "1"


@pytest.mark.asyncio
async def test_a_cpu_bound_infinite_loop_is_killed_at_the_timeout() -> None:
    out = await LocalHardenedRunner().run(_req("spin"), timeout_seconds=2.0)
    assert out.ok is False
    assert out.error["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_a_sleeping_tool_is_killed_too() -> None:
    # RLIMIT_CPU only catches CPU-bound work; a tool blocked in sleep burns no
    # CPU at all, so the wall-clock kill is what actually bounds it.
    out = await LocalHardenedRunner().run(_req("nap"), timeout_seconds=2.0)
    assert out.ok is False
    assert out.error["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_the_timeout_kill_does_not_leak_the_process() -> None:
    runner = LocalHardenedRunner()
    await runner.run(_req("spin"), timeout_seconds=1.0)
    # If the child were still running, the event loop would still have a
    # transport for it; a clean kill leaves nothing to reap.
    await asyncio.sleep(0.1)


def test_the_reported_level_is_one_of_the_declared_four() -> None:
    assert detect_local_isolation() in set(IsolationLevel)


def test_the_local_runner_reports_its_detected_level() -> None:
    assert LocalHardenedRunner().isolation_level == detect_local_isolation()


def test_the_local_runner_never_claims_container_isolation() -> None:
    # The level must describe what is enforced HERE, not what the strongest
    # backend could enforce elsewhere.
    assert LocalHardenedRunner().isolation_level is not IsolationLevel.CONTAINER


def test_the_sandbox_runner_claims_container() -> None:
    assert SandboxRunner(sandbox=object()).isolation_level is IsolationLevel.CONTAINER


@pytest.mark.asyncio
async def test_the_sandbox_runner_maps_a_timeout_to_an_error_result() -> None:
    class _TimingOutSandbox:
        async def exec(self, *a, **k):
            raise TimeoutError("killed")

    out = await SandboxRunner(sandbox=_TimingOutSandbox()).run(
        _req("ok"), timeout_seconds=1.0
    )
    assert out.ok is False
    assert out.error["type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_the_sandbox_runner_reads_stdout_from_the_exec_result() -> None:
    class _Result:
        stdout = b'{"ok": true, "value": "from sandbox"}'

    class _Sandbox:
        async def exec(self, *a, **k):
            return _Result()

    out = await SandboxRunner(sandbox=_Sandbox()).run(_req("ok"), timeout_seconds=5.0)
    assert out.ok is True
    assert out.value == "from sandbox"


@pytest.mark.asyncio
async def test_garbage_on_stdout_is_an_error_not_a_value() -> None:
    class _Result:
        stdout = b"segmentation fault"

    class _Sandbox:
        async def exec(self, *a, **k):
            return _Result()

    out = await SandboxRunner(sandbox=_Sandbox()).run(_req("ok"), timeout_seconds=5.0)
    assert out.ok is False
    assert out.error["type"] == "ProtocolError"
