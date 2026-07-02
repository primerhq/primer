"""Interactive PTY op handler for the workspace runtime.

Spawns a login shell (or an arbitrary command) attached to a
pseudo-terminal, streams the master-side output to the caller as
:class:`~protocol.Event` frames, and accepts stdin / window-size changes
via single-shot control requests.  This is the runtime half of the Studio
integrated terminal (spec §6.5).

Protocol
--------
Open (long-lived streaming op)::

    {"req_id": 9, "op": "pty_open",
     "args": {"cmd": ["/bin/bash", "-l"], "cols": 80, "rows": 24,
              "workdir": "sub/dir", "env": {"FOO": "bar"}}}

Outgoing (streaming)::

    {"req_id": 9, "event": "pty_open"}                         ← shell ready
    {"req_id": 9, "event": "data", "data": {"data_b64": ...}}  ← pty output
    ...
    {"req_id": 9, "event": "exit", "data": {"code": 0}}        ← child exited

Control (single-shot; reference the open op via ``target_req_id``)::

    {"req_id": 10, "op": "pty_stdin",  "args": {"target_req_id": 9, "data_b64": ...}}
    {"req_id": 11, "op": "pty_resize", "args": {"target_req_id": 9, "cols": 120, "rows": 40}}
    {"req_id": 12, "op": "pty_close",  "args": {"target_req_id": 9}}

Each control op replies with a single-shot :class:`~protocol.Response`
(``ok=True`` on success, or an ``OpError``/``ENOENT`` for an unknown
target).  This mirrors the ``watch_start`` / ``watch_cancel`` split — the
long-lived op streams events while short control requests act on it.

Spawning + reading
------------------
The PTY is created with :func:`os.openpty` and the child is launched via
``asyncio.create_subprocess_exec(stdin=stdout=stderr=slave, start_new_session=True)``.
The master fd is drained with ``loop.add_reader`` (fully non-blocking, no
executor threads) into an :class:`asyncio.Queue` that a single per-session
task forwards in order so terminal output never re-orders.  On child exit
the master read returns EOF (or ``EIO`` on Linux), the child is reaped, and
the final ``exit`` event carries its return code.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import logging
import os
import pty
import shutil
import signal
import struct
import termios
from collections.abc import Callable, Coroutine
from typing import Any

from primer_runtime.ops import OpError, _resolve_safe
from primer_runtime.protocol import ErrorCode, Event, serialize

log = logging.getLogger(__name__)

_READ_CHUNK = 65536


def _default_shell() -> list[str]:
    """Return the argv for a login shell: ``$SHELL`` → bash → sh, ``-l``."""
    shell = os.environ.get("SHELL")
    if not shell:
        shell = shutil.which("bash") or "/bin/bash"
        if not os.path.exists(shell):
            shell = "/bin/sh"
    return [shell, "-l"]


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Apply a terminal window size to *fd* via the ``TIOCSWINSZ`` ioctl.

    Clamps to [1, 1000] (matching the API endpoint's query bounds): the
    values arrive from client control frames, and ``struct.pack("HHHH")``
    raises ``struct.error`` — NOT OSError — for values outside uint16, which
    previously escaped the resize guards and could kill the runtime
    connection's message loop.
    """
    cols = max(1, min(int(cols), 1000))
    rows = max(1, min(int(rows), 1000))
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


# ---------------------------------------------------------------------------
# Per-session state + registry
# ---------------------------------------------------------------------------


