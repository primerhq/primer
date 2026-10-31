"""In-process PTY host for local-workspace terminals (Studio spec §6.5).

Hosts an interactive pseudo-terminal directly in the API process for
:class:`~primer.workspace.local.workspace.LocalWorkspace` terminals — the
local backend has no WebSocket runtime, so the PTY lives here (cwd = the
workspace root) instead of being proxied to a container.

This is a self-contained mirror of ``primer_runtime.pty_op`` — the runtime
package is intentionally NOT imported (the two packages stay decoupled; a
little duplication is the price).  The mechanics are identical: spawn a
login shell attached to an ``os.openpty`` master/slave pair, make the slave
the controlling terminal (job control), and drain the master via
``loop.add_reader`` into a queue.

Availability (spec §13 open question): the terminal is **default-enabled**
for every workspace in v1 — there is no per-workspace / per-role enable
toggle yet.  The endpoint that hosts this session is auth-gated
(``require_auth_ws``); the workspace is the sandbox boundary.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import shutil
import signal
import struct
import termios
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger(__name__)

_READ_CHUNK = 65536

# High-water mark for the master-fd -> WebSocket output queue. Each chunk is
# up to ``_READ_CHUNK`` (64 KiB), so this caps buffered terminal output at
# ~16 MiB. A runaway child (``yes``, a chatty build) paired with a slow or
# blocked WebSocket consumer would otherwise grow the queue without bound.
# When the cap is hit we drop the OLDEST buffered chunk (see
# ``_enqueue_output``) — losing the stalest terminal output is the acceptable
# degradation. Mirrors ``primer_runtime.pty_op``.
_OUTPUT_QUEUE_MAX_CHUNKS = 256

# Cap on stdin bytes queued for a program that is not reading them (P3-N1). The master fd is non-blocking, so a paste
# the pty cannot take yet waits here and the loop's writer callback drains it; a paste that would push the queue past
# the cap is dropped whole with a warning. Mirrors ``primer_runtime.pty_op``, which refuses it instead.
_STDIN_PENDING_MAX = 1024 * 1024


def _enqueue_output(queue: "asyncio.Queue[bytes]", data: bytes) -> bool:
    """Put *data* on the bounded output *queue*, dropping the OLDEST buffered
    chunk when the queue is already at its high-water mark.

    Returns ``True`` iff a chunk was dropped. Drop-oldest keeps memory bounded
    when a slow/blocked WebSocket consumer can't keep up with a runaway child.
    The empty EOF sentinel is never starved out: a slot is always freed before
    the re-put, so the terminating ``b""`` always lands.
    """
    try:
        queue.put_nowait(data)
        return False
    except asyncio.QueueFull:
        dropped = False
        try:
            queue.get_nowait()
            dropped = True
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            pass
        return dropped


#: What the shell keeps of the API server's environment (review W43, lead ruling D9); every other server variable (database
#: URLs, secret and provider keys) stays out. The workspace's own env is added on top.
_SHELL_ENV_KEPT = frozenset({"PATH", "HOME", "LANG", "TERM", "USER", "SHELL", "TZ"})


def _shell_env(workspace_env: dict[str, str]) -> dict[str, str]:
    """The terminal shell's environment: the kept server variables (``LC_*`` included), the workspace env, a default TERM."""
    env = {k: v for k, v in os.environ.items() if k in _SHELL_ENV_KEPT or k.startswith("LC_")}
    env.update(workspace_env)
    env.setdefault("TERM", "xterm-256color")
    return env


