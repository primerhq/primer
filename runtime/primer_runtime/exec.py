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
import pathlib
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import nullcontext
from typing import Any

from primer_runtime.locks import WorkspaceLockTable
from primer_runtime.ops import OpError, _resolve_safe, _strict_write_locking
from primer_runtime.process_group import NEW_SESSION, stop_process_group
from primer_runtime.protocol import ErrorCode, Event, Response, serialize

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
    locks: WorkspaceLockTable,
) -> AsyncIterator[Event]:
    """Async generator that runs a subprocess and yields streaming events.

    Tier-B write locking: unless ``access == "read"`` (which acquires nothing),
    the chosen lock context is held around the ENTIRE subprocess lifetime - the
    generator keeps it for as long as the process streams. A ``write`` exec that
    declares ``writes`` globs holds the sorted per-path locks; otherwise it holds
    the workdir SCOPE lock (the workspace root when strict-write-locking is on,
    else the resolved workdir), so concurrent writers to the same directory
    serialize while reads never wait.

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
    access: str = (args.get("access") or "write").lower()
    writes = args.get("writes")
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

    # Choose the Tier-B lock context held around the ENTIRE subprocess lifetime.
    #   read:            acquires nothing (nullcontext).
    #   write + writes:  the sorted declared per-path locks.
    #   write (default): the workdir scope lock (workspace root when strict, else
    #                    the resolved workdir).
    if access == "read":
        lock_ctx: Any = nullcontext()
    elif writes:
        resolved_writes = [str(_resolve_safe(w, workspace_root)) for w in writes]
        lock_ctx = locks.hold_paths(resolved_writes)
    else:
        if _strict_write_locking():
            scope = str(pathlib.Path(workspace_root).resolve())
        else:
            scope = str(pathlib.Path(workdir).resolve())
        lock_ctx = locks.hold_scope(scope)

    async with lock_ctx:
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

        # Its own session, so every way out of the exec but the command finishing can stop the WHOLE process group
        # (primer_runtime.process_group): the command is usually ``/bin/sh -c ...``, which forks, and signalling only
        # the shell left its children running with the pipes and the write lock.
        proc: asyncio.subprocess.Process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=proc_env,
            **NEW_SESSION,
        )

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

        async def _feed_stdin() -> None:
            """Write the stdin and close the pipe, ALONGSIDE the readers and under the same timeout and ``finally`` as
            the rest of the exec. It used to be written (and drained) before either started: a command that does not
            read a large stdin blocked ``drain()`` for as long as it lived, with no timeout and nothing to stop it on a
            cancel, and a command that writes a lot before it reads deadlocked against a writer waiting on it."""
            assert proc.stdin is not None and stdin_bytes is not None
            try:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001 - the transport may already be closed by the stop
                    pass

        helper_tasks = [stdout_task, stderr_task]
        if stdin_bytes is not None and proc.stdin is not None:
            helper_tasks.append(asyncio.create_task(_feed_stdin()))

        # We expect exactly two sentinels (one per reader)
        sentinels_remaining = 2
        timed_out = False
        finished = False

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
            finished = True

        except TimeoutError:
            timed_out = True
        finally:
            # EVERY way out of the exec but the command finishing stops the whole group: the timeout, a cancel (WS close,
            # an exec cancelled while it awaits the queue), and a generator closed where it is suspended at a ``yield``
            # (GeneratorExit: a task cancelled while it was blocked in ``send`` closes it there, which no ``except`` arm
            # saw, so the process was never signalled). Keyed on ``finished`` and not on ``proc.returncode``: the shell
            # can be gone while a job it left behind, still in the group, is not. The write lock is released only after
            # this returns (the ``async with lock_ctx`` above), with a deliberate exception: a consumer cancel that
            # ARRIVES while the stop is waiting (a second cancel after the first started the stop, or a cancel after the
            # timeout started it; in the SIGTERM grace or in the kill wait) does not wait it out. The stop's ``finally``
            # sends the SIGKILL (it cannot be ignored) and the cancel then propagates at once, so the lock is released
            # once the SIGKILL has been SENT, not once the group is confirmed gone.
            try:
                if not finished:
                    await stop_process_group(proc)
            finally:
                for task in helper_tasks:
                    task.cancel()
                try:
                    await asyncio.gather(*helper_tasks, return_exceptions=True)
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


# ---------------------------------------------------------------------------
# Exec-as-task: keep the runtime message loop free while an exec streams
# ---------------------------------------------------------------------------


class ExecRegistry:
    """Per-connection map of in-flight exec stream tasks, by the req_id they were requested under.

    Mirrors :class:`~primer_runtime.pty_op.PtyRegistry` for the exec op: the
    server spawns each ``exec`` as a tracked task so a long-running exec never
    blocks the single runtime message loop (which also services
    ``pty_stdin``/``pty_resize`` and file ops).  ``cancel`` serves the
    ``exec_cancel`` op (one exec, by req_id) and ``cancel_all`` is invoked on
    WS close to tear down any exec still streaming.
    """

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def add(self, req_id: int, task: asyncio.Task[None]) -> None:
        self._tasks[req_id] = task

    def discard(self, req_id: int, task: asyncio.Task[None]) -> None:
        """Forget *task*; a no-op if *req_id* now belongs to another task (a client that reuses a req_id)."""
        if self._tasks.get(req_id) is task:
            del self._tasks[req_id]

    def in_flight(self, req_id: int) -> bool:
        """Whether an exec requested under *req_id* is registered and not done (one that is being stopped still is).

        The registry holds ONE task per req_id, so the server refuses an ``exec`` under a req_id for which this is true:
        registering a second would drop the first from the registry, out of reach of ``cancel`` and of ``cancel_all``.
        """
        task = self._tasks.get(req_id)
        return task is not None and not task.done()

    def cancel(self, req_id: int) -> bool:
        """Cancel the in-flight exec requested under *req_id*; ``False`` if none is running (never started, or finished).

        Idempotent: an exec that is already being stopped is not cancelled again, since a second cancel of the task is what
        cuts the command's SIGTERM grace short (see ``run_exec``), and a client that repeats ``exec_cancel`` must not be
        able to do that. It still answers ``True``: the exec is in flight until its stop is done.
        """
        task = self._tasks.get(req_id)
        if task is None or task.done():
            return False
        if not task.cancelling():
            task.cancel()
        return True

    def cancel_all(self) -> None:
        """Cancel every in-flight exec task (called on WS close)."""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()


# Strong references to the lifecycle broadcasts in flight (see ``_announce``): the loop keeps only weak ones to its tasks.
_BROADCASTS: set[asyncio.Task[None]] = set()


def _broadcast_done(task: asyncio.Task[None]) -> None:
    _BROADCASTS.discard(task)
    if not task.cancelled():
        task.exception()                # retrieved: nobody awaits it any more once the exec task was cancelled


async def _announce(broadcaster: Any, kind: str, data: dict[str, Any]) -> None:
    """Broadcast a lifecycle event so that a cancel of the exec task cannot cut it off.

    The broadcast awaits each subscriber's socket, and an ``exec_cancel`` can land in that window: a client that left
    the wait just as the command ended. Awaited directly, the cancel would abandon the ``exec_exited`` fan-out half done.
    Here the broadcast runs as its own task and the exec task only waits for it behind a shield: a cancel ends the exec
    task and leaves the broadcast to finish.
    """
    task = asyncio.ensure_future(broadcaster.broadcast(kind, data))
    _BROADCASTS.add(task)
    task.add_done_callback(_broadcast_done)
    await asyncio.shield(task)


async def _run_exec_stream(
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
    locks: WorkspaceLockTable,
    send: Callable[[str], Coroutine[Any, Any, None]],
    broadcaster=None,
) -> None:
    """Task body: drive :func:`run_exec` and forward its frames via *send*.

    Preserves the original inline framing exactly — streaming
    ``stdout``/``stderr``/``exit`` events in order, an ``OpError`` mapped to a
    single-shot ``ok=false`` Response, and any other exception mapped to
    ``EINTERNAL`` — the only change is that this now runs off the message loop
    as its own task.  Per-req_id output ordering is unchanged (a single task
    awaits each ``send`` in turn).
    """
    agen = run_exec(req_id, args, workspace_root, locks)
    # Lifecycle broadcast (EVENTS_SUBSCRIBE subscribers): started before
    # the stream, exited with the exit event's code. Never raises.
    import time as _time

    cmd = list(args.get("cmd") or [])
    t0 = _time.monotonic()
    exit_code = None
    if broadcaster is not None:
        await broadcaster.broadcast("exec_started", {
            "cmd": cmd, "workdir": args.get("workdir"),
        })
    try:
        async for event in agen:
            if event.event == "exit" and isinstance(event.data, dict):
                exit_code = event.data.get("code")
            await send(serialize(event))
    except OpError as exc:
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": exc.code, "message": exc.message},
        )))
    except asyncio.CancelledError:
        # WS close / cancel_all: close the generator so run_exec's own
        # CancelledError/GeneratorExit teardown reaps the subprocess and
        # cancels its reader tasks, then propagate the cancellation.
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error handling exec op")
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": ErrorCode.EINTERNAL, "message": str(exc)},
        )))
    finally:
        # Idempotent: a no-op if the generator already ran to completion or
        # unwound via an exception; on the cancel-at-send window it throws
        # GeneratorExit into run_exec so its teardown (reader-task cancel +
        # subprocess terminate/reap) still runs.
        await agen.aclose()
        if broadcaster is not None:
            await _announce(broadcaster, "exec_exited", {
                "cmd": cmd,
                "exit_code": exit_code,
                "duration_ms": max(0, int((_time.monotonic() - t0) * 1000)),
            })


def start_exec(
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
    locks: WorkspaceLockTable,
    send: Callable[[str], Coroutine[Any, Any, None]],
    registry: ExecRegistry,
    broadcaster=None,
) -> asyncio.Task[None]:
    """Spawn a tracked exec stream task; returns it (tests await it).

    The task is registered in *registry* under *req_id* so ``cancel`` (the
    ``exec_cancel`` op) and ``cancel_all`` (WS close) can reach it, and a
    done-callback deregisters it on completion.
    """
    task = asyncio.create_task(
        _run_exec_stream(
            req_id, args, workspace_root, locks, send,
            broadcaster=broadcaster,
        ),
        name=f"exec:{req_id}",
    )
    registry.add(req_id, task)
    task.add_done_callback(lambda done: registry.discard(req_id, done))
    return task
