"""Sandbox ABC -- universal execution + filesystem interface for
non-local workspace backends.

Sibling of :class:`primer.int.workspace.Workspace`, but at a lower
layer: a Workspace owns one Sandbox; the Sandbox owns the actual
container / pod and presents a uniform interface to it. The
:class:`SandboxWorkspace` impl wraps any Sandbox to satisfy the
Workspace contract.

See ``docs/superpowers/specs/2026-05-11-workspace-backends-design.md``
§7 for the contract.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExecResult(BaseModel):
    """Result of one :meth:`Sandbox.exec` call."""

    exit_code: int = Field(..., description="Process exit code (-1 if killed).")
    stdout: str = Field(default="", description="UTF-8 decoded stdout.")
    stderr: str = Field(default="", description="UTF-8 decoded stderr.")
    duration_seconds: float = Field(
        default=0.0, description="Wall time the exec took.",
    )


class FileStat(BaseModel):
    """One filesystem entry's metadata."""

    path: str = Field(..., min_length=1)
    kind: Literal["file", "dir", "symlink"]
    size_bytes: int = Field(..., ge=0)
    mode: int = Field(..., description="POSIX mode bits.")
    modified_at: datetime


class SandboxInspectInfo(BaseModel):
    """Sandbox runtime status snapshot."""

    state: Literal[
        "created", "running", "stopped", "exited", "failed", "unknown"
    ]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Sandbox(ABC):
    """One materialised execution environment (container or pod)."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable identifier scoped to the backing runtime."""

    @abstractmethod
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
        """Run a command. Shell-string runs through ``sh -c``; list runs
        directly. On timeout, kills the process and raises
        :class:`TimeoutError`. ``abort`` is a cooperative cancel signal."""

    @abstractmethod
    async def read_file(self, path: str) -> bytes: ...

    @abstractmethod
    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None,
    ) -> None: ...

    async def append_file(self, path: str, content: bytes) -> None:
        """Atomically append ``content`` to the file at ``path``.

        Creates the file (and parent directories) if they do not exist.
        Implementations MUST NOT interleave partial writes from concurrent
        callers — each :meth:`append_file` call must land as a single
        contiguous chunk.

        Default implementation performs a read-modify-write via
        :meth:`read_file` + :meth:`write_file`.  This is correct but
        race-prone under concurrent writers.  Override with a proper
        atomic-append in runtimes where performance matters.
        """
        existing: bytes
        try:
            existing = await self.read_file(path)
        except (FileNotFoundError, OSError):
            existing = b""
        await self.write_file(path, existing + content)

    async def append_line(self, path: str, line: bytes) -> int:
        """Atomically append a single line to the file at ``path``.

        Returns the byte offset at which *line* was written (i.e. the
        file length before the append).

        This is the preferred append primitive for Cluster-4 session
        streaming.  It MUST NOT interleave partial writes from concurrent
        callers.  Backends that expose a native atomic-append op (e.g.
        the WS runtime's ``append_line`` op) MUST override this method.

        The default implementation delegates to :meth:`append_file`, which
        performs a read-modify-write — correct but not race-safe under
        concurrent writers.  A newline byte is appended after *line* to
        keep line-oriented consumers happy.
        """
        try:
            existing = await self.read_file(path)
        except (FileNotFoundError, OSError):
            existing = b""
        offset = len(existing)
        await self.write_file(path, existing + line + b"\n")
        return offset

    @abstractmethod
    async def list_dir(self, path: str) -> list[FileStat]: ...

    @abstractmethod
    async def stat(self, path: str) -> FileStat | None: ...

    @abstractmethod
    async def delete(self, path: str) -> None: ...

    @abstractmethod
    def archive(self, paths: list[str]) -> AsyncIterator[bytes]:
        """Stream a tar archive. Async generator; iterate with
        ``async for``."""

    @abstractmethod
    async def inspect(self) -> SandboxInspectInfo: ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the underlying container/pod. The handle stays usable --
        a subsequent ``exec`` raises a backend-specific error; the
        ``WorkspaceBackend.get()`` call is what restarts the sandbox."""

    @abstractmethod
    async def remove(self) -> None:
        """Permanently remove the container/pod AND its volumes."""


__all__ = [
    "ExecResult",
    "FileStat",
    "Sandbox",
    "SandboxInspectInfo",
]