def _default_shell() -> list[str]:
    """Return the argv for a login shell: ``$SHELL`` → bash → sh, ``-l``."""
    shell = os.environ.get("SHELL")
    if not shell:
        shell = shutil.which("bash") or "/bin/bash"
        if not os.path.exists(shell):
            shell = "/bin/sh"
    return [shell, "-l"]


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Apply a terminal window size to *fd* via ``TIOCSWINSZ``.

    Clamps to [1, 1000] (the API endpoint's query bounds): resize values
    arrive from client control frames, and ``struct.pack("HHHH")`` raises
    ``struct.error`` — NOT OSError — outside uint16, which previously
    escaped the ``except OSError`` resize guard.
    """
    cols = max(1, min(int(cols), 1000))
    rows = max(1, min(int(rows), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class LocalPtySession:
    """A single interactive PTY rooted at a local workspace directory.

    Lifecycle::

        session = LocalPtySession(root=ws.root, cols=80, rows=24)
        await session.start()
        async for chunk in session.output():   # pty stdout bytes
            ...                                 # forward to the browser
        await session.write(b"ls\\n")            # browser stdin -> pty
        await session.resize(120, 40)
        await session.close()                   # torn down on disconnect
        session.exit_code                       # child return code (or -1)

    ``output()`` yields raw bytes until the child exits (EOF), then returns;
    :attr:`exit_code` is populated once it returns (or after :meth:`close`).
    """

    def __init__(
        self,
        *,
        root: Path,
        cols: int = 80,
        rows: int = 24,
        env: dict[str, str] | None = None,
        cmd: list[str] | None = None,
    ) -> None:
        self._root = Path(root)
        self._cols = cols
        self._rows = rows
        self._env = env or {}
        self._cmd = cmd or _default_shell()

        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=_OUTPUT_QUEUE_MAX_CHUNKS
        )
        self._dropped_chunks = 0
        self._pending = bytearray()  # stdin the pty could not take yet, drained by the loop's writer callback
        self._exit_code: int | None = None
        self._started = False
        self._finalized = False

    @property
    def exit_code(self) -> int | None:
        """The child's return code, available once the PTY has closed."""
        return self._exit_code

    async def start(self) -> None:
        """Spawn the shell attached to a fresh pty and begin draining it."""
        if self._started:
            return
        self._started = True

        proc_env = _shell_env(self._env)

        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)  # a write the pty cannot take must never block the API loop (P3-N1)
        self._master_fd = master_fd

        def _child_preexec() -> None:
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            _set_winsize(master_fd, self._cols, self._rows)
            self._proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self._root),
                env=proc_env,
                preexec_fn=_child_preexec,
            )
        finally:
            os.close(slave_fd)

        loop = asyncio.get_running_loop()

        def _on_readable() -> None:
            try:
                data = os.read(master_fd, _READ_CHUNK)
            except BlockingIOError:
                return  # nothing to read after all; not EOF
            except OSError:
                data = b""  # EIO on Linux after the child exits == EOF
            if _enqueue_output(self._queue, data):
                self._dropped_chunks += 1
            if not data:
                if self._dropped_chunks:
                    logger.warning(
                        "local pty dropped %d output chunk(s): consumer "
                        "could not keep up (queue capped at %d chunks)",
                        self._dropped_chunks, _OUTPUT_QUEUE_MAX_CHUNKS,
                    )
                loop.remove_reader(master_fd)

        loop.add_reader(master_fd, _on_readable)

    async def output(self) -> AsyncIterator[bytes]:
        """Yield pty output bytes until the child exits, then finalise."""
        while True:
            chunk = await self._queue.get()
            if not chunk:
                await self._finalize()
                return
            yield chunk

    async def write(self, data: bytes) -> None:
        """Write *data* (browser stdin) to the pty master without ever blocking the event loop.

        The master fd is non-blocking: what the pty takes now is written (looping over short writes), and the rest
        waits in ``_pending`` for the writer callback, so input stays in order and a program that is not reading
        holds up only its own terminal (P3-N1). Input that would push the queue past ``_STDIN_PENDING_MAX`` is
        dropped whole with a warning. ``OSError`` means the pty is gone (child exited), so the write is dropped.
        """
        if self._master_fd is None or self._finalized:
            return
        if len(self._pending) + len(data) > _STDIN_PENDING_MAX:
            logger.warning(
                "local pty dropped %d stdin bytes: %d are already waiting for the program to read them (cap %d)",
                len(data), len(self._pending), _STDIN_PENDING_MAX,
            )
            return
        if self._pending:
            self._pending += data  # behind what is already queued
            return
        mv = memoryview(data)
        while mv:
            try:
                written = os.write(self._master_fd, mv)
            except BlockingIOError:
                self._pending += mv
                asyncio.get_running_loop().add_writer(self._master_fd, self._drain_stdin)
                return
            except OSError:
                return
            if written <= 0:
                return
            mv = mv[written:]

    def _drain_stdin(self) -> None:
        """Writer callback: hand the pty what it takes of the queued input; stop watching once it is all written."""
        master_fd = self._master_fd
        if master_fd is None:
            return
        try:
            written = os.write(master_fd, self._pending)
        except BlockingIOError:
            return
        except OSError:
            written = len(self._pending)  # pty gone: drop the rest
        del self._pending[:written]
        if not self._pending:
            asyncio.get_running_loop().remove_writer(master_fd)

    async def resize(self, cols: int, rows: int) -> None:
        """Change the terminal window size."""
        self._cols, self._rows = cols, rows
        if self._master_fd is None or self._finalized:
            return
        try:
            _set_winsize(self._master_fd, cols, rows)
        except (OSError, ValueError, TypeError):
            # OSError: pty gone. ValueError/TypeError: garbage cols/rows
            # from a malformed control frame — drop, never propagate.
            pass

    async def close(self) -> None:
        """Terminate the child and release the pty (idempotent)."""
        await self._finalize()

    async def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        master_fd = self._master_fd
        loop = asyncio.get_running_loop()
        if master_fd is not None:
            try:
                loop.remove_reader(master_fd)
                loop.remove_writer(master_fd)
            except (ValueError, OSError):
                pass
            self._pending.clear()

        proc = self._proc
        if proc is not None:
            if proc.returncode is None:
                # SIGHUP is the "terminal closed" signal an interactive shell
                # honours (SIGTERM is ignored by interactive shells).
                #
                # killpg ONLY when the child is already its own process-group
                # leader (pgid == pid, i.e. its post-fork setsid completed).
                # Before that instant the child still shares OUR process
                # group — killpg(getpgid(child)) would SIGHUP this whole
                # process (it killed pytest-xdist workers intermittently).
                try:
                    pgid = os.getpgid(proc.pid)
                    if pgid == proc.pid:
                        os.killpg(pgid, signal.SIGHUP)
                    else:
                        proc.send_signal(signal.SIGHUP)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.send_signal(signal.SIGHUP)
                    except ProcessLookupError:
                        pass
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
            self._exit_code = proc.returncode if proc.returncode is not None else -1
        else:
            self._exit_code = -1

        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
            self._master_fd = None


__all__ = ["LocalPtySession"]
