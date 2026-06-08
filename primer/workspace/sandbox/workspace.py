"""SandboxWorkspace -- concrete :class:`Workspace` impl shared by
Container and K8s backends.

All file/exec operations delegate to a :class:`Sandbox`. Session
lifecycle mirrors :class:`LocalWorkspace`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal

from primer.int.sandbox import FileStat, Sandbox
from primer.int.workspace import Workspace
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    SessionStatus,
)
from primer.model.workspace import (
    FileEntry,
    WorkspaceDiagnosticResult,
    WorkspaceRuntimeMeta,
    WorkspaceStatus,
    WorkspaceTemplate,
)
from primer.workspace.sandbox.cache import SandboxTruncationStore
from primer.workspace.sandbox.state import SandboxStateRepo
from primer.workspace.sandbox.tools import (
    SandboxEdit,
    SandboxExec,
    SandboxGlob,
    SandboxGrep,
    SandboxLs,
    SandboxRead,
    SandboxWrite,
)
from primer.workspace.session import AgentSession
from primer.workspace.tool import WorkspaceTool


if TYPE_CHECKING:
    from primer.model.workspace import CommitInfo


logger = logging.getLogger(__name__)


def _generate_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:16]}"


_INSPECT_TO_STATUS: dict[str, str] = {
    "running": "ready",
    "created": "starting",
    "stopped": "stopped",
    "exited": "stopped",
    "failed": "unreachable",
    "unknown": "unreachable",
}


class SandboxWorkspace(Workspace):
    """Workspace backed by a :class:`Sandbox`. Shared by Container + K8s."""

    def __init__(
        self,
        *,
        workspace_id: str,
        template: WorkspaceTemplate,
        sandbox: Sandbox,
        state_repo: SandboxStateRepo,
        truncation_store: SandboxTruncationStore,
        tools: list[WorkspaceTool],
        backend_kind: Literal["container", "kubernetes"],
        runtime_meta: WorkspaceRuntimeMeta,
        workspace_root: str = "/workspace",
    ) -> None:
        self._workspace_id = workspace_id
        self._template = template
        self._sandbox = sandbox
        self._state_repo = state_repo
        self._cache = truncation_store
        self._tools = tools
        self._backend_kind = backend_kind
        self._runtime_meta = runtime_meta
        self._workspace_root = workspace_root.rstrip("/")
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def materialise(
        cls,
        *,
        workspace_id: str,
        template: WorkspaceTemplate,
        sandbox: Sandbox,
        backend_kind: Literal["container", "kubernetes"],
        runtime_meta: WorkspaceRuntimeMeta,
        workspace_root: str = "/workspace",
    ) -> "SandboxWorkspace":
        """Build the in-sandbox state repo + cache + tools and return a
        ready :class:`SandboxWorkspace`."""
        state_repo = SandboxStateRepo(
            sandbox,
            state_path=f"{workspace_root}/{template.state_path}",
            workspace_id=workspace_id,
        )
        await state_repo.initialize()
        cache = SandboxTruncationStore(
            sandbox, root=f"{workspace_root}/{template.tmp_path}",
        )
        tools: list[WorkspaceTool] = [
            SandboxLs(sandbox, workspace_root=workspace_root),
            SandboxRead(sandbox, workspace_root=workspace_root),
            SandboxWrite(sandbox, workspace_root=workspace_root),
            SandboxEdit(sandbox, workspace_root=workspace_root),
            SandboxGlob(sandbox, workspace_root=workspace_root),
            SandboxGrep(sandbox, workspace_root=workspace_root),
            SandboxExec(sandbox, workspace_root=workspace_root),
        ]
        return cls(
            workspace_id=workspace_id,
            template=template,
            sandbox=sandbox,
            state_repo=state_repo,
            truncation_store=cache,
            tools=tools,
            backend_kind=backend_kind,
            runtime_meta=runtime_meta,
            workspace_root=workspace_root,
        )

    # ---- Workspace ABC --------------------------------------------------

    @property
    def id(self) -> str:
        return self._workspace_id

    @property
    def template(self) -> WorkspaceTemplate:
        return self._template

    @property
    def runtime_meta(self) -> WorkspaceRuntimeMeta:
        return self._runtime_meta

    @property
    def sandbox(self) -> Sandbox:
        """The underlying :class:`Sandbox` handle.

        Exposed so backends performing teardown (``destroy``) can stop +
        remove the sandbox without reaching into private attributes."""
        return self._sandbox

    @property
    def state_repo(self) -> SandboxStateRepo:
        """The workspace's git-backed state repository.

        Exposes the :class:`SandboxStateRepo` so the graph executor and
        other consumers that access ``workspace.state_repo`` obtain the
        sandbox-backed implementation rather than the ABC default (None).
        """
        return self._state_repo

    def get_tools(self) -> list[WorkspaceTool]:
        return list(self._tools)

    async def start_session(
        self,
        agent_binding: AgentBinding,
        *,
        id: str | None = None,
        instructions: str | None = None,
        parent_session_id: str | None = None,
    ) -> AgentSession:
        async with self._lock:
            if id is not None and id in self._sessions:
                raise ConflictError(
                    f"session {id!r} already exists on this workspace"
                )
            session_id = id if id is not None else _generate_session_id()
            session = await AgentSession.start(
                session_id=session_id,
                workspace_id=self._workspace_id,
                agent_binding=agent_binding,
                state_repo=self._state_repo,
                truncation_store=self._cache,
                workspace_tools=self._tools,
                instructions=instructions,
                parent_session_id=parent_session_id,
            )
            self._sessions[session_id] = session
            return session

    async def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        status: SessionStatus | None = None,
    ) -> list[SessionInfo]:
        out: list[SessionInfo] = []
        for session in self._sessions.values():
            info = await session.info()
            if agent_id is not None and info.agent_id != agent_id:
                continue
            if status is not None and info.status != status:
                continue
            out.append(info)
        out.sort(key=lambda i: i.started_at, reverse=True)
        return out

    async def get_session(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    # ---- File browsing for users ----------------------------------------

    def _resolve_path(self, path: str) -> str:
        if not path:
            raise BadRequestError("path must be non-empty")
        if "\x00" in path:
            raise BadRequestError("path contains a null byte")
        if path == "." or path == "":
            return self._workspace_root
        parts: list[str] = []
        for part in path.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise BadRequestError(
                        f"path resolves outside workspace: {path!r}"
                    )
                parts.pop()
            else:
                parts.append(part)
        return f"{self._workspace_root}/{'/'.join(parts)}" if parts else self._workspace_root

    def _refuse_reserved(self, path: str) -> None:
        for r in (self._template.state_path, self._template.tmp_path):
            if path == r or path.startswith(f"{r}/"):
                raise BadRequestError(
                    f"refusing to mutate path inside reserved tree {r!r}: "
                    f"{path!r}"
                )

    def _file_entry_from_stat(
        self, fs: FileStat, abs_path: str,
    ) -> FileEntry:
        if abs_path == self._workspace_root:
            rel = "."
        elif abs_path.startswith(self._workspace_root + "/"):
            rel = abs_path[len(self._workspace_root) + 1:]
        else:
            rel = abs_path
        return FileEntry(
            path=rel or ".",
            kind=fs.kind,
            size_bytes=fs.size_bytes,
            modified_at=fs.modified_at,
        )

    async def list_files(
        self, path: str = ".", *, recursive: bool = False,
    ) -> list[FileEntry]:
        target = self._resolve_path(path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{path!r} not found")
        if info.kind != "dir":
            raise BadRequestError(f"{path!r} is not a directory")

        out: list[FileEntry] = []
        if recursive:
            await self._walk(target, out)
            out.sort(key=lambda fe: fe.path)
            return out
        for fs in await self._sandbox.list_dir(target):
            child = f"{target}/{fs.path}"
            out.append(self._file_entry_from_stat(fs, child))
        return out

    async def _walk(self, dir_abs: str, out: list[FileEntry]) -> None:
        for fs in await self._sandbox.list_dir(dir_abs):
            child = f"{dir_abs}/{fs.path}"
            out.append(self._file_entry_from_stat(fs, child))
            if fs.kind == "dir":
                await self._walk(child, out)

    async def read_file(self, path: str) -> bytes:
        target = self._resolve_path(path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{path!r} not found")
        if info.kind != "file":
            raise BadRequestError(f"{path!r} is not a file")
        return await self._sandbox.read_file(target)

    async def write_file(self, path: str, content: bytes) -> None:
        self._refuse_reserved(path)
        target = self._resolve_path(path)
        info = await self._sandbox.stat(target)
        if info is not None and info.kind == "dir":
            raise BadRequestError(
                f"{path!r} is a directory; cannot overwrite with file content"
            )
        await self._sandbox.write_file(target, content)

    async def make_dir(self, path: str) -> None:
        self._refuse_reserved(path)
        target = self._resolve_path(path)
        if await self._sandbox.stat(target) is not None:
            raise BadRequestError(f"{path!r} already exists")
        try:
            await self._sandbox.make_dir(target)
        except OSError as exc:
            raise BadRequestError(
                f"cannot create directory {path!r}: {exc}"
            ) from exc

    async def delete_file(self, path: str, *, recursive: bool = False) -> None:
        self._refuse_reserved(path)
        target = self._resolve_path(path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{path!r} not found")
        # The sandbox delete is recursive; guard non-empty directories
        # behind the recursive flag to match the local backend's contract.
        if info.kind == "dir" and not recursive:
            children = await self._sandbox.list_dir(target)
            if children:
                raise BadRequestError(
                    f"directory {path!r} is not empty; pass recursive=true "
                    f"to delete it and its contents"
                )
        await self._sandbox.delete(target)

    async def file_info(self, path: str) -> FileEntry:
        target = self._resolve_path(path)
        info = await self._sandbox.stat(target)
        if info is None:
            raise NotFoundError(f"{path!r} not found")
        return self._file_entry_from_stat(info, target)

    async def download_archive(
        self, paths: list[str] | None = None,
    ) -> AsyncIterator[bytes]:
        if paths is None:
            resolved = [self._workspace_root]
        else:
            resolved = [self._resolve_path(p) for p in paths]
        async for chunk in self._sandbox.archive(resolved):
            yield chunk

    async def log(self, *, limit: int = 50) -> "list[CommitInfo]":
        return await self._state_repo.history(limit=limit)

    async def status(self) -> WorkspaceStatus:
        info = await self._sandbox.inspect()
        return WorkspaceStatus(
            state=_INSPECT_TO_STATUS.get(info.state, "unreachable"),  # type: ignore[arg-type]
            backend=self._backend_kind,
            detail=info.detail,
        )

    async def ping(self) -> bool:
        """Delegate the liveness probe to the underlying sandbox.

        Sandbox impls that speak the WS runtime protocol (i.e.
        :class:`WSSandbox`) wrap a ``health`` request; impls without a
        cheap probe should override :meth:`Sandbox.ping` directly.
        Returns False on any error rather than propagating — the
        Phase-7 probe task interprets False as "transport unhealthy".
        """
        ping = getattr(self._sandbox, "ping", None)
        if ping is None:
            return False
        try:
            return await ping()
        except Exception:  # noqa: BLE001
            return False

    async def diagnostic_exec(
        self,
        command: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> WorkspaceDiagnosticResult:
        """Delegate to the underlying :meth:`Sandbox.exec`.

        The Sandbox ABC already exposes a generic ``exec`` primitive
        with ``timeout_seconds``; we forward verbatim and re-wrap the
        :class:`primer.int.sandbox.ExecResult` into a
        :class:`WorkspaceDiagnosticResult` (same shape, different package
        — the model lives next to the other workspace models so the API
        surface doesn't import the sandbox ABC). A timeout from
        :meth:`Sandbox.exec` (which raises :class:`TimeoutError`) is
        caught and returned as ``exit_code=-1``.
        """
        start = asyncio.get_event_loop().time()
        try:
            result = await self._sandbox.exec(
                command,
                workdir=self._workspace_root,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError:
            return WorkspaceDiagnosticResult(
                stdout="",
                stderr=f"command timed out after {timeout_seconds}s",
                exit_code=-1,
                duration_seconds=asyncio.get_event_loop().time() - start,
            )
        return WorkspaceDiagnosticResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
        )

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append ``line`` to the session's ``messages.jsonl``.

        Path inside the sandbox:
        ``<workspace_root>/<template.state_path>/sessions/<session_id>/messages.jsonl``

        Delegates to :meth:`Sandbox.append_file`, which uses read-modify-write
        by default.  The ``FakeSandbox`` (used in tests) overrides with a
        fast O_APPEND path.

        .. TODO(Cluster-5): The container/k8s Sandbox impls still use the
           default read-modify-write ``append_file``.  Cluster 5 will replace
           the exec-per-call runtime with a persistent WS runtime that
           exposes a native ``append_line`` op — at that point each backend's
           ``append_file`` override will be the thin shim.
        """
        if not line:
            return
        if not line.endswith(b"\n"):
            line = line + b"\n"

        path = (
            f"{self._workspace_root}/{self._template.state_path}"
            f"/sessions/{session_id}/messages.jsonl"
        )
        await self._sandbox.append_file(path, line)

    async def append_state_line(self, relative_path: str, line: bytes) -> None:
        """Append ``line`` to ``<workspace_root>/<relative_path>``.

        Delegates to :meth:`Sandbox.append_file`. Mirrors the
        ``append_message_line`` shape but with operator-controlled path.
        """
        if not line:
            return
        if not line.endswith(b"\n"):
            line = line + b"\n"
        path = f"{self._workspace_root}/{relative_path}"
        await self._sandbox.append_file(path, line)

    async def aclose(self) -> None:
        """Tear down every live session. Errors from any one session must
        not skip the rest -- log and continue, mirroring
        :meth:`LocalWorkspaceBackend.aclose`."""
        async with self._lock:
            for session in list(self._sessions.values()):
                try:
                    await session.aclose()
                except ConflictError:
                    # Already ended; benign.
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "SandboxWorkspace: aclose on session failed",
                        extra={
                            "workspace_id": self._workspace_id,
                            "session_id": session.session_id,
                            "error": str(exc),
                        },
                    )
            self._sessions.clear()


__all__ = ["SandboxWorkspace"]
