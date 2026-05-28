"""FakeSandbox -- in-process :class:`Sandbox` implementation for tests + dev.

Backs ops with a host ``tempfile.TemporaryDirectory`` (caller-supplied).
Maps every ``/workspace/...`` path to ``<root>/...``. ``exec`` runs via
``asyncio.create_subprocess_shell`` with ``cwd=<root>/<workdir-stripped>``.

This file is production code (importable from non-test code) but is
intended only for tests and single-process dev. It is excluded from the
coverage gate the same way ``InMemoryScheduler`` is.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from matrix.int.sandbox import (
    ExecResult,
    FileStat,
    Sandbox,
    SandboxInspectInfo,
)


_TAR_CHUNK_BYTES = 64 * 1024
_WORKSPACE_PREFIX = "/workspace"


def _strip_leading_slash(path: str) -> str:
    """Convert ``/foo/bar`` to ``foo/bar``; leave relative paths alone."""
    return path.lstrip("/")


def _find_posix_shell() -> str | None:
    """Find a POSIX-style shell for running shell-string commands.

    Production sandboxes run Linux shells inside containers. FakeSandbox
    is a test seam; on Windows we look for git-for-windows' bash so
    POSIX shell scripts in tests behave the same as in real sandboxes.
    Returns the shell path or ``None`` if no POSIX shell is found
    (caller falls back to the OS default).
    """
    if os.name != "nt":
        # POSIX systems: just use ``sh`` via shell=True semantics.
        return None
    # Windows: prefer bash from git-for-windows.
    for candidate in ("bash", "bash.exe"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    for candidate in ("sh.exe", "sh"):
        path = shutil.which(candidate)
        if path is not None:
            return path
    return None


_POSIX_SHELL = _find_posix_shell()


class FakeSandbox(Sandbox):
    """In-process Sandbox impl backed by a host tempdir.

    Used by every test that wants to exercise sandbox-tool / state /
    cache code without standing up a real container. Production code
    should not depend on this; it's exposed only because tests dispatch
    real workspace tools through it.
    """

    def __init__(self, root: Path, *, sandbox_id: str = "fake") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._id = sandbox_id
        self._stopped = False
        self._started_at = datetime.now(tz=timezone.utc)

    @property
    def id(self) -> str:
        return self._id

    def _host_path(self, sandbox_path: str) -> Path:
        """Map ``/workspace/foo`` and friends to ``<root>/foo``."""
        rel = _strip_leading_slash(sandbox_path)
        # Strip the leading "workspace/" so the agent's view of /workspace
        # maps onto the tempdir root.
        if rel == "workspace":
            return self._root
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/"):]
        if not rel:
            return self._root
        return (self._root / rel)

    async def exec(
        self,
        command,
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        stdin: bytes | None = None,
        abort: asyncio.Event | None = None,
    ) -> ExecResult:
        if self._stopped:
            raise RuntimeError("sandbox is stopped")
        cwd = self._host_path(workdir)
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
        start = time.perf_counter()

        if isinstance(command, list):
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        elif _POSIX_SHELL is not None:
            # Force a POSIX shell so test scripts written for real
            # (Linux-container) sandboxes behave the same way locally.
            proc = await asyncio.create_subprocess_exec(
                _POSIX_SHELL, "-c", command, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        async def _abort_waiter() -> None:
            if abort is None:
                return
            await abort.wait()
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        abort_task = asyncio.create_task(_abort_waiter()) if abort else None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=timeout_seconds,
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        finally:
            if abort_task is not None:
                abort_task.cancel()

        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_seconds=time.perf_counter() - start,
        )

    async def read_file(self, path: str) -> bytes:
        target = self._host_path(path)
        return await asyncio.to_thread(target.read_bytes)

    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None,
    ) -> None:
        target = self._host_path(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, content)
        if mode is not None:
            try:
                await asyncio.to_thread(target.chmod, mode)
            except (OSError, NotImplementedError):
                pass

    async def append_file(self, path: str, content: bytes) -> None:
        """Atomically append ``content`` to ``path`` (fast O_APPEND path)."""
        target = self._host_path(path)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

        def _append() -> None:
            with target.open("ab") as fh:
                fh.write(content)

        await asyncio.to_thread(_append)

    async def list_dir(self, path: str) -> list[FileStat]:
        host = self._host_path(path)
        if not await asyncio.to_thread(host.is_dir):
            return []
        entries = await asyncio.to_thread(
            lambda: sorted(host.iterdir(), key=lambda p: p.name)
        )
        return [self._stat_from_path(entry, host) for entry in entries]

    async def stat(self, path: str) -> FileStat | None:
        host = self._host_path(path)
        if not await asyncio.to_thread(host.exists):
            return None
        return self._stat_from_path(host, self._root)

    def _stat_from_path(self, host: Path, anchor: Path) -> FileStat:
        st = host.stat()
        if host.is_symlink():
            kind = "symlink"
        elif host.is_dir():
            kind = "dir"
        else:
            kind = "file"
        try:
            rel = host.relative_to(anchor).as_posix()
        except ValueError:
            rel = host.name
        return FileStat(
            path=rel,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=st.st_size if kind == "file" else 0,
            mode=st.st_mode & 0o7777,
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
        )

    async def delete(self, path: str) -> None:
        target = self._host_path(path)
        if not await asyncio.to_thread(target.exists):
            return
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target)
        else:
            await asyncio.to_thread(target.unlink)

    async def archive(self, paths: list[str]) -> AsyncIterator[bytes]:
        members = [self._host_path(p) for p in paths]
        buf = io.BytesIO()

        def _build() -> None:
            with tarfile.open(fileobj=buf, mode="w") as tf:
                for m in members:
                    if not m.exists():
                        continue
                    tf.add(str(m), arcname=m.name, recursive=True)

        await asyncio.to_thread(_build)
        buf.seek(0)
        while True:
            chunk = buf.read(_TAR_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    async def inspect(self) -> SandboxInspectInfo:
        return SandboxInspectInfo(
            state="stopped" if self._stopped else "running",
            started_at=self._started_at,
        )

    async def stop(self) -> None:
        self._stopped = True

    async def remove(self) -> None:
        self._stopped = True
        await asyncio.to_thread(shutil.rmtree, self._root, ignore_errors=True)


__all__ = ["FakeSandbox"]
