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
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
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

        proc_env = dict(os.environ)
        proc_env.update(self._env)
        proc_env.setdefault("TERM", "xterm-256color")

        master_fd, slave_fd = pty.openpty()
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
            except OSError:
                data = b""  # EIO on Linux after the child exits == EOF
            self._queue.put_nowait(data)
            if not data:
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
        """Write *data* (browser stdin) to the pty master."""
        if self._master_fd is None or self._finalized:
            return
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass

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
            except (ValueError, OSError):
                pass

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
