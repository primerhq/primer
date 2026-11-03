"""Formal StateRepo contract for workspace state backends.

LocalStateRepo (primer.workspace.local.state) and SandboxStateRepo
(primer.workspace.sandbox.state) both implement this Protocol.
Conformance is verified by tests/workspace/test_state_repo_conformance.py.

This module is type-only; it does not import any concrete implementation.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from primer.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    WaitingState,
)
from primer.model.workspace import CommitInfo, Op


@runtime_checkable
class StateRepo(Protocol):
    """Protocol describing the full surface of a workspace state repository.

    All methods are async. Implementations may raise NotImplementedError
    (HTTP 501 equivalent) for methods that are not applicable to their
    backend -- show_commit in particular may 501 on the sandbox backend.

    Two capabilities are OPTIONAL and declared apart, in :class:`StatingStateRepo` and :class:`AppendingStateRepo`: callers probe for them
    with ``getattr`` and degrade when they are absent.
    """

    async def initialize(self) -> None:
        """Open / initialise the repo. Idempotent."""
        ...

    async def create_session(
        self,
        session_info: SessionInfo,
        agent_binding: AgentBinding,
    ) -> str:
        """Allocate the session slot and return the SHA of the attach commit."""
        ...

    async def commit(
        self,
        session_id: str,
        *,
        summary: str,
        op: Op,
        tool: str | None = None,
        call_id: str | None = None,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
    ) -> str:
        """Stage files under sessions/<session_id>/, commit with trailers.

        Returns the new commit's full SHA.
        """
        ...

    async def commit_arbitrary(
        self,
        *,
        summary: str,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
        trailers: dict[str, str] | None = None,
    ) -> str:
        """Commit arbitrary files relative to the .state/ repo root.

        Returns the new commit's full SHA.
        """
        ...

    async def history(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        with_files: bool = False,
    ) -> list[CommitInfo]:
        """Return commits, optionally filtered by session or agent. Newest first.

        ``with_files=True`` asks for per-file line deltas on each
        ``CommitInfo.files``. Backends that cannot supply them leave the field
        ``None`` rather than ``[]`` -- see :class:`CommitInfo.files`. Callers
        must treat None as "unknown", never as zero.
        """
        ...

    async def show_commit(self, sha: str) -> dict:
        """Return diff view for a single commit.

        Optional -- may raise NotImplementedError on backends that do not
        expose per-commit diff data (e.g. the sandbox backend).
        """
        ...

    async def load_session_info(self, session_id: str) -> SessionInfo | None:
        """Read sessions/<session_id>/session.json if present."""
        ...

    async def load_agent_binding(self, session_id: str) -> AgentBinding | None:
        """Read sessions/<session_id>/agent.json if present."""
        ...

    async def load_waiting_state(self, session_id: str) -> WaitingState | None:
        """Read sessions/<session_id>/waiting.json if present; None when absent."""
        ...

    async def read_state_file(self, path: str) -> bytes | None:
        """Read a file by path relative to the .state/ repo root.

        Returns the file bytes, or None if the file is absent.
        """
        ...


@runtime_checkable
class StatingStateRepo(Protocol):
    """OPTIONAL: a repo that can say cheaply whether a state file changed (the message-log cache asks).

    Callers reach it with ``getattr(repo, "stat_state_file", None)`` and fall back to reading the file when it is absent, so a repo that
    lacks it (the sandbox backend) is still a :class:`StateRepo`; one that has it must answer as below.
    """

    async def stat_state_file(self, path: str) -> tuple[int, int] | None:
        """``(size, mtime_ns)`` of a file relative to the .state/ repo root, or None when it is absent."""
        ...


@runtime_checkable
class ListingStateRepo(Protocol):
    """OPTIONAL: a repo that can list a directory of the .state/ tree (the segmented session log asks for its ``messages*.jsonl`` files).

    Callers reach it with ``getattr(repo, "list_state_dir", None)`` and probe the segment names in order when it is absent.
    """

    async def list_state_dir(self, path: str) -> list[str]:
        """The file names directly under ``path`` (relative to the .state/ repo root); an absent directory lists nothing."""
        ...


@dataclass(frozen=True)
class LogRewrite:
    """A whole-file write of the session log's ACTIVE segment (a repo that cannot append, or the flag off): the caller read ``existing`` (the bytes of
    segment ``active_name``) under the session's messages lock and adds ``new``.

    The repo writes ``existing + new`` to the active segment, or, when that segment already passed the rotation size, only ``new`` to the next one.
    A ``ConflictError`` answers a call whose ``active_name`` is no longer the active segment (the log rotated since the caller read it).
    """

    active_name: str
    existing: bytes
    new: bytes


@runtime_checkable
class AppendingStateRepo(Protocol):
    """OPTIONAL: a repo whose ``commit`` can extend a file by appending (``messages.jsonl`` lines) instead of rewriting it.

    Callers check ``getattr(repo, "supports_append", False)`` before passing ``appends=`` to ``commit``; a repo without the flag is
    committed whole-file. A repo that sets it true must accept ``appends={relative path: bytes}`` on ``commit`` (the keyword is the only
    addition to :meth:`StateRepo.commit`).
    """

    supports_append: bool

    async def commit(
        self,
        session_id: str,
        *,
        summary: str,
        op: Op,
        tool: str | None = None,
        call_id: str | None = None,
        files: dict[str, str | bytes] | None = None,
        delete_files: list[str] | None = None,
        appends: dict[str, bytes] | None = None,
        log_rewrite: LogRewrite | None = None,
    ) -> str:
        """As :meth:`StateRepo.commit`, plus bytes added to the end of the named files under sessions/<session_id>/.

        The key ``"messages.jsonl"`` in ``appends`` (or a ``log_rewrite``) names the session LOG, not a file: the repo maps it to the active segment and
        rotates to the next one when the active segment has passed ``session_log_segment_bytes`` (design S4).
        """
        ...


__all__ = ["AppendingStateRepo", "ListingStateRepo", "StateRepo", "StatingStateRepo"]
