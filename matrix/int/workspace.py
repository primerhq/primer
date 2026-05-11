"""Abstract base classes for Workspace and WorkspaceBackend.

Sibling of :class:`matrix.int.LLM`, :class:`matrix.int.Embedder`,
:class:`matrix.int.ToolsetProvider`, :class:`matrix.int.Storage`, and
:class:`matrix.int.VectorStore`.

Two ABCs are exported:

* :class:`Workspace` -- one materialised sandbox + ``.state`` + ``.tmp``
  + a session registry. Multiple :class:`AgentSession` instances can
  attach concurrently and share the workspace's filesystem and shell.
* :class:`WorkspaceBackend` -- backend-agnostic factory + lifecycle
  for :class:`Workspace` instances. One per execution backend
  (``LocalWorkspaceBackend`` ships in sub-project E; future
  Docker / Firecracker / chroot backends in later sub-projects). The
  configuration that selects which backend is the
  :class:`matrix.model.workspace.WorkspaceProvider` model.

The :class:`AgentSession` concrete class and the workspace tool
implementations live under :mod:`matrix.workspace` -- those land in
sub-projects D and C respectively. This module references them
through forward strings so it remains importable on its own.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from matrix.model.session import AgentBinding, SessionInfo, SessionStatus
from matrix.model.workspace import (
    FileEntry,
    WorkspaceStatus,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)


if TYPE_CHECKING:
    # AgentSession is concrete and lives at matrix/workspace/session.py
    # (sub-project D); WorkspaceTool ABC lives at matrix/workspace/tool.py
    # (already shipped in sub-project A). Import both here under
    # TYPE_CHECKING so this module is importable without sub-project D
    # while still giving type checkers the right types.
    from matrix.workspace.session import AgentSession
    from matrix.workspace.local.state import CommitInfo
    from matrix.workspace.tool import WorkspaceTool


# ===========================================================================
# Workspace
# ===========================================================================


class Workspace(ABC):
    """One materialised sandbox + ``.state`` + ``.tmp`` + session registry.

    Multiple :class:`AgentSession` instances can run concurrently. The
    workspace is the user-facing handle: it lets the user list
    sessions, append instructions (via the returned session handles),
    browse / download files, and inspect history. It does NOT drive
    sessions -- the agent runtime calls into the workspace to start
    sessions and update their state.
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable identifier of this workspace."""

    @property
    @abstractmethod
    def template(self) -> WorkspaceTemplate:
        """The template this workspace was materialised from."""

    # ---------- Tool surface ----------------------------------------------

    @abstractmethod
    def get_tools(self) -> list["WorkspaceTool"]:
        """Return the workspace tool set.

        Returns the seven concrete tools (``ls``, ``read``, ``write``,
        ``edit``, ``glob``, ``grep``, ``exec``). The agent runtime
        composes these onto an agent's other tools at session start.

        These tools are NOT registered in the global tools collection;
        they are workspace-local and their execution requires an
        :class:`AgentSession` for context.
        """

    # ---------- Session lifecycle -----------------------------------------

    @abstractmethod
    async def start_session(
        self,
        agent_binding: AgentBinding,
        *,
        id: str | None = None,
        instructions: str | None = None,
        parent_session_id: str | None = None,
    ) -> "AgentSession":
        """Begin a new session of ``agent_binding.agent_id`` on this workspace.

        Allocates a fresh ``session_id``, creates the
        ``.state/sessions/<session_id>/`` slot, writes ``session.json``
        and ``agent.json`` (the supplied :class:`AgentBinding` snapshot),
        creates the per-session cache subdirectory ``.tmp/<session_id>/``,
        and records an initial ``attach`` commit. If ``instructions``
        is non-empty, writes them as the first user-role message in
        ``messages.jsonl`` -- the agent will see them on its first turn.

        ``id``: If supplied, use as the session_id; otherwise generate a
        fresh UUID. Lets the REST API allocate the id ahead of time so
        the persisted Session row and the on-disk slot share the same
        identifier.

        ``parent_session_id`` is set when the session was spawned by
        another session (the agent runtime's spawn meta-tool); used
        for history attribution. No state is automatically propagated
        from parent to child.

        Returns a fresh :class:`AgentSession` in status
        :attr:`SessionStatus.RUNNING`.
        """

    @abstractmethod
    async def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        status: SessionStatus | None = None,
    ) -> list[SessionInfo]:
        """List sessions on this workspace, optionally filtered.

        Reads from the in-memory registry (fast). Filters are AND-ed.
        Returns sessions in start order, most recent first.
        """

    @abstractmethod
    async def get_session(self, session_id: str) -> "AgentSession | None":
        """Return the live session handle, or ``None`` if no such session."""

    # ---------- File browsing for users ----------------------------------

    @abstractmethod
    async def list_files(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
    ) -> list[FileEntry]:
        """List files in the workspace (user-facing, NOT a tool).

        Distinct from the ``ls`` workspace tool: this method is called
        by the user via the workspace handle, not by an agent. Returns
        :class:`FileEntry` records (path, kind, size, modified_at)
        suitable for rendering in a UI / CLI.
        """

    @abstractmethod
    async def read_file(self, path: str) -> bytes:
        """Return raw bytes of a file (user-facing).

        For downloading a single file. Bypasses the truncation cache
        and the workspace tool dispatch -- the user might want a 100 MB
        binary.
        """

    @abstractmethod
    def download_archive(
        self,
        paths: list[str] | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream a tar archive of the requested paths (or whole workspace).

        Used by the user UI / CLI to bulk-download workspace contents.
        ``paths=None`` means "everything except .state and .tmp".

        Concrete implementations are async generators
        (``async def download_archive(...): ... yield chunk``); this
        ABC declaration uses a regular ``def`` returning
        :class:`AsyncIterator` so the call shape doesn't require an
        extra ``await`` before iteration -- mirroring the convention
        used by :meth:`matrix.int.LLM.stream` and
        :meth:`matrix.int.Storage.list_tools`.
        """

    @abstractmethod
    async def file_info(self, path: str) -> FileEntry:
        """Return :class:`FileEntry` for one path (file, dir, or symlink).

        User-facing — distinct from the agent ``ls`` tool which
        returns descriptive metadata for many entries. Raises
        :class:`matrix.model.except_.NotFoundError` if the path does
        not exist; :class:`matrix.model.except_.BadRequestError` if
        the path tries to escape the workspace root.
        """

    @abstractmethod
    async def write_file(self, path: str, content: bytes) -> None:
        """Replace (or create) the file at ``path`` with ``content``.

        Creates parent directories as needed. Raises
        :class:`matrix.model.except_.BadRequestError` for invalid
        paths (null byte, escape-attempt, attempts to overwrite a
        directory).
        """

    @abstractmethod
    async def delete_file(self, path: str) -> None:
        """Delete the file or empty directory at ``path``.

        Raises :class:`matrix.model.except_.NotFoundError` if absent.
        Refuses to delete the workspace root or the ``.state`` /
        ``.tmp`` directories with
        :class:`matrix.model.except_.BadRequestError`.
        """

    @abstractmethod
    async def log(self, *, limit: int = 50) -> "list[CommitInfo]":
        """Return up to ``limit`` recent commits from the ``.state`` repo.

        Newest commits first. Each commit carries the parsed
        ``X-Matrix-*`` trailers (workspace, session, agent, op, tool,
        call) so callers can render structured history without
        re-parsing the message body.
        """

    @abstractmethod
    async def status(self) -> WorkspaceStatus:
        """Return a snapshot of this workspace's runtime health.

        See :class:`matrix.model.workspace.WorkspaceStatus`. The
        ``backend`` field in the returned object identifies the
        materialising backend ('local', 'container', 'kubernetes').
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Tear down the workspace.

        Whether this destroys the underlying container / chroot is the
        backend's choice (durable backends keep state on disk;
        ephemeral backends clean up). The ``.state`` repo is
        preserved if at all possible -- backends that destroy data
        MUST surface the choice in their config.
        """


# ===========================================================================
# Workspace provider
# ===========================================================================


class WorkspaceBackend(ABC):
    """Backend-agnostic factory + lifecycle for :class:`Workspace`.

    One per execution backend. Mirrors the
    :class:`matrix.int.StorageProvider` /
    :class:`matrix.int.VectorStoreProvider` pattern. The configuration
    that selects which backend (and supplies its connection settings)
    lives in :class:`matrix.model.workspace.WorkspaceProvider`.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Open backend connections / pools. Idempotent."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close backend connections / pools. Idempotent."""

    @abstractmethod
    async def create(
        self,
        template: WorkspaceTemplate,
        *,
        overrides: WorkspaceTemplateOverrides | None = None,
    ) -> Workspace:
        """Materialise a new workspace from ``template``.

        ``overrides`` allows per-instantiation tweaks (additional env
        vars, additional files, additional init commands). Override
        semantics are merge-then-extend (see
        :class:`matrix.model.workspace.WorkspaceTemplateOverrides`).
        """

    @abstractmethod
    async def get(self, workspace_id: str) -> Workspace | None:
        """Look up an existing workspace by id, or ``None`` if not found."""

    @abstractmethod
    async def list(self) -> list[str]:
        """Return ids of every workspace this provider currently manages."""

    @abstractmethod
    async def destroy(self, workspace_id: str) -> None:
        """Permanently remove a workspace -- backend resources AND state.

        Distinct from :meth:`Workspace.aclose`, which only releases
        the runtime handle. Raises
        :class:`matrix.model.except_.NotFoundError` if no such
        workspace exists.
        """


__all__ = [
    "Workspace",
    "WorkspaceBackend",
]
