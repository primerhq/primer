"""Read surface for invoker-supplied (external) tool calls.

Write paths live on the invocation endpoints (session steer / chat
send); this router only exposes discovery and audit:

* per-conversation pending lists (what the invoker must answer),
* the global cross-conversation list (the orchestrator poll point and
  the audit trail of resolved calls).

Timeout is materialised lazily here: worker resume hooks have no
storage handle, so any read that touches a pending row whose
``timeout_at`` passed flips it to ``timed_out`` first (the park itself
is resumed by the existing ``parked_until`` sweeper).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from primer.api.deps import get_external_tool_call_storage
from primer.api.errors import common_responses
from primer.model.external_tool import ExternalToolCall
from primer.model.storage import OffsetPage
from primer.storage.q import Q

external_tools_router = APIRouter(tags=["external-tools"])


class PendingExternalCall(BaseModel):
    """One pending call as the invoker sees it."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    timeout_at: str | None = None
    node_id: str | None = None


async def sweep_expired(storage, rows: list[ExternalToolCall]) -> None:
    """Flip pending rows whose ``timeout_at`` has passed to timed_out."""
    now = datetime.now(UTC)
    for row in rows:
        if row.status == "pending" and row.timeout_at and row.timeout_at < now:
            row.status = "timed_out"
            row.result = {"timed_out": True}
            row.is_error = True
            row.resolved_at = now
            await storage.update(row)


async def _pending_for(storage, *, field: str, value: str) -> dict:
    q = Q(ExternalToolCall).where(field, value).where("status", "pending")
    page = await storage.find(q.build(), OffsetPage(offset=0, length=200))
    rows = list(page.items)
    await sweep_expired(storage, rows)
    items = [
        PendingExternalCall(
            tool_call_id=r.tool_call_id,
            tool_name=r.tool_name,
            arguments=r.arguments,
            created_at=r.created_at.isoformat() if r.created_at else None,
            timeout_at=r.timeout_at.isoformat() if r.timeout_at else None,
            node_id=r.node_id,
        )
        for r in rows
        if r.status == "pending"
    ]
    return {"items": [i.model_dump() for i in items]}


@external_tools_router.get(
    "/sessions/{session_id}/external_tools/pending",
    summary="Pending external tool calls for one session",
    responses=common_responses(500),
)
async def session_pending(
    session_id: str = Path(...),
    storage=Depends(get_external_tool_call_storage),
) -> dict:
    return await _pending_for(storage, field="session_id", value=session_id)


@external_tools_router.get(
    "/chats/{chat_id}/external_tools/pending",
    summary="Pending external tool calls for one chat",
    responses=common_responses(500),
)
async def chat_pending(
    chat_id: str = Path(...),
    storage=Depends(get_external_tool_call_storage),
) -> dict:
    return await _pending_for(storage, field="chat_id", value=chat_id)


@external_tools_router.get(
    "/external_tool_calls",
    summary="Global external tool call list (audit + orchestrator poll)",
    responses=common_responses(500),
)
async def list_external_tool_calls(
    status: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    chat_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    storage=Depends(get_external_tool_call_storage),
) -> dict:
    # The status filter is applied post-sweep on purpose: a pending row
    # whose deadline passed must be reported (and persisted) as
    # timed_out regardless of which status the caller asked for.
    q = Q(ExternalToolCall)
    predicate = None
    if session_id:
        q = q.where("session_id", session_id)
        predicate = q.build()
    if chat_id:
        q = q.where("chat_id", chat_id)
        predicate = q.build()
    if predicate is not None:
        page = await storage.find(
            predicate, OffsetPage(offset=offset, length=limit)
        )
    else:
        page = await storage.list(OffsetPage(offset=offset, length=limit))
    rows = list(page.items)
    await sweep_expired(storage, rows)
    items = [r.model_dump(mode="json") for r in rows]
    if status:
        items = [i for i in items if i["status"] == status]
    return {
        "items": items,
        "total": page.total if not status else len(items),
        "offset": offset,
        "limit": limit,
    }


__all__ = ["external_tools_router", "sweep_expired"]
