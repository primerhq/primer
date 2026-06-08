"""Formal StateRepo contract for workspace state backends.

LocalStateRepo (primer.workspace.local.state) and SandboxStateRepo
(primer.workspace.sandbox.state) both implement this Protocol.
Conformance is verified by tests/workspace/test_state_repo_conformance.py.

This module is type-only; it does not import any concrete implementation.
"""

from __future__ import annotations

import typing
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
    ) -> list[CommitInfo]:
        """Return commits, optionally filtered by session or agent. Newest first."""
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


__all__ = ["StateRepo"]