class PtySession:
    """One live PTY: the forwarding task, the master fd, and the child proc."""

    def __init__(
        self,
        req_id: int,
        master_fd: int,
        proc: asyncio.subprocess.Process,
    ) -> None:
        self.req_id = req_id
        self.master_fd = master_fd
        self.proc = proc
        self.task: asyncio.Task[None] | None = None
        self._closed = False

    def write_stdin(self, data: bytes) -> None:
        """Write *data* to the master fd (child stdin)."""
        if self._closed:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            # The pty is gone (child exited between frames) — drop the write;
            # the reader task will emit ``exit`` and self-deregister.
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Change the terminal window size."""
        if self._closed:
            return
        try:
            _set_winsize(self.master_fd, cols, rows)
        except (OSError, ValueError, TypeError):
            # OSError: pty gone. ValueError/TypeError: garbage cols/rows
            # from a malformed control frame — drop, never propagate.
            pass

    def terminate(self) -> None:
        """Signal the child to exit (SIGHUP to its session group).

        The child is a session leader whose controlling terminal is the pty
        slave, so a hang-up (SIGHUP) is the semantically-correct "terminal
        closed" signal — and, unlike SIGTERM, an *interactive* shell honours
        it and exits.  The child closing the slave makes the master read
        return EOF, which drives the reader task's teardown (reap + ``exit``
        event + master close).
        """
        if self.proc.returncode is not None:
            return
        # killpg ONLY when the child is already its own process-group leader
        # (pgid == pid, i.e. its post-fork setsid completed). Before that
        # instant the child still shares OUR process group —
        # killpg(getpgid(child)) would SIGHUP this whole process (it killed
        # pytest-xdist workers intermittently when terminate() raced the
        # child's setsid).
        try:
            pgid = os.getpgid(self.proc.pid)
            if pgid == self.proc.pid:
                os.killpg(pgid, signal.SIGHUP)
            else:
                self.proc.send_signal(signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.send_signal(signal.SIGHUP)
            except ProcessLookupError:
                pass


class PtyRegistry:
    """Per-connection registry mapping req_id → live :class:`PtySession`."""

    def __init__(self) -> None:
        self._sessions: dict[int, PtySession] = {}

    def add(self, session: PtySession) -> None:
        self._sessions[session.req_id] = session

    def get(self, req_id: int) -> PtySession | None:
        return self._sessions.get(req_id)

    def remove(self, req_id: int) -> None:
        self._sessions.pop(req_id, None)

    def write_stdin(self, target_req_id: int, data: bytes) -> bool:
        session = self._sessions.get(target_req_id)
        if session is None:
            return False
        session.write_stdin(data)
        return True

    def resize(self, target_req_id: int, cols: int, rows: int) -> bool:
        session = self._sessions.get(target_req_id)
        if session is None:
            return False
        session.resize(cols, rows)
        return True

    def close(self, target_req_id: int) -> bool:
        session = self._sessions.get(target_req_id)
        if session is None:
            return False
        session.terminate()
        return True

    def cancel_all(self) -> None:
        """Terminate every live PTY (called on WS close)."""
        for session in list(self._sessions.values()):
            session.terminate()
            if session.task is not None:
                session.task.cancel()
        self._sessions.clear()


# ---------------------------------------------------------------------------
# Session coroutine
# ---------------------------------------------------------------------------


async def _run_pty(
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
    send: Callable[[str], Coroutine[Any, Any, None]],
    registry: PtyRegistry,
) -> None:
    """Task body: spawn the PTY, stream output, reap on exit.

    On a bad workdir / spawn failure a single-shot ``ok=false`` Response is
    sent (the runtime client routes it into the open op's stream queue so
    the consumer surfaces the error).  On success ``pty_open`` is emitted,
    followed by ``data`` chunks and a final ``exit`` event.
    """
    from primer_runtime.protocol import Response

    # ---- Validate + resolve args ----------------------------------------
    cmd: list[str] = args.get("cmd") or _default_shell()
    if not isinstance(cmd, list) or not cmd:
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": ErrorCode.EPROTOCOL, "message": "pty_open: 'cmd' must be a non-empty list"},
        )))
        return
    cols = int(args.get("cols") or 80)
    rows = int(args.get("rows") or 24)
    workdir_raw: str | None = args.get("workdir")
    env_extra: dict[str, str] | None = args.get("env")

    try:
        if workdir_raw is not None:
            workdir = str(_resolve_safe(workdir_raw, workspace_root))
        else:
            workdir = workspace_root
    except OpError as exc:
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": exc.code, "message": exc.message},
        )))
        return
    except Exception as exc:  # noqa: BLE001 — protocol boundary: the client
        # is waiting on this op; an uncaught exception here would leave it
        # hanging with no frame at all. Map anything unexpected to EINTERNAL.
        log.exception("pty_open validation failed unexpectedly (req_id=%d)", req_id)
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": ErrorCode.EINTERNAL, "message": f"pty_open: {exc}"},
        )))
        return

    proc_env = dict(os.environ)
    if env_extra:
        proc_env.update(env_extra)
    # Terminal apps need a TERM; default to a widely-supported value.
    proc_env.setdefault("TERM", "xterm-256color")

    # ---- Spawn the child attached to a pty ------------------------------
    master_fd, slave_fd = pty.openpty()

    def _child_preexec() -> None:
        # Start a new session and make the pty slave the controlling
        # terminal so the shell gets proper job control (Ctrl-C, SIGHUP on
        # close, foreground process groups). Runs post-fork in the child.
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except OSError:
            pass

    try:
        _set_winsize(master_fd, cols, rows)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workdir,
            env=proc_env,
            preexec_fn=_child_preexec,
        )
    except (OSError, ValueError) as exc:
        os.close(master_fd)
        os.close(slave_fd)
        await send(serialize(Response(
            req_id=req_id, ok=False,
            error={"code": ErrorCode.EINTERNAL, "message": f"pty_open: spawn failed: {exc}"},
        )))
        return

    # Parent keeps only the master end.
    os.close(slave_fd)

    session = PtySession(req_id, master_fd, proc)
    session.task = asyncio.current_task()
    registry.add(session)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, _READ_CHUNK)
        except OSError:
            data = b""  # EIO on Linux after the child exits == EOF
        queue.put_nowait(data)
        if not data:
            loop.remove_reader(master_fd)

    # Announce readiness, then start draining the master fd.
    await send(serialize(Event(req_id=req_id, event="pty_open")))
    loop.add_reader(master_fd, _on_readable)

    try:
        while True:
            chunk = await queue.get()
            if not chunk:
                break  # EOF: child closed the pty (exited)
            await send(serialize(Event(
                req_id=req_id,
                event="data",
                data={"data_b64": base64.b64encode(chunk).decode()},
            )))
    except asyncio.CancelledError:
        # WS disconnect / cancel_all — fall through to teardown, re-raise
        # after cleanup so the cancellation propagates.
        session._closed = True
        loop.remove_reader(master_fd)
        session.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()  # reap — mirrors the normal-teardown path
            except ProcessLookupError:
                pass
        _safe_close(master_fd)
        registry.remove(req_id)
        raise
    except Exception:
        log.exception("Unexpected error in PTY session req_id=%d", req_id)
    finally:
        loop.remove_reader(master_fd)

    # ---- Normal teardown: reap the child, emit exit ---------------------
    session._closed = True
    if proc.returncode is None:
        session.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
    _safe_close(master_fd)
    registry.remove(req_id)

    code = proc.returncode if proc.returncode is not None else -1
    try:
        await send(serialize(Event(req_id=req_id, event="exit", data={"code": code})))
    except Exception:  # noqa: BLE001 — ws may already be closed
        pass


def _safe_close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public helper called from server.py
# ---------------------------------------------------------------------------


def start_pty(
    req_id: int,
    args: dict[str, Any],
    workspace_root: str,
    send: Callable[[str], Coroutine[Any, Any, None]],
    registry: PtyRegistry,
) -> asyncio.Task[None]:
    """Spawn a PTY session task for a ``pty_open`` op; returns the task.

    The task registers itself in *registry* under *req_id*; the control ops
    (``pty_stdin`` / ``pty_resize`` / ``pty_close``) look it up by
    ``target_req_id``. The server ignores the returned task (the registry
    owns lifecycle); tests await it for deterministic completion.
    """
    # The task binds itself onto its PtySession (via asyncio.current_task)
    # right after the spawn, so cancel_all() can reach it. A done-callback
    # is a belt-and-suspenders deregister in case the body raised before its
    # own teardown ran (req_ids are unique per connection, so no reuse race).
    task = asyncio.create_task(
        _run_pty(req_id, args, workspace_root, send, registry),
        name=f"pty:{req_id}",
    )
    task.add_done_callback(lambda t: registry.remove(req_id))
    return task
