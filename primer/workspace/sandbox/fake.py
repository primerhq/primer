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
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from primer.int.sandbox import (
    ExecResult,
    FileStat,
    Sandbox,
    SandboxInspectInfo,
)
from primer.workspace._locks import WorkspaceLockTable


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


def _parse_trailers(message: str) -> dict[str, str]:
    """Extract ``Key: Value`` trailer lines from a git commit message.

    Used by :meth:`FakeSandbox.state_history` to surface the
    ``X-Primer-*`` trailers (workspace / session / agent / op) the state
    repo embeds in every commit, matching the flat shape the real runtime
    returns. Only lines of the form ``Key: Value`` are recognised.
    """
    trailers: dict[str, str] = {}
    for line in message.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            trailers[key] = value
    return trailers


class FakeSandbox(Sandbox):
    """In-process Sandbox impl backed by a host tempdir.

    Used by every test that wants to exercise sandbox-tool / state /
    cache code without standing up a real container. Production code
    should not depend on this; it's exposed only because tests dispatch
    real workspace tools through it.

    Satisfies the :class:`~primer.workspace.sandbox.state._StateCapableSandbox`
    structural protocol so that :meth:`SandboxStateRepo.initialize` skips
    the exec-based git init (which would fail on host paths). The state ops
    are in-memory stubs that return plausible no-op results.
    """

    #: Protocol version advertised to SandboxStateRepo.  Set >= "1.1" so
    #: _require_state_ops() accepts this sandbox in tests that exercise the
    #: full state-op surface.
    protocol_version: str = "1.1"

    def __init__(self, root: Path, *, sandbox_id: str = "fake") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._id = sandbox_id
        self._stopped = False
        self._started_at = datetime.now(tz=timezone.utc)
        # Mirrors the runtime's advisory write-lock table so unit tests can
        # observe same-workdir write-exec serialization without a container.
        self._locks = WorkspaceLockTable()
        # In-memory state store: path -> bytes.  Used by the stub state ops.
        self._state_files: dict[str, bytes] = {}
        self._state_commits: list[dict] = []

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
        access: str = "write",
        writes: list[str] | None = None,
    ) -> ExecResult:
        cwd = self._host_path(workdir)
        if access == "read":
            lock_ctx = nullcontext()
        elif writes:
            lock_ctx = self._locks.hold_paths(
                [str(self._host_path(w)) for w in writes]
            )
        else:
            lock_ctx = self._locks.hold_scope(str(cwd))
        async with lock_ctx:
            return await self._exec_unlocked(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
                stdin=stdin,
                abort=abort,
            )

    async def _exec_unlocked(
        self,
        command,
        *,
        cwd: Path,
        env: dict[str, str] | None,
        timeout_seconds: float | None,
        stdin: bytes | None,
        abort: asyncio.Event | None,
    ) -> ExecResult:
        if self._stopped:
            raise RuntimeError("sandbox is stopped")
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
        async with self._locks.hold_write(str(target.parent), str(target)):
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

        def _append() -> None:
            with target.open("ab") as fh:
                fh.write(content)

        async with self._locks.hold_write(str(target.parent), str(target)):
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_append)

    async def make_dir(self, path: str) -> None:
        target = self._host_path(path)
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)

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

    # ------------------------------------------------------------------
    # _StateCapableSandbox stub ops
    # ------------------------------------------------------------------

    async def state_commit(
        self,
        *,
        files: dict[str, bytes],
        deletes: list[str],
        message: str,
        allow_empty: bool = False,
    ) -> str:
        """In-memory state commit stub.

        Stores files in ``_state_files``, removes deleted paths, and
        records a commit entry.  Returns a deterministic fake SHA derived
        from the current commit count.
        """
        for path, content in files.items():
            self._state_files[path] = content
        for path in deletes:
            self._state_files.pop(path, None)
        sha = f"{len(self._state_commits):040x}"
        self._state_commits.append(
            {"sha": sha, "message": message, "files": list(files)}
        )
        return sha

    async def state_read(self, paths: list[str]) -> dict[str, bytes | None]:
        """Return the in-memory content for the requested paths."""
        return {p: self._state_files.get(p) for p in paths}

    async def state_history(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return in-memory commit history (newest first, up to *limit*).

        Mirrors the real runtime's *flat* commit shape: the
        ``X-Primer-Session`` / ``X-Primer-Agent`` / ``X-Primer-Op`` trailers
        embedded in the commit message are parsed out into top-level
        ``session_id`` / ``agent_id`` / ``op`` fields, and the optional
        ``session_id`` / ``agent_id`` filters are honoured. This keeps the
        stub faithful enough for session-rehydration tests that drive
        ``create_session`` and then expect ``list_session_ids`` to recover
        the persisted ids.
        """
        out: list[dict] = []
        for commit in reversed(self._state_commits):
            trailers = _parse_trailers(commit.get("message", ""))
            sid = trailers.get("X-Primer-Session")
            aid = trailers.get("X-Primer-Agent")
            if session_id is not None and sid != session_id:
                continue
            if agent_id is not None and aid != agent_id:
                continue
            enriched = dict(commit)
            if sid is not None:
                enriched["session_id"] = sid
            if aid is not None:
                enriched["agent_id"] = aid
            if "X-Primer-Workspace" in trailers:
                enriched["workspace_id"] = trailers["X-Primer-Workspace"]
            if "X-Primer-Op" in trailers:
                enriched["op"] = trailers["X-Primer-Op"]
            out.append(enriched)
            if len(out) >= limit:
                break
        return out


__all__ = ["FakeSandbox"]
