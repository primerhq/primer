"""Exec op handler for the workspace runtime.

Spawns a subprocess with ``asyncio.create_subprocess_exec``, streams stdout
and stderr to the caller as :class:`~protocol.Event` frames, and emits a
final ``exit`` event once the process terminates.

Usage (from server.py)::

    from primer_runtime.exec import run_exec

    # Inside the WS handler, after receiving an exec request:
    async for event in run_exec(req_id, args, workspace_root):
        await ws.send_str(serialize(event))

The generator handles its own timeout and subprocess teardown.  Callers only
need to iterate and forward frames; cancellation propagates naturally when the
caller's task is cancelled (e.g. on WS disconnect).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from primer_runtime.ops import OpError, _resolve_safe
from primer_runtime.protocol import ErrorCode, Event

log = logging.getLogger(__name__)

_CHUNK_SIZE: int = 4096
_DEFAULT_TIMEOUT_S: float = 60.0


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def run_exec(
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
) -> AsyncIterator[Event]:
    """Async generator that runs a subprocess and yields streaming events.

    Yields
    ------
    Event
        ``event="stdout"`` or ``event="stderr"`` carrying ``data_b64`` for
        each chunk, then a final ``event="exit"`` with ``code`` (and
        ``timed_out=True`` if the timeout was exceeded).

    Raises
    ------
    OpError
        If *workdir* escapes the workspace root, or *cmd* is empty.
    """
    cmd: list[str] = args.get("cmd", [])
    timeout_s: float = float(args.get("timeout_s") or _DEFAULT_TIMEOUT_S)
    stdin_b64: str = args.get("stdin_b64") or ""
    workdir_raw: str | None = args.get("workdir")
    env_extra: dict[str, str] | None = args.get("env")

    if not cmd:
        raise OpError(ErrorCode.EPROTOCOL, "exec: 'cmd' must be a non-empty list")

    # Resolve workdir with path-safety check
    if workdir_raw is not None:
        workdir = str(_resolve_safe(workdir_raw, workspace_root))
    else:
        workdir = workspace_root

    # Decode optional stdin
    stdin_bytes: bytes | None = None
    if stdin_b64:
        try:
            stdin_bytes = base64.b64decode(stdin_b64)
        except Exception as exc:
            raise OpError(ErrorCode.EPROTOCOL, f"exec: invalid base64 for stdin_b64: {exc}")

    # Build environment (inherit host env; overlay extras)
    proc_env = None
    if env_extra:
        proc_env = dict(os.environ)
        proc_env.update(env_extra)

    stdin_pipe = asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL

    proc: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=stdin_pipe,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=proc_env,
    )

    # Write stdin if provided, then close the pipe
    if stdin_bytes is not None and proc.stdin is not None:
        proc.stdin.write(stdin_bytes)
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        proc.stdin.close()

    # Collect events in an async queue so stdout/stderr can be read concurrently
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def _reader(stream: asyncio.StreamReader, event_name: str) -> None:
        while True:
            try:
                chunk = await stream.read(_CHUNK_SIZE)
            except Exception:
                break
            if not chunk:
                break
            queue.put_nowait(
                Event(
                    req_id=req_id,
                    event=event_name,
                    data={"data_b64": base64.b64encode(chunk).decode()},
                )
            )
        queue.put_nowait(None)  # sentinel: this reader is done

    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_task = asyncio.create_task(_reader(proc.stdout, "stdout"))
    stderr_task = asyncio.create_task(_reader(proc.stderr, "stderr"))

    # We expect exactly two sentinels (one per reader)
    sentinels_remaining = 2
    timed_out = False

    try:
        async with asyncio.timeout(timeout_s):
            while sentinels_remaining > 0:
                item = await queue.get()
                if item is None:
                    sentinels_remaining -= 1
                else:
                    yield item

            # Wait for process to exit (readers already drained)
            await proc.wait()

    except TimeoutError:
        timed_out = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        # WS disconnect: cancel the subprocess
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
        stdout_task.cancel()
        stderr_task.cancel()
        try:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        except Exception:
            pass
        raise
    finally:
        stdout_task.cancel()
        stderr_task.cancel()
        try:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        except Exception:
            pass

    if timed_out:
        yield Event(
            req_id=req_id,
            event="exit",
            data={"code": -1, "timed_out": True},
        )
    else:
        returncode = proc.returncode if proc.returncode is not None else -1
        yield Event(
            req_id=req_id,
            event="exit",
            data={"code": returncode},
        )
