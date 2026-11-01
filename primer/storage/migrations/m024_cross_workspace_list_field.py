"""Migration 24: record the workspace on external tool calls and tool approval records (access-control Phase 2, Task 5).

The cross-workspace lists (``GET /v1/external_tool_calls``, ``GET /v1/tool_approval/records``) filter ``workspace_id IN <the caller's readable
workspaces>`` in the query, so each row must carry the workspace of the session it belongs to. New rows are stamped where they are created; this
step stamps the rows written before: ``workspace_id`` is read off the row's session. A row with no session (a chat call or gate), or whose session
no longer exists, is left without one (only an admin lists it). A row that already carries a ``workspace_id`` is left alone, so re-running
converges. Only the rows the step changes are read (no workspace, a session), through migration-local views (``extra="allow"``), and
each is stamped with ``patch_if``, which writes the one field and leaves the rest of the document as stored (PERF-T16).
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.storage.migrations._rows import iter_rows
from primer.model.common import Identifiable
from primer.storage.q import Q

logger = logging.getLogger(__name__)


class _Owned(Identifiable):
    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    workspace_id: str | None = None


# The class name selects the table (the storage layer lowercases it).
class ExternalToolCall(_Owned):  # noqa: N801
    pass


class ToolApprovalRecord(_Owned):  # noqa: N801
    pass


class WorkspaceSession(Identifiable):  # noqa: N801 - maps to the ``sessions`` table
    model_config = ConfigDict(extra="allow")

    workspace_id: str | None = None


class M024CrossWorkspaceListField:
    """Stamp ``workspace_id`` on external tool calls and approval records from their session."""

    version = 24
    description = "record the workspace on external tool calls and tool approval records"

    async def apply(self, sp: StorageProvider) -> None:
        sessions = sp.get_storage(WorkspaceSession)
        workspace_of: dict[str, str | None] = {}
        for model in (ExternalToolCall, ToolApprovalRecord):
            store = sp.get_storage(model)
            count = 0
            unstamped = Q(model).where_null("workspace_id").where_not_null("session_id").build()
            async for row in iter_rows(store, predicate=unstamped):
                if row.session_id not in workspace_of:
                    session = await sessions.get(row.session_id)
                    workspace_of[row.session_id] = session.workspace_id if session is not None else None
                workspace_id = workspace_of[row.session_id]
                if workspace_id is None:
                    continue
                if await store.patch_if(row.id, {"workspace_id": workspace_id}, where={"workspace_id": [None]}) is not None:
                    count += 1
            if count:
                logger.info("recorded the workspace on rows", extra={"model": model.__name__, "rows": count})


__all__ = ["M024CrossWorkspaceListField"]
