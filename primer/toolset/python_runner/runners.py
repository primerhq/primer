"""Where a python tool actually runs, and what that buys.

Two runners: the container-backed one (real namespaces, via the Sandbox the
workspace subsystem already ships) and a hardened local subprocess.

The local one deliberately does NOT run under primer's interpreter. ``-I -S``
with no PYTHONPATH keeps primer's site-packages off ``sys.path``, because a
function that can ``import primer.storage`` reaches the database with ambient
credentials and no rlimit or syscall filter touches that.

The achieved isolation level is reported rather than assumed. ``rlimit-only``
is a real level with a real gap - it bounds CPU and memory but stops neither
filesystem reads nor egress - and the console says so instead of showing a
generic "sandboxed" badge.
"""

from __future__ import annotations

import asyncio
import ctypes.util
import json
import platform
import shutil
import sys
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from primer.toolset.python_runner._shim import SHIM_SOURCE
from primer.toolset.python_runner.protocol import ShimResponse, parse_response

# A tool gets a minimal PATH and nothing else. Notably no PYTHONPATH: see the
# module docstring.
_BASE_ENV = {"PATH": "/usr/bin:/bin"}

_MACOS_SANDBOX_PROFILE = (
    "(version 1)"
    "(deny default)"
    "(allow process-exec)"
    "(allow process-fork)"
    "(allow sysctl-read)"
    "(allow file-read*)"
    "(deny network*)"
)


class IsolationLevel(str, Enum):
    """What the runner can actually enforce, not what it aspires to."""

    CONTAINER = "container"
    SECCOMP = "seccomp"
    SANDBOX_EXEC = "sandbox-exec"
    RLIMIT_ONLY = "rlimit-only"


def detect_local_isolation() -> IsolationLevel:
    """What the local runner can enforce on this host."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return IsolationLevel.SANDBOX_EXEC
    if platform.system() == "Linux":
        # libseccomp is a SYSTEM library reached through ctypes, not a Python
        # package. It has to be: the shim runs under -I -S, so no
        # site-package is importable there and a pip dependency would never
        # load.
        if ctypes.util.find_library("seccomp"):
            return IsolationLevel.SECCOMP
        return IsolationLevel.RLIMIT_ONLY
    return IsolationLevel.RLIMIT_ONLY


def _timeout_error() -> ShimResponse:
    return ShimResponse(
        ok=False,
        error={
            "type": "TimeoutError",
            "message": "the tool exceeded its timeout and was killed",
            "traceback": "",
        },
    )


class Runner(ABC):
    """Executes one shim request and returns its response."""

    @property
    @abstractmethod
    def isolation_level(self) -> IsolationLevel: ...

    @abstractmethod
    async def run(
        self, request: dict[str, Any], *, timeout_seconds: float
    ) -> ShimResponse: ...


class LocalHardenedRunner(Runner):
    """Subprocess on the host, hardened as far as the platform allows."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = dict(env or {})
        self._level = detect_local_isolation()

    @property
    def isolation_level(self) -> IsolationLevel:
        return self._level

    def _argv(self) -> list[str]:
        base = [sys.executable, "-I", "-S", "-c", SHIM_SOURCE]
        if self._level is IsolationLevel.SANDBOX_EXEC:
            return ["sandbox-exec", "-p", _MACOS_SANDBOX_PROFILE, *base]
        return base

    async def run(
        self, request: dict[str, Any], *, timeout_seconds: float
    ) -> ShimResponse:
        proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**_BASE_ENV, **self._env},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(request).encode()),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            # The wall clock is the real guarantee; RLIMIT_CPU only catches
            # CPU-bound work, so a tool sleeping forever needs this kill.
            proc.kill()
            await proc.wait()
            return _timeout_error()

        if proc.returncode != 0 and not stdout:
            return ShimResponse(
                ok=False,
                error={
                    "type": "RunnerError",
                    "message": f"the runner exited with code {proc.returncode}",
                    "traceback": stderr.decode("utf-8", "replace")[-2000:],
                },
            )
        return parse_response(stdout.decode("utf-8", "replace"))


class SandboxRunner(Runner):
    """Runs the shim inside a container/k8s Sandbox.

    Sandbox.exec already kills on timeout and raises TimeoutError, so the
    wall-clock guarantee is the sandbox's rather than ours.
    """

    def __init__(self, sandbox: Any, env: dict[str, str] | None = None) -> None:
        self._sandbox = sandbox
        self._env = dict(env or {})

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.CONTAINER

    async def run(
        self, request: dict[str, Any], *, timeout_seconds: float
    ) -> ShimResponse:
        try:
            result = await self._sandbox.exec(
                ["python3", "-I", "-S", "-c", SHIM_SOURCE],
                env={**_BASE_ENV, **self._env},
                timeout_seconds=timeout_seconds,
                stdin=json.dumps(request).encode(),
                # A tool call does not write the workspace tree, so it must not
                # serialise against writers.
                access="read",
            )
        except TimeoutError:
            return _timeout_error()

        stdout = getattr(result, "stdout", b"")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        return parse_response(str(stdout))
