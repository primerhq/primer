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
import ctypes
import ctypes.util
import json
import logging
import os
import platform
import shutil
import ssl
import sys
import sysconfig
import tempfile
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from primer.common.process_group import NEW_SESSION, kill_process_group
from primer.toolset.python_runner._shim import SHIM_SOURCE
from primer.toolset.python_runner.protocol import ShimResponse, parse_response

logger = logging.getLogger(__name__)

# A tool gets a minimal PATH and nothing else. Notably no PYTHONPATH: see the
# module docstring.
_BASE_ENV = {"PATH": "/usr/bin:/bin"}

# What the host holds of a tool's output: the response line, and the end of stderr for a crash report.
STDOUT_CAP_BYTES = 16 * 1024 * 1024
STDERR_TAIL_BYTES = 64 * 1024

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
    LANDLOCK = "landlock"
    SECCOMP = "seccomp"
    SANDBOX_EXEC = "sandbox-exec"
    RLIMIT_ONLY = "rlimit-only"


def detect_landlock_abi() -> int:
    """The kernel's Landlock ABI version (``landlock_create_ruleset`` with the VERSION flag), or 0 where Landlock is missing or switched off."""
    if platform.system() != "Linux":
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return max(int(libc.syscall(ctypes.c_long(444), None, ctypes.c_size_t(0), ctypes.c_uint32(1))), 0)
    except (OSError, AttributeError):
        return 0


# Read-only for a tool under Landlock (a missing path is skipped): system libraries (extension modules and the libraries they load),
# name resolution and TLS trust files, zone data, and the two device files a tool may use. The interpreter's stdlib prefixes and the
# certificate paths this host's ssl module reports are added by _landlock_read_paths.
_LANDLOCK_READ = (
    "/usr/lib", "/usr/lib64", "/usr/local/lib", "/lib", "/lib64", "/etc/ld.so.cache",
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/host.conf", "/etc/localtime",
    "/etc/ssl", "/etc/pki", "/usr/share/ca-certificates", "/usr/share/zoneinfo",
    "/dev/urandom",
)
_LANDLOCK_READ_WRITE = ("/dev/null",)


def _landlock_read_paths() -> list[str]:
    paths = list(_LANDLOCK_READ)
    paths += [sys.base_prefix, sys.base_exec_prefix, sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]]
    verify = ssl.get_default_verify_paths()
    paths += [p for p in (verify.cafile, verify.capath, verify.openssl_cafile, verify.openssl_capath) if p]
    try:
        import certifi

        paths.append(certifi.where())
    except ImportError:
        pass
    return sorted({p for p in paths if os.path.exists(p)})


def detect_local_isolation() -> IsolationLevel:
    """What the local runner can enforce on this host."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return IsolationLevel.SANDBOX_EXEC
    if platform.system() == "Linux":
        # Landlock confines file access (the shim applies it before the tool loads); with libseccomp the tool also gets the syscall filter.
        if detect_landlock_abi() >= 1:
            return IsolationLevel.LANDLOCK
        # libseccomp is a SYSTEM library reached through ctypes, not a Python
        # package. It has to be: the shim runs under -I -S, so no
        # site-package is importable there and a pip dependency would never
        # load.
        if ctypes.util.find_library("seccomp"):
            return IsolationLevel.SECCOMP
        return IsolationLevel.RLIMIT_ONLY
    return IsolationLevel.RLIMIT_ONLY


def warn_if_unconfined(level: IsolationLevel) -> None:
    """Startup note: the local runner cannot confine file access without Landlock (D17); tools still run, and the badge says what they get."""
    if level in (IsolationLevel.LANDLOCK, IsolationLevel.CONTAINER):
        return
    logger.warning(
        "python tools run without Landlock file confinement (isolation level %r): a tool can read any file this "
        "process's user can. Use a Linux kernel with Landlock (5.13+) or a container runner.", level.value,
    )


def _timeout_error() -> ShimResponse:
    return ShimResponse(
        ok=False,
        error={
            "type": "TimeoutError",
            "message": "the tool exceeded its timeout and was killed",
            "traceback": "",
        },
    )


def _output_too_large_error() -> ShimResponse:
    return ShimResponse(
        ok=False,
        error={
            "type": "OutputTooLargeError",
            "message": f"the tool wrote more than {STDOUT_CAP_BYTES >> 20} MiB of output and was killed",
            "traceback": "",
        },
    )


async def _communicate_capped(
    proc: asyncio.subprocess.Process, data: bytes
) -> tuple[bytes | None, bytes]:
    """``proc.communicate(data)`` that holds at most :data:`STDOUT_CAP_BYTES` of stdout and the last
    :data:`STDERR_TAIL_BYTES` of stderr. A pipe has no RLIMIT_FSIZE, so a printing loop would otherwise fill API memory.
    Returns ``(None, b"")`` as soon as stdout passes the cap; the caller kills the group."""
    async def feed() -> None:
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass          # the shim exited without reading its request; its exit code and output say why
        finally:
            proc.stdin.close()

    async def tail() -> bytes:
        kept = bytearray()
        while chunk := await proc.stderr.read(1 << 16):
            kept += chunk
            del kept[:-STDERR_TAIL_BYTES]
        return bytes(kept)

    feeding, draining = asyncio.ensure_future(feed()), asyncio.ensure_future(tail())
    try:
        out = bytearray()
        while chunk := await proc.stdout.read(1 << 16):
            out += chunk
            if len(out) > STDOUT_CAP_BYTES:
                return None, b""
        await feeding
        stderr = await draining
        await proc.wait()
        return bytes(out), stderr
    finally:
        feeding.cancel()
        draining.cancel()


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
        # One scratch dir per call: the tool's cwd and TMPDIR, the only place Landlock lets it write.
        with tempfile.TemporaryDirectory(prefix="primer-tool-") as scratch:
            return await self._run_in(scratch, request, timeout_seconds)

    async def _run_in(
        self, scratch: str, request: dict[str, Any], timeout_seconds: float
    ) -> ShimResponse:
        if self._level is IsolationLevel.LANDLOCK:
            request = {**request, "landlock": {
                "read": _landlock_read_paths(), "read_write": [p for p in _LANDLOCK_READ_WRITE if os.path.exists(p)], "scratch": scratch,
            }}
        # Its own session, so a timeout or a cancel kills the shim's whole process group (primer.common.process_group):
        # anything the shim started goes with it. A process the tool deliberately detached (setsid) does not.
        proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**_BASE_ENV, "TMPDIR": scratch, **self._env},
            cwd=scratch,
            **NEW_SESSION,
        )
        finished = False
        try:
            stdout, stderr = await asyncio.wait_for(
                _communicate_capped(proc, json.dumps(request).encode()),
                timeout=timeout_seconds,
            )
            if stdout is None:
                # Past the cap: the finally below kills the group.
                return _output_too_large_error()
            finished = True
        except TimeoutError:
            # The wall clock is the real guarantee; RLIMIT_CPU only catches
            # CPU-bound work, so a tool sleeping forever needs this kill (the finally below).
            return _timeout_error()
        finally:
            # Any way out of the wait that is not the shim finishing: the timeout, a cancel (the hard Cancel, a
            # shutdown, a Stop that cancels the call: the wait_for above was the only thing enforcing the wall
            # clock, and it is gone), or anything else.
            if not finished:
                await kill_process_group(proc)

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
