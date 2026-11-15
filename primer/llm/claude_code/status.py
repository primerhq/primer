"""Host-side status of the claude CLI for the claude_code provider (plan task 2).

The check runs the operator's own CLI the way a worker would reach it: a short,
bounded, process-group-isolated subprocess. Only the facts a worker can verify
locally come back -- installed, version, logged in, meets minimum. ``claude
auth status`` also prints account details (email, org); those are dropped at
the source, so nothing downstream (the API route, a tool, a log line) can
carry them.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass

from primer.common.process_group import NEW_SESSION, kill_process_group

#: The minimum claude CLI version the provider runs. The stream-json contract
#: the adapter relies on is pinned to it; older CLIs get a clean 400 instead
#: of a half-working stream.
MINIMUM_VERSION = (2, 1, 322)

#: The model aliases the CLI accepts in place of a pinned model name.
CLAUDE_CODE_ALIASES = ("sonnet", "opus", "haiku")

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

#: Each CLI call is bounded; a hung CLI is group-killed and reported as missing.
_CALL_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class RuntimeStatus:
    """What the host can say about the claude CLI.

    Deliberately holds no account data: ``email``, org or any other field the
    CLI's auth output prints is not read (see :func:`_parse_logged_in`).
    """

    host: str
    installed: bool
    version: str | None
    logged_in: bool
    meets_minimum: bool


def _parse_version(text: str) -> str | None:
    """The first ``X.Y.Z`` of the ``--version`` line, or None."""
    m = _VERSION_RE.search(text)
    return m.group(0) if m else None


def _parse_logged_in(text: str) -> bool:
    """The ``loggedIn`` boolean of the ``auth status`` JSON; False on any miss.

    Nothing else is read from the output: the account fields are left on the
    child's stdout and die with it.
    """
    try:
        return bool(json.loads(text).get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        return False


async def _run(binary_path: str, *args: str, env: Mapping[str, str]) -> str | None:
    """Run ``binary_path *args`` and return its stdout, or None when it cannot be used.

    ``None`` when the binary is missing, it exits non-zero, or it does not
    finish within the bound (its process group is then killed, so a hung CLI
    leaves no residue). A cancel of the caller kills the group the same way,
    before the cancellation propagates. ``env`` replaces the worker's
    environment wholesale -- the caller builds it (e.g. ``process.child_env``).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            binary_path,
            *args,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **NEW_SESSION,
        )
    except OSError:
        return None  # missing or not executable
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CALL_TIMEOUT_S)
    except TimeoutError:
        await asyncio.shield(kill_process_group(proc))
        return None  # a hung CLI is group-killed and reported as missing
    except BaseException:
        # A CANCEL (a client disconnect while communicate() is awaited) must not
        # leave the child running: kill the group before the exception goes on,
        # shielded so the cancel cannot interrupt the kill itself.
        await asyncio.shield(kill_process_group(proc))
        raise
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


async def runtime_status(binary_path: str, *, env: Mapping[str, str]) -> RuntimeStatus:
    """Check the claude CLI on THIS host: installed, at or above the minimum, logged in.

    Runs ``<binary> --version`` and ``<binary> auth status`` in ``env``.
    ``host`` is ``socket.gethostname()`` -- the worker answering, which is what
    a "your CLI is not logged in ON <host>" error must name.
    """
    host = socket.gethostname()
    version_text = await _run(binary_path, "--version", env=env)
    version = _parse_version(version_text) if version_text is not None else None
    installed = version is not None
    logged_in = False
    if installed:
        auth_text = await _run(binary_path, "auth", "status", env=env)
        logged_in = _parse_logged_in(auth_text) if auth_text is not None else False
    version_tuple = tuple(int(part) for part in version.split(".")) if version else None
    meets_minimum = version_tuple is not None and version_tuple >= MINIMUM_VERSION
    return RuntimeStatus(
        host=host,
        installed=installed,
        version=version,
        logged_in=logged_in,
        meets_minimum=meets_minimum,
    )
