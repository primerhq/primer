"""Local-FS workspace backend.

Two exports:

* :class:`LocalWorkspaceBackend` -- :class:`matrix.int.WorkspaceBackend`
  implementation. Materialises workspaces as plain directories under a
  configurable root path. Runs in the host process; no container, no
  isolation; intended for development / testing the contract end-to-end.
* :class:`LocalWorkspace` -- the per-workspace handle. Owns one
  :class:`StateRepo`, one :class:`TruncationStore`, the seven concrete
  workspace tools, and an in-memory session registry.

Per the spec, this backend skips capabilities it cannot enforce:

* Resource limits (CPU / memory / disk) are not enforced; a startup
  warning is emitted if any are set.
* Network mode is not enforced.
* Package installation is not performed; init_commands are still run
  (so the agent can install via the configured shell if needed).
* File sources other than ``inline`` are logged-and-skipped with a
  warning (URL / document / secret integration belongs to a future
  sub-project that has access to the storage + secret subsystems).
* Provider state does not persist across restart -- the in-memory
  workspace registry starts empty each session; on-disk workspaces
  are not auto-rediscovered.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` (the
"Sub-project E" section) for the full design.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tarfile
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from matrix.int.workspace import Workspace, WorkspaceBackend
from matrix.model.except_ import BadRequestError, ConflictError, NotFoundError
from matrix.model.session import (
    AgentBinding,
    SessionInfo,
    SessionStatus,
)
from matrix.model.workspace import (
    FileEntry,
    FileMount,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from matrix.workspace.cache import TruncationStore
from matrix.workspace.session import AgentSession
from matrix.workspace.state import StateRepo
from matrix.workspace.tool import WorkspaceTool
from matrix.workspace.tools import Edit, Exec, Glob, Grep, Ls, Read, Write


if TYPE_CHECKING:
    from pydantic import SecretStr


logger = logging.getLogger(__name__)


_TAR_CHUNK_BYTES = 64 * 1024


def _generate_workspace_id() -> str:
    return f"ws-{uuid.uuid4().hex[:16]}"


def _generate_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:16]}"


def _resolve_env(env: "dict[str, SecretStr]") -> dict[str, str]:
    """Unwrap SecretStr values for use as a real process environment."""
    return {k: v.get_secret_value() for k, v in env.items()}


# ===========================================================================
# LocalWorkspace
# ===========================================================================


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
        state_repo: StateRepo,
        truncation_store: TruncationStore,
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
        repo = StateRepo(state_path, workspace_id=workspace_id)
        await repo.initialize()
        cache = TruncationStore(tmp_path)

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

    def get_tools(self) -> list[WorkspaceTool]:
        return list(self._tools)

    async def start_session(
        self,
        agent_binding: AgentBinding,
        *,
        instructions: str | None = None,
        parent_session_id: str | None = None,
    ) -> AgentSession:
        async with self._lock:
            session_id = _generate_session_id()
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
        await asyncio.to_thread(parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, content)

    async def delete_file(self, path: str) -> None:
        target = self._resolve_path(path)
        if not await asyncio.to_thread(target.exists):
            raise NotFoundError(f"{path!r} not found")
        if target == self._root.resolve():
            raise BadRequestError("refusing to delete workspace root")
        self._refuse_reserved(target, path)
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(target.rmdir)  # rmdir => empty-only
        else:
            await asyncio.to_thread(target.unlink)

    async def log(self, *, limit: int = 50):
        return await self._state.history(limit=limit)

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
# LocalWorkspaceBackend
# ===========================================================================


class LocalWorkspaceBackend(WorkspaceBackend):
    """:class:`WorkspaceProvider` backed by ordinary directories on disk.

    Stores every workspace under ``<root>/<workspace_id>/``. Workspaces
    materialised in one process are tracked in memory only; provider
    re-discovery on restart is a future enhancement.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._workspaces: dict[str, LocalWorkspace] = {}
        self._lock = asyncio.Lock()
        self._initialised = False

    @property
    def root(self) -> Path:
        return self._root

    async def initialize(self) -> None:
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        self._initialised = True

    async def aclose(self) -> None:
        async with self._lock:
            for ws in list(self._workspaces.values()):
                try:
                    await ws.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "LocalWorkspaceBackend: aclose on workspace failed",
                        extra={"workspace_id": ws.id, "error": str(exc)},
                    )
            self._workspaces.clear()
            self._initialised = False

    async def create(
        self,
        template: WorkspaceTemplate,
        *,
        overrides: WorkspaceTemplateOverrides | None = None,
    ) -> Workspace:
        if not self._initialised:
            await self.initialize()

        # Warn on capabilities we cannot enforce; do NOT fail.
        _warn_unenforced(template)

        # Merge template + overrides (merge-then-extend semantics).
        merged_env = dict(template.env)
        if overrides is not None:
            merged_env.update(overrides.env)
        merged_files = list(template.files) + (
            list(overrides.files) if overrides else []
        )
        merged_init = list(template.init_commands) + (
            list(overrides.init_commands) if overrides else []
        )

        env_str = _resolve_env(merged_env)

        workspace_id = _generate_workspace_id()
        ws_root = self._root / workspace_id
        await asyncio.to_thread(ws_root.mkdir, parents=True, exist_ok=False)

        try:
            for fm in merged_files:
                await self._materialise_file(ws_root, fm)
            for cmd in merged_init:
                await self._run_init_command(ws_root, cmd, env_str)
            ws = await LocalWorkspace.materialise(
                workspace_id=workspace_id,
                root=ws_root,
                template=template,
                env=env_str,
            )
        except Exception:
            # Roll back the partially-built workspace directory so a
            # retry sees a clean root.
            try:
                await asyncio.to_thread(shutil.rmtree, ws_root)
            except Exception:  # noqa: BLE001
                pass
            raise

        async with self._lock:
            self._workspaces[workspace_id] = ws
        return ws

    async def get(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    async def list(self) -> list[str]:
        return list(self._workspaces)

    async def destroy(self, workspace_id: str) -> None:
        async with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
        if ws is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        try:
            await ws.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LocalWorkspaceBackend: aclose on destroy failed",
                extra={"workspace_id": workspace_id, "error": str(exc)},
            )
        await asyncio.to_thread(shutil.rmtree, ws.root, ignore_errors=True)

    # ---- internals ------------------------------------------------------

    async def _materialise_file(self, ws_root: Path, fm: FileMount) -> None:
        if "\x00" in fm.path:
            raise BadRequestError(f"file path contains null byte: {fm.path!r}")
        target = ws_root / fm.path
        # Defensive: keep writes inside ws_root.
        try:
            target.resolve().relative_to(ws_root.resolve())
        except ValueError as exc:
            raise BadRequestError(
                f"file path resolves outside workspace: {fm.path!r}"
            ) from exc
        await asyncio.to_thread(
            target.parent.mkdir, parents=True, exist_ok=True
        )
        kind = fm.source.kind
        if kind == "inline":
            await asyncio.to_thread(
                target.write_text, fm.source.content, encoding="utf-8"
            )
        else:
            logger.warning(
                "LocalWorkspaceBackend: file source kind not yet supported",
                extra={"path": fm.path, "kind": kind},
            )
            return
        if fm.mode is not None:
            try:
                octal = int(fm.mode, 8)
                await asyncio.to_thread(target.chmod, octal)
            except (ValueError, OSError, NotImplementedError):
                # Mode application is best-effort on local backend.
                pass

    async def _run_init_command(
        self,
        ws_root: Path,
        command: str,
        env: dict[str, str],
    ) -> None:
        proc_env = {**os.environ, **env} if env else None
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ws_root),
            env=proc_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise BadRequestError(
                f"init command failed (rc={proc.returncode}): {command!r}\n"
                f"stderr: {stderr.decode('utf-8', errors='replace').strip()}"
            )
        del stdout  # success path does not surface stdout


# ===========================================================================
# Helpers
# ===========================================================================


def _warn_unenforced(template: WorkspaceTemplate) -> None:
    """Emit warnings for template features the local backend cannot enforce."""
    r = template.resources
    if (
        r.cpu_cores is not None
        or r.memory_bytes is not None
        or r.disk_bytes is not None
    ):
        logger.warning(
            "LocalWorkspaceBackend does not enforce resource limits"
        )
    if r.network != "egress":
        logger.warning(
            "LocalWorkspaceBackend does not enforce network mode",
            extra={"network": r.network},
        )
    if template.packages:
        logger.warning(
            "LocalWorkspaceBackend does not install packages declaratively; "
            "run them via init_commands",
            extra={"packages": [p.name for p in template.packages]},
        )


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
    out: list[FileEntry] = []
    iterator = (
        target.rglob("*")
        if recursive
        else sorted(target.iterdir(), key=lambda p: p.name)
    )
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
        rel = entry.relative_to(workspace_root).as_posix()
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


__all__ = [
    "LocalWorkspace",
    "LocalWorkspaceBackend",
]
