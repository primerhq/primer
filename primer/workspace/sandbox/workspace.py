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
    def gone(self) -> bool:
        """Delegate to the underlying sandbox's ``gone`` flag.

        :class:`WSSandbox` exposes ``gone`` (true once its
        :class:`RuntimeClient` self-evicts on a 404 handshake); sandbox
        impls without a runtime client (e.g. the test ``FakeSandbox``)
        don't expose it, so we default to ``False``. The backend cache
        evicts a workspace whose ``gone`` is ``True``.
        """
        return bool(getattr(self._sandbox, "gone", False))

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
        name: str | None = None,
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
                name=name,
            )
            self._sessions[session_id] = session
            return session

    async def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        status: SessionStatus | None = None,
    ) -> list[SessionInfo]:
        # Rehydrate every persisted session into the in-memory registry so
        # sessions created in another process (the API/worker split) or
        # before a platform restart are visible. The container/k8s
        # ``.state`` tree is a runtime-managed git repo, so we enumerate
        # ids via ``state_history`` (see SandboxStateRepo.list_session_ids)
        # and rebuild any handle we don't already hold. This brings the
        # sandbox backends to parity with the local backend's cross-process
        # session survival -- a session is no longer dropped just because a
        # different process owns the in-memory slot.
        await self._rehydrate_all_sessions()
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
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        # Cross-process rehydration -- mirrors LocalWorkspace.get_session.
        # The session may have been created on a different process (e.g. the
        # API process allocated the slot via start_session; a worker process
        # now needs to build its executor and run the turn) or before a
        # platform restart. The slot is persisted inside the sandbox under
        # ``<state_path>/sessions/<sid>/`` (session.json + agent.json) in the
        # runtime-managed git repo, so rebuild the in-memory handle from it.
        # Returns None when no slot exists.
        async with self._lock:
            # Re-check under the lock in case a concurrent caller rehydrated.
            cached = self._sessions.get(session_id)
            if cached is not None:
                return cached
            return await self._rehydrate_locked(session_id)

    async def remove_session(self, session_id: str) -> bool:
        """Drop the in-memory handle for ``session_id`` and reap its slot.

        Pops the cached handle, then best-effort removes the persisted slot
        (``sessions/<sid>/session.json`` + ``agent.json``) from the pod's
        runtime state repo so a rehydrating :meth:`list_sessions` /
        :meth:`get_session` no longer surfaces the deleted session. The
        sandbox backend keeps its slot inside the pod's ``.state`` git repo
        (via ``_state_repo``), which the API handler's local-only host
        rmtree never reached -- so the reap has to happen here. Returns
        ``True`` when a cached entry was removed.
        """
        async with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
        # Reap outside the lock: it's a runtime RPC round-trip that must
        # never wedge the in-memory registry, and it's best-effort -- an
        # unreachable workspace must not fail the delete.
        try:
            await self._state_repo.delete_session(session_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "SandboxWorkspace.remove_session: slot reap failed for %s",
                session_id,
                exc_info=True,
            )
        return removed

    async def _rehydrate_locked(self, session_id: str) -> AgentSession | None:
        """Rebuild one session handle from persisted state. Caller holds
        ``self._lock``. Returns ``None`` when no slot is persisted."""
        info = await self._state_repo.load_session_info(session_id)
        binding = await self._state_repo.load_agent_binding(session_id)
        if info is None or binding is None:
            return None
        session = AgentSession(
            session_info=info,
            agent_binding=binding,
            state_repo=self._state_repo,
            truncation_store=self._cache,
            workspace_tools=self._tools,
        )
        self._sessions[session_id] = session
        return session

    async def _rehydrate_all_sessions(self) -> None:
        """Rebuild handles for every persisted session not already cached.

        Best-effort: a slot whose ``session.json``/``agent.json`` can't be
        read (mid-write race, partial create) is skipped rather than
        failing the whole listing."""
        try:
            session_ids = await self._state_repo.list_session_ids()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SandboxWorkspace: list_session_ids failed during rehydrate",
                extra={"workspace_id": self._workspace_id, "error": str(exc)},
            )
            return
        async with self._lock:
            for sid in session_ids:
                if sid in self._sessions:
                    continue
                try:
                    await self._rehydrate_locked(sid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "SandboxWorkspace: skipping unrehydratable session",
                        extra={
                            "workspace_id": self._workspace_id,
                            "session_id": sid,
                            "error": str(exc),
                        },
                    )

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
            # The runtime's list_dir returns each entry's ABSOLUTE path
            # (/workspace/foo); FakeSandbox returns a basename. Take the
            # basename either way so we don't double-anchor the workspace
            # root (which yielded an absolute FileEntry.path that the read
            # endpoint then re-anchored a second time -> ENOENT).
            name = fs.path.rsplit("/", 1)[-1]
            child = f"{target}/{name}"
            out.append(self._file_entry_from_stat(fs, child))
        return out

    async def _walk(self, dir_abs: str, out: list[FileEntry]) -> None:
        for fs in await self._sandbox.list_dir(dir_abs):
            name = fs.path.rsplit("/", 1)[-1]
            child = f"{dir_abs}/{name}"
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
        if info.kind == "dir":
            children = await self._sandbox.list_dir(target)
            if children and not recursive:
                raise BadRequestError(
                    f"directory {path!r} is not empty; pass recursive=true "
                    f"to delete it and its contents"
                )
            # The runtime ``delete`` op removes a single file or an EMPTY
            # directory only -- it is NOT recursive, so a non-empty dir 500s.
            # Empty the tree child-first so every delete lands on a leaf file
            # or an already-emptied directory.
            await self._delete_tree(target)
        else:
            await self._sandbox.delete(target)

    async def _delete_tree(self, target: str) -> None:
        """Depth-first delete of everything under ``target`` then ``target``.

        Compensates for the non-recursive runtime ``delete`` op. ``target`` is
        an absolute sandbox path; children are re-anchored from each entry's
        basename (``list_dir`` returns absolute paths on the real runtime and
        basenames on the fake -- taking the leaf handles both).
        """
        for fs in await self._sandbox.list_dir(target):
            name = fs.path.rsplit("/", 1)[-1]
            child = f"{target}/{name}"
            if fs.kind == "dir":
                await self._delete_tree(child)
            else:
                await self._sandbox.delete(child)
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
        # Serialise against this session's messages.jsonl read->rewrite
        # windows (AgentSession.append_instruction, the executor's turn
        # persist). The default sandbox ``append_file`` is itself
        # read-modify-write, so without this the event row could be
        # truncated by a concurrent rewrite. Keyed by session id, so a
        # flush for one session never waits on another session's commit.
        async with self._state_repo.messages_lock(session_id):
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

    async def write_state_file(self, relative_path: str, content: bytes) -> None:
        """Overwrite ``<workspace_root>/<relative_path>`` via the sandbox.

        Privileged, guard-free sibling of :meth:`append_state_line`: the mount
        sidecar (``.state/mounts.json``) lives under the reserved ``.state``
        tree that the public :meth:`write_file` refuses to mutate, so it is
        written straight through the sandbox rather than the guarded facade.
        ``_resolve_path`` still rejects ``..``/absolute escapes (keeping the
        write inside the workspace root); only ``_refuse_reserved`` is skipped.
        """
        target = self._resolve_path(relative_path)
        await self._sandbox.write_file(target, content)

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
