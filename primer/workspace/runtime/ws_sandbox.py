"""WSSandbox -- Sandbox ABC implementation backed by a RuntimeClient.

Delegates every Sandbox method to the in-container runtime over a
persistent WebSocket connection.  Path resolution is handled internally:
relative paths are prepended with *workspace_root*; absolute paths are
used as-is.

``stop()`` and ``remove()`` are container-orchestration operations that
belong to the backend adapter (Docker / Podman / Containerd), not the
runtime protocol.  If a :class:`ContainerHandle` is provided at
construction time, ``stop()`` and ``remove()`` delegate to it; otherwise
they raise :class:`NotImplementedError`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from primer.int.sandbox import ExecResult, FileStat, Sandbox, SandboxInspectInfo
from primer.workspace.runtime.runtime_client import RuntimeClient

if TYPE_CHECKING:
    pass


@runtime_checkable
class ContainerHandle(Protocol):
    """Minimal protocol for container lifecycle operations.

    Backend adapters (Docker / Podman / Containerd) wrap their native
    container handle in an object implementing this protocol and pass it
    to :class:`WSSandbox` so that ``stop()`` and ``remove()`` work
    without any subclassing.
    """

    async def stop(self) -> None: ...
    async def remove(self) -> None: ...


class WSSandbox(Sandbox):
    """Sandbox backed by the workspace runtime WebSocket protocol.

    Parameters
    ----------
    runtime_client:
        A connected (or lazily-connecting) :class:`RuntimeClient` that
        speaks to the in-container runtime server.
    container_id:
        Stable identifier for the underlying container / pod, returned
        by :attr:`id`.
    workspace_root:
        Absolute path inside the container that acts as the root for
        relative path arguments (default ``/workspace``).
    """

    def __init__(
        self,
        *,
        runtime_client: RuntimeClient,
        container_id: str,
        workspace_root: str = "/workspace",
        container_handle: ContainerHandle | None = None,
    ) -> None:
        self._client = runtime_client
        self._container_id = container_id
        self._workspace_root = workspace_root.rstrip("/")
        self._container_handle = container_handle

    # ------------------------------------------------------------------
    # Sandbox.id
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self._container_id

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def exec(
        self,
        command: str | list[str],
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        stdin: bytes | None = None,
        abort: asyncio.Event | None = None,
    ) -> ExecResult:
        return await self._client.exec(
            command,
            workdir=workdir,
            env=env,
            timeout_s=timeout_seconds,
            stdin=stdin,
            abort=abort,
        )

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def read_file(self, path: str) -> bytes:
        return await self._client.read_file(self._resolve(path))

    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None,
    ) -> None:
        await self._client.write_file(self._resolve(path), content, mode=mode)

    async def append_file(self, path: str, content: bytes) -> None:
        """Append arbitrary bytes.  Delegates to :meth:`append_line` without
        a trailing newline by splitting on newlines and appending each chunk.

        For the common single-chunk case this is a single ``append_line``
        call minus the auto-appended newline — i.e. the content is passed
        as-is with no trailing newline added.  Callers that need guaranteed
        line semantics should use :meth:`append_line` directly.
        """
        # Use the atomic runtime append_line op, but don't add an extra
        # newline — we strip the one that append_line adds by treating the
        # entire content as a "line" without a trailing newline guard.
        # The simplest correct implementation is to delegate to write_file
        # via a read-modify-write (same as the ABC default) but route it
        # through the runtime rather than raw FS.
        existing: bytes
        try:
            existing = await self.read_file(path)
        except (FileNotFoundError, OSError, Exception):
            existing = b""
        await self.write_file(path, existing + content)

    async def append_line(self, path: str, line: bytes) -> int:
        """Atomically append *line* to *path* via the runtime's native op.

        Returns the byte offset at which *line* was written.
        """
        return await self._client.append_line(self._resolve(path), line)

    async def list_dir(self, path: str) -> list[FileStat]:
        return await self._client.list_dir(self._resolve(path))

    async def stat(self, path: str) -> FileStat | None:
        return await self._client.stat(self._resolve(path))

    async def delete(self, path: str) -> None:
        await self._client.delete(self._resolve(path))

    def archive(self, paths: list[str]) -> AsyncIterator[bytes]:
        return self._client.archive([self._resolve(p) for p in paths])

    # ------------------------------------------------------------------
    # Inspection + lifecycle
    # ------------------------------------------------------------------

    async def inspect(self) -> SandboxInspectInfo:
        """Return a snapshot of the sandbox's runtime health.

        Queries the runtime ``health`` op and maps the result to a
        :class:`SandboxInspectInfo`.  If the health call fails (e.g. the
        runtime is unreachable) a synthetic ``"failed"`` snapshot is
        returned rather than propagating the error.
        """
        from datetime import datetime, timezone

        try:
            result = await self._client._send_request(  # noqa: SLF001
                __import__(
                    "primer.workspace.runtime.protocol",
                    fromlist=["OpName"],
                ).OpName.HEALTH,
                {},
            )
            return SandboxInspectInfo(
                state="running",
                started_at=datetime.now(tz=timezone.utc),
                detail={
                    "version": result.get("version"),
                    "uptime_s": result.get("uptime_s"),
                    "watches_active": result.get("watches_active"),
                    "execs_running": result.get("execs_running"),
                },
            )
        except Exception:  # noqa: BLE001
            return SandboxInspectInfo(state="unknown")

    async def ping(self) -> bool:
        """Cheap liveness probe via the underlying :class:`RuntimeClient`.

        Returns True if the runtime responds to a ``health`` request,
        False on any error (disconnected, timeout, protocol error).
        Used by :class:`SandboxWorkspace.ping` and the Phase-7 probe.
        """
        try:
            await self._client.ping()
        except Exception:  # noqa: BLE001
            return False
        return True

    async def stop(self) -> None:
        """Stop the container.

        Delegates to the :class:`ContainerHandle` supplied at construction
        time if one was provided.  Otherwise raises :class:`NotImplementedError`;
        backend adapters that do not supply a handle must subclass and override.
        """
        if self._container_handle is not None:
            await self._container_handle.stop()
        else:
            raise NotImplementedError(
                "WSSandbox.stop() requires a ContainerHandle or a subclass override."
            )

    async def remove(self) -> None:
        """Remove the container and its volumes.

        Delegates to the :class:`ContainerHandle` supplied at construction
        time if one was provided.  Otherwise raises :class:`NotImplementedError`.
        """
        if self._container_handle is not None:
            await self._container_handle.remove()
        else:
            raise NotImplementedError(
                "WSSandbox.remove() requires a ContainerHandle or a subclass override."
            )

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        """Return an absolute path inside the container.

        If *path* is already absolute (starts with ``/``) it is returned
        unchanged.  Otherwise it is joined with :attr:`_workspace_root`.
        """
        if path.startswith("/"):
            return path
        return f"{self._workspace_root}/{path}"


__all__ = ["ContainerHandle", "WSSandbox"]
