"""The typed refusal of a workspace the deployment's topology forbids (ticket 01a1072f).

Its own leaf module so the API's error map can import it without loading the workspace
runtime, and so :mod:`primer.model.except_` (a CRLF file) stays untouched.
"""

from __future__ import annotations

from primer.model.except_ import ConflictError


class WorkspaceRefusedError(ConflictError):
    """A workspace on a local provider was asked for where no local workspace may be used.

    A local workspace's write lock exists only inside one process, so an API process and a worker
    process (or two workers) writing the same directory would not exclude each other. The deployment
    refuses to hand one out instead of letting that happen quietly.

    This is a REFUSAL, not a loss: the workspace and its sessions are intact on disk and become usable
    again once the workspace moves to a docker or kubernetes provider (or the deployment is single
    process). Nothing that catches it may treat it as ``workspace_lost``: the probe must not count it
    as a miss, and a turn that meets it must leave its session resumable.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str | None = None,
        workspace_id: str | None = None,
        signals: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.workspace_id = workspace_id
        self.signals = signals

    def for_workspace(self, workspace_id: str) -> "WorkspaceRefusedError":
        """The same refusal, naming the workspace the caller was resolving (the gate itself only sees a provider)."""
        return WorkspaceRefusedError(
            f"workspace {workspace_id!r}: {self.message}",
            provider_id=self.provider_id,
            workspace_id=workspace_id,
            signals=self.signals,
        )

    @property
    def problem_extensions(self) -> dict[str, object]:
        """Structured fields the RFC7807 envelope carries, so a client can tell this 409 from any other."""
        extensions: dict[str, object] = {"signals": list(self.signals)}
        if self.provider_id is not None:
            extensions["provider_id"] = self.provider_id
        if self.workspace_id is not None:
            extensions["workspace_id"] = self.workspace_id
        return extensions


__all__ = ["WorkspaceRefusedError"]
