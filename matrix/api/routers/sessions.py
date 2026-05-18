"""Session REST surface — nested create + (later) cancel + top-level routes.

Task 19 ships only the nested ``POST /v1/workspaces/{wid}/sessions``
endpoint; Task 20 appends ``resume`` / ``pause`` / ``cancel`` plus the
top-level ``GET`` / ``find`` routes onto :data:`top_session_router`.
Both routers are mounted from ``matrix/api/app.py`` up-front so later
additions don't require a follow-up edit to ``app.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from matrix.api.deps import (
    get_agent_storage,
    get_graph_storage,
    get_scheduler,
    get_session_storage,
    get_workspace_registry,
    get_workspace_storage,
)
from matrix.api.errors import common_responses
from matrix.api.pagination import FindRequest, parse_order_by, parse_page
from matrix.model.except_ import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from matrix.model.session import (
    AgentBinding as OnDiskAgentBinding,
)
from matrix.model.session import (
    AgentSessionBinding,
    GraphSessionBinding,
    Session,
    SessionBinding,
    SessionStatus,
)
from matrix.model.storage import (
    FieldRef,
    Op,
    OrderBy,
    PageRequest,
    Predicate,
    Value,
)


logger = logging.getLogger(__name__)


nested_session_router = APIRouter(tags=["workspace-sessions"])
top_session_router = APIRouter(tags=["sessions"])


class SessionCreateBody(BaseModel):
    """Body of ``POST /v1/workspaces/{workspace_id}/sessions``.

    Mirrors spec §11.4: a discriminated-union ``binding`` selecting the
    Agent or Graph this session executes, plus optional initial-prompt /
    parent-session pointers and an ``auto_start`` flag that transitions
    the row to ``RUNNING`` and enqueues with the scheduler in one call.
    """

    binding: SessionBinding
    initial_instructions: str | None = None
    parent_session_id: str | None = None
    auto_start: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions",
    response_model=Session,
    status_code=201,
    summary="Create a new session attached to an agent or graph",
    responses=common_responses(404, 409, 422, 500),
)
async def create_session(
    body: SessionCreateBody,
    workspace_id: str = Path(...),
    workspaces=Depends(get_workspace_storage),
    sessions=Depends(get_session_storage),
    agents=Depends(get_agent_storage),
    graphs=Depends(get_graph_storage),
    scheduler=Depends(get_scheduler),
    workspace_registry=Depends(get_workspace_registry),
) -> Session:
    """Create a session bound to an agent or graph on this workspace.

    Steps (per spec §11.4):

    1. 404 when the workspace doesn't exist.
    2. 422 when the agent / graph referenced by the binding can't be
       resolved (binding-level semantic validation failure).
    3. Persist the row with ``status=CREATED``.
    4. For agent bindings, allocate the on-disk session slot inside the
       workspace via :meth:`Workspace.start_session` so the
       scheduler-visible Session row and the workspace's
       ``.state/sessions/<sid>/`` directory share the same id (spec
       §11.4 step 5). Graph bindings defer this — the graph executor
       wires its own per-node session slots.
    5. If ``auto_start``: bump status to ``RUNNING``, stamp
       ``started_at``, and enqueue with the scheduler.
    """
    workspace = await workspaces.get(workspace_id)
    if workspace is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")

    resolved_agent = None
    if isinstance(body.binding, AgentSessionBinding):
        resolved_agent = await agents.get(body.binding.agent_id)
        if resolved_agent is None:
            raise ValidationError(
                f"Agent {body.binding.agent_id!r} does not exist"
            )
    elif isinstance(body.binding, GraphSessionBinding):
        if await graphs.get(body.binding.graph_id) is None:
            raise ValidationError(
                f"Graph {body.binding.graph_id!r} does not exist"
            )

    sid = f"sess-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    session = Session(
        id=sid,
        workspace_id=workspace_id,
        binding=body.binding,
        status=SessionStatus.CREATED,
        parent_session_id=body.parent_session_id,
        initial_instructions=body.initial_instructions,
        metadata=body.metadata,
        created_at=now,
    )
    await sessions.create(session)

    # Allocate the on-disk session slot inside the workspace so the
    # scheduler-visible Session row and the workspace's
    # .state/sessions/<sid>/ directory share the same id (spec §11.4
    # step 5). Both agent and graph bindings get a holder slot —
    # graph bindings use a synthetic agent_id (``graph:<graph_id>``)
    # so the graph executor in matrix/worker/pool.py can compose the
    # workspace's tools into every per-node ToolExecutionManager.
    if isinstance(body.binding, AgentSessionBinding):
        assert resolved_agent is not None  # guarded above
        on_disk_binding = OnDiskAgentBinding(
            agent_id=resolved_agent.id,
            agent_name=resolved_agent.id,
            registered_tool_ids=list(resolved_agent.tools or []),
        )
        live_workspace = await workspace_registry.get_workspace(workspace_id)
        await live_workspace.start_session(
            on_disk_binding,
            id=sid,
            instructions=body.initial_instructions,
            parent_session_id=body.parent_session_id,
        )
    elif isinstance(body.binding, GraphSessionBinding):
        # Synthetic AgentBinding for the graph-holder slot. The
        # registered_tool_ids list is informational only — the per-node
        # tool managers register their own toolsets via the worker
        # pool's tool_manager_resolver. The workspace tools (ls/read/
        # write/exec/...) become available to every graph node via the
        # AgentSession the executor consumes.
        on_disk_binding = OnDiskAgentBinding(
            agent_id=f"graph:{body.binding.graph_id}",
            agent_name=f"graph:{body.binding.graph_id}",
            registered_tool_ids=[],
        )
        live_workspace = await workspace_registry.get_workspace(workspace_id)
        await live_workspace.start_session(
            on_disk_binding,
            id=sid,
            instructions=body.initial_instructions,
            parent_session_id=body.parent_session_id,
        )

    if body.auto_start:
        session.status = SessionStatus.RUNNING
        session.started_at = now
        await sessions.update(session)
        await scheduler.enqueue(sid)

    return session


# ===========================================================================
# Task 20 — resume / pause / cancel + top-level list / find / get
# ===========================================================================


_RESUMABLE = {SessionStatus.CREATED, SessionStatus.PAUSED, SessionStatus.WAITING}


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/resume",
    response_model=Session,
    summary="Idempotent start-or-resume",
    responses=common_responses(404, 409, 500),
)
async def resume_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
    scheduler=Depends(get_scheduler),
) -> Session:
    """Idempotent start-or-resume.

    * No-op (200) when the session is already RUNNING.
    * Transitions CREATED / PAUSED / WAITING → RUNNING, stamps
      ``started_at`` if unset, clears the pause flag, and enqueues with
      the scheduler.
    * 409 when the session is ENDED.
    """
    s = await sessions.get(session_id)
    if s is None or s.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    if s.status == SessionStatus.ENDED:
        raise ConflictError(f"Session {session_id!r} has ended")
    if s.status == SessionStatus.RUNNING:
        return s  # idempotent no-op
    if s.status in _RESUMABLE:
        s.status = SessionStatus.RUNNING
        if s.started_at is None:
            s.started_at = datetime.now(timezone.utc)
        s.pause_requested = False
        await sessions.update(s)
        await scheduler.enqueue(session_id)
        return s
    raise ConflictError(
        f"Session {session_id!r} cannot resume from status {s.status.value}"
    )


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/pause",
    status_code=204,
    summary="Soft pause request",
    responses=common_responses(404, 409, 500),
)
async def pause_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
) -> None:
    """Soft pause.

    * For sessions that no worker is holding a lease on (CREATED /
      WAITING) we transition directly to PAUSED.
    * For RUNNING sessions we set ``pause_requested=True`` and return —
      the worker will observe the flag at the next turn boundary and
      transition the row itself.
    * 409 when the session is already ENDED.
    """
    s = await sessions.get(session_id)
    if s is None or s.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    if s.status == SessionStatus.ENDED:
        raise ConflictError(f"Session {session_id!r} has ended")
    if s.status in {SessionStatus.WAITING, SessionStatus.CREATED}:
        s.status = SessionStatus.PAUSED
        await sessions.update(s)
        return
    s.pause_requested = True
    await sessions.update(s)


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/cancel",
    response_model=Session,
    summary="Hard cancel — transitions to ENDED/cancelled",
    responses=common_responses(404, 409, 500),
)
async def cancel_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
    scheduler=Depends(get_scheduler),
) -> Session:
    """Hard cancel.

    * For sessions no worker is leasing (CREATED / WAITING / PAUSED) we
      transition directly to ENDED with ``ended_reason='cancelled'``.
    * For RUNNING sessions we set the cancel flag and best-effort
      signal the worker holding the lease.
    * 409 when the session is already ENDED.
    """
    s = await sessions.get(session_id)
    if s is None or s.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    if s.status == SessionStatus.ENDED:
        raise ConflictError(f"Session {session_id!r} has ended")
    if s.status in {
        SessionStatus.CREATED,
        SessionStatus.WAITING,
        SessionStatus.PAUSED,
    }:
        s.status = SessionStatus.ENDED
        s.ended_reason = "cancelled"
        s.ended_at = datetime.now(timezone.utc)
        await sessions.update(s)
        return s
    s.cancel_requested = True
    await sessions.update(s)
    await scheduler.signal_cancel(session_id)
    return s


def _and(predicates: list[Predicate]) -> Predicate:
    """Left-fold a list of predicates into a single AND tree.

    The :class:`Predicate` tree is strictly binary, so multi-clause
    AND/OR expressions nest. Caller guarantees the list is non-empty.
    """
    out = predicates[0]
    for p in predicates[1:]:
        out = Predicate(left=out, op=Op.AND, right=p)
    return out


@top_session_router.get(
    "/sessions",
    summary="List sessions across workspaces (optionally filtered)",
    responses=common_responses(400, 422, 500),
)
async def list_sessions(
    page: PageRequest = Depends(parse_page),
    order_by: list[OrderBy] | None = Depends(parse_order_by),
    status: Annotated[
        SessionStatus | None,
        Query(description="Filter by session status."),
    ] = None,
    workspace_id: Annotated[
        str | None,
        Query(description="Filter by workspace_id."),
    ] = None,
    agent_id: Annotated[
        str | None,
        Query(
            description=(
                "Filter by binding.agent_id. Only matches sessions whose "
                "binding kind is 'agent'; graph-bound sessions never "
                "satisfy this filter. Translated by the storage layer to "
                "a nested-JSON path lookup; backends that cannot express "
                "such paths reject the request with 400."
            ),
        ),
    ] = None,
    parent_session_id: Annotated[
        str | None,
        Query(description="Filter by parent_session_id."),
    ] = None,
    worker_id: Annotated[
        str | None,
        Query(
            description=(
                "Filter by the id of the worker that last held the "
                "session lease (`last_worker_id`). Useful for the "
                "Workers UI page to list which sessions a given worker "
                "is currently processing or has recently touched."
            ),
        ),
    ] = None,
    sessions=Depends(get_session_storage),
):
    """List sessions across workspaces, optionally filtered.

    Per spec §11.2. When no filter query params are supplied, falls
    back to a plain paginated list. When any filter is supplied, builds
    an AND-joined predicate and dispatches to :meth:`Storage.find`.
    """
    filters: list[Predicate] = []
    if status is not None:
        filters.append(
            Predicate(
                left=FieldRef(name="status"),
                op=Op.EQ,
                right=Value(value=status.value),
            )
        )
    if workspace_id is not None:
        filters.append(
            Predicate(
                left=FieldRef(name="workspace_id"),
                op=Op.EQ,
                right=Value(value=workspace_id),
            )
        )
    if parent_session_id is not None:
        filters.append(
            Predicate(
                left=FieldRef(name="parent_session_id"),
                op=Op.EQ,
                right=Value(value=parent_session_id),
            )
        )
    if agent_id is not None:
        # Nested JSONB path; the Postgres backend translates this to
        # ``data->'binding'->>'agent_id'``. Backends that cannot express
        # nested paths will reject the predicate with 400.
        filters.append(
            Predicate(
                left=FieldRef(name="binding.agent_id"),
                op=Op.EQ,
                right=Value(value=agent_id),
            )
        )
    if worker_id is not None:
        filters.append(
            Predicate(
                left=FieldRef(name="last_worker_id"),
                op=Op.EQ,
                right=Value(value=worker_id),
            )
        )
    if filters:
        return await sessions.find(_and(filters), page, order_by=order_by)
    return await sessions.list(page, order_by=order_by)


@top_session_router.post(
    "/sessions/find",
    summary="Find sessions with predicate",
    responses=common_responses(400, 422, 500),
)
async def find_sessions(
    body: FindRequest,
    sessions=Depends(get_session_storage),
):
    return await sessions.find(body.predicate, body.page, order_by=body.order_by)


@top_session_router.get(
    "/sessions/{session_id}",
    response_model=Session,
    summary="Get session by id (no workspace context required)",
    responses=common_responses(404, 500),
)
async def get_session_by_id(
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
) -> Session:
    s = await sessions.get(session_id)
    if s is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    return s


__all__ = [
    "SessionCreateBody",
    "cancel_session",
    "create_session",
    "find_sessions",
    "get_session_by_id",
    "list_sessions",
    "nested_session_router",
    "pause_session",
    "resume_session",
    "top_session_router",
]
