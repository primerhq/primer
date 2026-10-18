"""The typed refusal of a workspace the deployment's topology forbids (ticket 01a1072f).

Its own leaf module so the API's error map can import it without loading the workspace
runtime, and so :mod:`primer.model.except_` (a CRLF file) stays untouched.
"""

from __future__ import annotations

from primer.model.except_ import ConflictError, PrimerError


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


class ReservedWorkspaceError(PrimerError):
    """The default workspace (prime) was asked to be deleted.

    Raised by :meth:`primer.api.registries.workspace_registry.WorkspaceRegistry.destroy`, the one funnel every workspace delete
    passes, before it reads the row or touches a backend. The REST route turns it into a 403 ``reserved_workspace`` and the
    ``delete_workspace`` tool into a ``forbidden`` result; neither lets it escape (it is not in the API's error map, so an
    uncaught one would be a 500). Nothing is torn down and no session is ended when it is raised.

    The default workspace is the one the console opens on first login and the home of the prime operator, so it is protected
    for as long as it is the default. Renaming it stays allowed: only the delete is refused.
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace {workspace_id!r} is the default workspace (prime) and cannot be deleted")
        self.workspace_id = workspace_id

    @property
    def problem_extensions(self) -> dict[str, object]:
        """Structured fields for the RFC7807 envelope, so a client can tell this refusal from any other 403."""
        return {"error": "reserved_workspace", "workspace_id": self.workspace_id}


__all__ = ["ReservedWorkspaceError", "WorkspaceRefusedError"]
