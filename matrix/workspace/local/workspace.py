"""LocalWorkspace — one materialised workspace backed by a host directory.

Constructed by :meth:`matrix.workspace.local.backend.LocalWorkspaceBackend.create`
and held by the in-process workspace registry. Owns one
:class:`StateRepo`, one :class:`TruncationStore`, the seven concrete
workspace tools, and an in-memory session registry.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` and
``docs/superpowers/specs/2026-05-11-workspace-backends-design.md`` §12.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from matrix.int.workspace import Workspace
from matrix.model.except_ import BadRequestError, ConflictError, NotFoundError
from matrix.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    SessionStatus,
)
from matrix.model.workspace import (
    CommitInfo,
    FileEntry,
    WorkspaceStatus,
    WorkspaceTemplate,
)
from matrix.workspace.local.cache import LocalTruncationStore
from matrix.workspace.local.state import LocalStateRepo, _GitCommandError
from matrix.workspace.local.tools import Edit, Exec, Glob, Grep, Ls, Read, Write
from matrix.workspace.session import AgentSession
from matrix.workspace.tool import WorkspaceTool


_TAR_CHUNK_BYTES = 64 * 1024


def _generate_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:16]}"


class LocalWorkspace(Workspace):
    """One materialised workspace backed by a local directory.

    Construct via :meth:`LocalWorkspaceBackend.create`; do not
    instantiate directly outside the provider (or for tests).
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        root: Path,
        template: WorkspaceTemplate,
        env: dict[str, str],
        state_repo: LocalStateRepo,
        truncation_store: LocalTruncationStore,
        tools: list[WorkspaceTool],
    ) -> None:
        self._workspace_id = workspace_id
        self._root = root
        self._template = template
        self._env = env
        self._state = state_repo
        self._cache = truncation_store
        self._tools = tools
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def materialise(
        cls,
        *,
        workspace_id: str,
        root: Path,
        template: WorkspaceTemplate,
        env: dict[str, str],
    ) -> "LocalWorkspace":
        """Build the on-disk pieces (state repo, tmp store, tools).

        ``root`` must already exist. Files / init_commands are NOT run
        here -- the provider does that before calling this constructor
        so it can decide ordering and surface init failures cleanly.
        """
        state_path = root / template.state_path
        tmp_path = root / template.tmp_path
        repo = LocalStateRepo(state_path, workspace_id=workspace_id)
        # Catch _GitCommandError + OSError so a malformed state_path
        # (deep nesting overflowing MAX_PATH, invalid filename chars,
        # permission denials, etc.) maps to a clean 4xx envelope
        # instead of leaking /errors/internal. The git layer's own
        # stderr (e.g. "Filename too long") is surfaced as the
        # detail so operators can correlate.
        try:
            await repo.initialize()
        except (OSError, _GitCommandError) as exc:
            raise BadRequestError(
                f"cannot initialise workspace state at "
                f"{template.state_path!r}: {exc.strerror or exc}"
                if isinstance(exc, OSError)
                else f"cannot initialise workspace state at "
                f"{template.state_path!r}: {exc}"
            ) from exc
        cache = LocalTruncationStore(tmp_path)

        tools: list[WorkspaceTool] = [
            Ls(root),
            Read(root),
            Write(root),
            Edit(root),
            Glob(root),
            Grep(root),
            Exec(root, env=env if env else None),
        ]
        return cls(
            workspace_id=workspace_id,
            root=root,
            template=template,
            env=env,
            state_repo=repo,
            truncation_store=cache,
            tools=tools,
        )

    # ---- Workspace ABC --------------------------------------------------

    @property
    def id(self) -> str:
        return self._workspace_id

    @property
    def template(self) -> WorkspaceTemplate:
        return self._template

    @property
    def root(self) -> Path:
        """The on-disk filesystem root the agent sees as ``/``."""
        return self._root

    @property
    def state_repo(self) -> LocalStateRepo:
        """Override the ABC default (``None``) — local workspaces expose
        their git-backed state repo so the graph executor can commit
        per-graph state via the workspace's ``.state/`` repo. The same
        repo also persists agent-session messages, so graph state and
        agent state share one git history per workspace."""
        return self._state

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
                state_repo=self._state,
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

    async def list_files(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
    ) -> list[FileEntry]:
        target = self._resolve_path(path)
        if not await asyncio.to_thread(target.exists):
            raise NotFoundError(f"{path!r} not found")
        if not await asyncio.to_thread(target.is_dir):
            raise BadRequestError(f"{path!r} is not a directory")

        return await asyncio.to_thread(
            _walk_for_user, target, self._root, recursive=recursive
        )

    async def read_file(self, path: str) -> bytes:
        target = self._resolve_path(path)
        if not await asyncio.to_thread(target.exists):
            raise NotFoundError(f"{path!r} not found")
        if not await asyncio.to_thread(target.is_file):
            raise BadRequestError(f"{path!r} is not a file")
        return await asyncio.to_thread(target.read_bytes)

    async def download_archive(
        self,
        paths: list[str] | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream a tar archive of the requested paths.

        Async generator -- callers iterate with ``async for`` directly.
        """
        if paths is None:
            members = await asyncio.to_thread(self._collect_default_members)
        else:
            members = []
            for p in paths:
                resolved = self._resolve_path(p)
                if not await asyncio.to_thread(resolved.exists):
                    raise NotFoundError(f"{p!r} not found")
                members.append(resolved)

        buf = io.BytesIO()
        # Use a non-streaming tar to keep the implementation simple;
        # the buffer is yielded in chunks afterwards. Acceptable for v1
        # because workspaces are bounded in size; a streaming writer can
        # come later if/when archives grow large.
        await asyncio.to_thread(_build_tar, buf, members, self._root)
        buf.seek(0)
        while True:
            chunk = buf.read(_TAR_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    async def file_info(self, path: str) -> FileEntry:
        target = self._resolve_path(path)
        if not await asyncio.to_thread(target.exists):
            raise NotFoundError(f"{path!r} not found")
        return await asyncio.to_thread(_make_file_entry, target, self._root)

    async def write_file(self, path: str, content: bytes) -> None:
        target = self._resolve_path(path)
        if await asyncio.to_thread(target.is_dir):
            raise BadRequestError(
                f"{path!r} is a directory; cannot overwrite with file content"
            )
        # Refuse writes inside the reserved state / tmp paths so the
        # API can't corrupt the backend's bookkeeping.
        self._refuse_reserved(target, path)
        parent = target.parent
        try:
            await asyncio.to_thread(parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, content)
        except OSError as exc:
            # Map filesystem-rejection errors (invalid filename chars on
            # Windows, MAX_PATH overflow, etc.) to a clean 4xx instead
            # of letting the OSError leak as 500 /errors/internal.
            raise BadRequestError(
                f"cannot write {path!r}: {exc.strerror or exc}"
            ) from exc

    async def delete_file(self, path: str) -> None:
        target = self._resolve_path(path)
        if not await asyncio.to_thread(target.exists):
            raise NotFoundError(f"{path!r} not found")
        if target == self._root.resolve():
            raise BadRequestError("refusing to delete workspace root")
        self._refuse_reserved(target, path)
        if await asyncio.to_thread(target.is_dir):
            try:
                await asyncio.to_thread(target.rmdir)  # rmdir => empty-only
            except OSError as exc:
                raise BadRequestError(
                    f"directory {path!r} is not empty"
                ) from exc
        else:
            await asyncio.to_thread(target.unlink)

    async def log(self, *, limit: int = 50) -> list[CommitInfo]:
        try:
            return await self._state.history(limit=limit)
        except (OSError, _GitCommandError) as exc:
            # Either the workspace .state directory disappeared
            # mid-read (race with destroy) or git refused to read a
            # partial state. Map to NotFound so the API returns a
            # clean 404 instead of leaking 500 /errors/internal.
            raise NotFoundError(
                f"workspace log unavailable (state repo missing): {exc}"
            ) from exc

    async def status(self) -> WorkspaceStatus:
        if await asyncio.to_thread(self._root.exists):
            return WorkspaceStatus(state="ready", backend="local")
        return WorkspaceStatus(state="destroyed", backend="local")

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append ``line`` to the session's ``messages.jsonl``.

        Path: ``<root>/<template.state_path>/sessions/<session_id>/messages.jsonl``

        Uses ``open(path, 'ab')`` for an O_APPEND write, which is atomically
        safe at the OS level for all callers within this process (different
        sessions write to different files) and for single-session sequential
        writers. The trailing newline is enforced here so callers don't need
        to track it.
        """
        if not line:
            return
        if not line.endswith(b"\n"):
            line = line + b"\n"

        target = (
            self._root
            / self._template.state_path
            / "sessions"
            / session_id
            / "messages.jsonl"
        )

        def _append() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("ab") as fh:
                fh.write(line)

        await asyncio.to_thread(_append)

    async def aclose(self) -> None:
        """End any non-ENDED sessions, then release backend resources."""
        async with self._lock:
            for session in list(self._sessions.values()):
                try:
                    await session.aclose()
                except ConflictError:
                    # already ended; fine
                    pass
            self._sessions.clear()

    def _refuse_reserved(self, resolved: Path, original: str) -> None:
        """Block writes / deletes inside ``.state`` and ``.tmp``."""
        root_resolved = self._root.resolve()
        for reserved_name in (self._template.state_path, self._template.tmp_path):
            reserved = (root_resolved / reserved_name).resolve()
            try:
                resolved.relative_to(reserved)
            except ValueError:
                continue
            raise BadRequestError(
                f"refusing to mutate path inside reserved tree {reserved_name!r}: "
                f"{original!r}"
            )

    # ---- internals ------------------------------------------------------

    def _resolve_path(self, path: str) -> Path:
        if not path:
            raise BadRequestError("path must be non-empty")
        if "\x00" in path:
            raise BadRequestError("path contains a null byte")
        root_resolved = self._root.resolve()
        candidate = (root_resolved / path).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise BadRequestError(
                f"path resolves outside workspace: {path!r}"
            ) from exc
        return candidate

    def _collect_default_members(self) -> list[Path]:
        """Top-level entries under root EXCEPT .state/ and .tmp/."""
        skip = {self._template.state_path, self._template.tmp_path}
        return [
            entry
            for entry in self._root.iterdir()
            if entry.name not in skip
        ]


# ===========================================================================
# File-entry helpers
# ===========================================================================


def _make_file_entry(target: Path, workspace_root: Path) -> FileEntry:
    """Build one :class:`FileEntry` for ``target`` (file/dir/symlink)."""
    stat = target.stat()
    if target.is_symlink():
        kind: str = "symlink"
        size = 0
    elif target.is_dir():
        kind = "dir"
        size = 0
    else:
        kind = "file"
        size = stat.st_size
    rel = target.resolve().relative_to(workspace_root.resolve()).as_posix()
    if rel == ".":
        rel = ""
    return FileEntry(
        path=rel or ".",
        kind=kind,  # type: ignore[arg-type]
        size_bytes=size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def _walk_for_user(
    target: Path,
    workspace_root: Path,
    *,
    recursive: bool,
) -> list[FileEntry]:
    # `target` is already a resolved path (see _resolve_path). Resolve the
    # workspace root too so entries from `target.iterdir()` share a common
    # prefix with it — otherwise `entry.relative_to(workspace_root)`
    # raises ValueError on platforms where the unresolved root differs
    # from its resolved form (e.g. Windows short-name 8.3 temp paths
    # like USMANS~1 vs the long-name Usman Shahid).
    root_resolved = workspace_root.resolve()
    out: list[FileEntry] = []
    # `iterdir()` and `rglob()` both raise OSError (e.g.
    # FileNotFoundError) when the target directory disappears
    # between the list_files exists()/is_dir() gate and this walk
    # (TOCTOU window under concurrent PUT/DELETE). Treat the
    # missing-dir case as an empty listing — the priority-6
    # contract is "no /errors/internal leak on the listing path";
    # callers can re-check existence with /files/info if they
    # need to distinguish "empty" from "gone".
    try:
        if recursive:
            iterator = list(target.rglob("*"))
        else:
            iterator = sorted(target.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in iterator:
        try:
            stat = entry.stat()
        except OSError:
            continue
        if entry.is_symlink():
            kind: str = "symlink"
            size = 0
        elif entry.is_dir():
            kind = "dir"
            size = 0
        else:
            kind = "file"
            size = stat.st_size
        rel = entry.relative_to(root_resolved).as_posix()
        out.append(
            FileEntry(
                path=rel,
                kind=kind,  # type: ignore[arg-type]
                size_bytes=size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    if recursive:
        out.sort(key=lambda fe: fe.path)
    return out


def _build_tar(buf: io.BytesIO, members: list[Path], workspace_root: Path) -> None:
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for member in members:
            arcname = member.relative_to(workspace_root).as_posix()
            tf.add(str(member), arcname=arcname, recursive=True)


__all__ = ["LocalWorkspace"]
