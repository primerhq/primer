"""``WorkspaceIO`` shim for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). This
is a pure adapter class — it takes a workspace registry at construction and
carries NO reference to the :class:`~primer.worker.pool.WorkerPool`, so unlike
the other extracted helpers it needs no ``pool`` argument.

Re-exported from ``primer.worker.pool`` so existing importers
(``tests/worker/test_pool.py``) keep resolving
``primer.worker.pool._WorkspaceIOShim``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class _WorkspaceIOShim:
    """``WorkspaceIO`` adapter that delegates to the workspace runtime.

    Satisfies the :class:`primer.session.persistence.WorkspaceIO` protocol
    used by :class:`WorkspaceMessageWriter`.

    Dispatch: resolves the workspace from the registry and calls
    ``workspace.append_message_line(session_id, line)`` directly.  Every
    concrete :class:`primer.int.workspace.Workspace` backend now implements
    this method (added in Task 9).

    The shim calls ``_workspace_registry.get_workspace(workspace_id)`` on
    each flush so hot-reloaded workspace instances are picked up during
    long-lived workers.  Because the session_id alone is enough to identify
    the session slot inside the workspace, but the workspace_id is needed to
    locate the workspace, the shim tracks the mapping via a lightweight
    ``_session_to_workspace`` dict populated via :meth:`register_session`
    before the first write.
    """

    def __init__(self, workspace_registry) -> None:
        self._registry = workspace_registry
        # session_id -> workspace_id mapping; populated via register_session()
        # from the _build_session_executor path before any append is called.
        self._session_to_workspace: dict[str, str] = {}

    def register_session(self, session_id: str, workspace_id: str) -> None:
        """Pre-register the workspace_id for a session (called by the pool)."""
        self._session_to_workspace[session_id] = workspace_id

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append ``line`` to the session's ``messages.jsonl`` via the workspace runtime."""
        if self._registry is None:
            logger.warning(
                "_WorkspaceIOShim: no workspace_registry configured; "
                "dropping %d bytes for session %s",
                len(line), session_id,
            )
            return

        workspace_id = self._session_to_workspace.get(session_id)
        if workspace_id is None:
            logger.warning(
                "_WorkspaceIOShim: no workspace_id registered for session %s; "
                "dropping %d bytes",
                session_id, len(line),
            )
            return

        workspace = await self._registry.get_workspace(workspace_id)
        if workspace is None:
            logger.warning(
                "_WorkspaceIOShim: workspace %r not found for session %s; "
                "dropping %d bytes",
                workspace_id, session_id, len(line),
            )
            return

        await workspace.append_message_line(session_id, line)

    def workspace_id_for(self, session_id: str) -> str | None:
        """Public lookup for the workspace id bound to a session.

        Replaces direct reads of the private ``_session_to_workspace``
        dict by call sites that need to resolve a session's workspace
        (e.g. dispatch's turn-log factory closure).
        """
        return self._session_to_workspace.get(session_id)

    async def append_state_line(
        self, workspace_id: str, state_relative_path: str, line: bytes,
    ) -> None:
        """Append ``line`` to ``<workspace.state_path>/<state_relative_path>``.

        Resolves the workspace via the registry, prepends the workspace's
        own ``state_path`` (so operators can override the default
        ``.state`` via :class:`WorkspaceTemplate` without losing the
        writer/reader path agreement), then delegates to the backend's
        ``append_state_line``. Logs and drops the bytes if the registry
        is absent or the workspace can't be resolved (mirroring
        ``append_message_line``'s best-effort policy).
        """
        if self._registry is None:
            logger.warning(
                "_WorkspaceIOShim: no workspace_registry configured; "
                "dropping %d state bytes for workspace %s",
                len(line), workspace_id,
            )
            return
        workspace = await self._registry.get_workspace(workspace_id)
        if workspace is None:
            logger.warning(
                "_WorkspaceIOShim: workspace %r not found; "
                "dropping %d state bytes",
                workspace_id, len(line),
            )
            return
        state_path = getattr(workspace, "state_path", ".state")
        full_path = f"{state_path}/{state_relative_path}"
        try:
            await workspace.append_state_line(full_path, line)
        except NotImplementedError:
            # Backend without turn-log support; silently no-op so
            # the dispatch doesn't bubble the failure.
            logger.debug(
                "_WorkspaceIOShim: workspace %r has no append_state_line; "
                "dropping %d bytes", workspace_id, len(line),
            )

    async def read_state_file(
        self, workspace_id: str, state_relative_path: str,
    ) -> bytes:
        """Read ``<workspace.state_path>/<state_relative_path>`` from the workspace.

        Returns ``b""`` when the workspace is gone, the path doesn't
        exist, or any other backend error fires. Used by the turn-log
        writer's lazy bootstrap so the same path-resolution rule
        applies to both reads and writes.
        """
        if self._registry is None:
            return b""
        workspace = await self._registry.get_workspace(workspace_id)
        if workspace is None:
            return b""
        state_path = getattr(workspace, "state_path", ".state")
        full_path = f"{state_path}/{state_relative_path}"
        try:
            return await workspace.read_file(full_path)
        except Exception:  # noqa: BLE001
            return b""
