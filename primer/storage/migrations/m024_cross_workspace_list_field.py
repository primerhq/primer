"""Migration 24: record the workspace on external tool calls and tool approval records (access-control Phase 2, Task 5).

The cross-workspace lists (``GET /v1/external_tool_calls``, ``GET /v1/tool_approval/records``) filter ``workspace_id IN <the caller's readable
workspaces>`` in the query, so each row must carry the workspace of the session it belongs to. New rows are stamped where they are created; this
step stamps the rows written before: ``workspace_id`` is read off the row's session. A row with no session (a chat call or gate), or whose session
no longer exists, is left without one (only an admin lists it). A row that already carries a ``workspace_id`` is left alone, so re-running
converges. Rows are read in full through migration-local views (``extra="allow"``) before any write, as ``m021`` does.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200


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


async def _read_all(store) -> list[Any]:
    rows: list[Any] = []
    offset = 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=_PAGE))
        rows.extend(page.items)
        if len(page.items) < _PAGE:
            return rows
        offset += _PAGE


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
            for row in await _read_all(store):
                if row.workspace_id is not None or row.session_id is None:
                    continue
                if row.session_id not in workspace_of:
                    session = await sessions.get(row.session_id)
                    workspace_of[row.session_id] = session.workspace_id if session is not None else None
                workspace_id = workspace_of[row.session_id]
                if workspace_id is None:
                    continue
                await store.update(row.model_copy(update={"workspace_id": workspace_id}))
                count += 1
            if count:
                logger.info("recorded the workspace on rows", extra={"model": model.__name__, "rows": count})


__all__ = ["M024CrossWorkspaceListField"]
