"""Session REST surface — nested create + (later) cancel + top-level routes.

Task 19 ships only the nested ``POST /v1/workspaces/{wid}/sessions``
endpoint; Task 20 appends ``resume`` / ``pause`` / ``cancel`` plus the
top-level ``GET`` / ``find`` routes onto :data:`top_session_router`.
Both routers are mounted from ``primer/api/app.py`` up-front so later
additions don't require a follow-up edit to ``app.py``.

Task 10 adds the WS endpoint at
``WS /v1/workspaces/{wid}/sessions/{sid}/ws?cursor=N``.
The session WS mirrors the chat WS: cursor replay of ``messages.jsonl``
followed by live tick subscriptions.  The source of truth is the
per-session ``messages.jsonl`` file in the workspace (unlike chat, which
stores messages in the database).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_agent_storage,
    get_claim_engine,
    get_event_bus,
    get_graph_storage,
    get_scheduler,
    get_session_storage,
    get_storage_provider,
    get_workspace_registry,
    get_workspace_storage,
)
from primer.api.errors import common_responses
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics
from primer.api.pagination import FindRequest, parse_order_by, parse_page
from primer.model.except_ import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from primer.model.workspace_session import (
    AgentBinding as OnDiskAgentBinding,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    WorkspaceSession,
    SessionBinding,
    SessionStatus,
)
from primer.model.storage import (
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
    graph_input: Any | None = Field(
        default=None,
        description=(
            "Input value for graph bindings. Validated against the "
            "graph's Begin.input_schema when set. For graphs without a "
            "schema, accepted shapes are str, list[Message], or dict. "
            "Persisted to ``session.metadata['graph_input']`` so the "
            "workspace graph executor can pick it up as the initial "
            "input."
        ),
    )


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions",
    response_model=WorkspaceSession,
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
    engine=Depends(get_claim_engine),
    storage_provider=Depends(get_storage_provider),
) -> WorkspaceSession:
    """Create a session bound to an agent or graph on this workspace.

    Steps (per spec §11.4):

    1. 404 when the workspace doesn't exist.
    2. 422 when the agent / graph referenced by the binding can't be
       resolved (binding-level semantic validation failure).
    3. Persist the row with ``status=CREATED``.
    4. Allocate the on-disk session slot inside the workspace via
       :meth:`Workspace.start_session` so the scheduler-visible Session
       row and the workspace's ``.state/sessions/<sid>/`` directory
       share the same id (spec §11.4 step 5). Agent bindings get a
       slot keyed by the resolved agent. Graph bindings get a *holder*
       slot whose synthetic agent_id is ``graph:<graph_id>`` — the
       graph executor (primer/worker/pool.py) looks the holder up via
       :meth:`Workspace.get_session` and composes the workspace's
       tools into every per-node ``ToolExecutionManager``. Without the
       holder, graph-bound sessions cannot access workspace tools.
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
        resolved_graph = await graphs.get(body.binding.graph_id)
        if resolved_graph is None:
            raise ValidationError(
                f"Graph {body.binding.graph_id!r} does not exist"
            )
        # Pre-validate graph_input against the graph's Begin.input_schema
        # before persisting the session row. The workspace executor reads
        # ``session.metadata['graph_input']`` as the initial input, so any
        # shape mismatch must surface as a 422 at create time rather than
        # blowing up the first turn. When no Begin or no schema is
        # present we accept whatever was sent (back-compat).
        import jsonschema as _jsonschema  # local import keeps top clean
        from primer.model.graph import _BeginNode

        begin = next(
            (n for n in resolved_graph.nodes if isinstance(n, _BeginNode)),
            None,
        )
        if begin is not None and begin.input_schema is not None:
            resolved_input: Any | None = body.graph_input
            if resolved_input is None and body.initial_instructions:
                # Legacy fallback: parse initial_instructions as JSON so
                # callers that still drive graphs through that field
                # continue to work.
                try:
                    resolved_input = json.loads(body.initial_instructions)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        "initial_instructions for graph with "
                        "input_schema must be valid JSON (or pass "
                        "graph_input directly)"
                    ) from exc
            if resolved_input is None:
                raise ValidationError(
                    f"graph {body.binding.graph_id!r} requires graph_input"
                )
            try:
                _jsonschema.validate(
                    instance=resolved_input,
                    schema=begin.input_schema,
                )
            except _jsonschema.ValidationError as exc:
                raise ValidationError(
                    f"graph_input invalid at path "
                    f"{list(exc.absolute_path)!r}: {exc.message}"
                ) from exc
            # Normalise so the persistence block below writes the
            # validated value (covers the legacy
            # initial_instructions-as-JSON fallback path).
            body.graph_input = resolved_input

    # Pre-generate the sid so we can allocate the on-disk slot BEFORE
    # the factory's auto_start path makes the row claimable. Spec §12.5
    # (Plan §3.2): persist + auto-start + claim registration live in
    # primer.workspace.session_factory so the trigger dispatcher and
    # this REST handler share one canonical create path.
    sid = f"sess-{uuid.uuid4().hex[:12]}"

    # Allocate the on-disk session slot inside the workspace so the
    # scheduler-visible Session row and the workspace's
    # .state/sessions/<sid>/ directory share the same id (spec §11.4
    # step 5). Both agent and graph bindings get a holder slot —
    # graph bindings use a synthetic agent_id (``graph:<graph_id>``)
    # so the graph executor in primer/worker/pool.py can compose the
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

    # Persist the row + (optionally) auto-start + always register a
    # forward-compat ClaimEngine upsert via the shared service helper.
    from primer.workspace.session_factory import (
        SessionFactoryDeps,
        create_session as _persist_session,
    )

    session = await _persist_session(
        workspace_id=workspace_id,
        binding=body.binding,
        initial_instructions=body.initial_instructions,
        graph_input=body.graph_input,
        auto_start=body.auto_start,
        metadata=body.metadata,
        parent_session_id=body.parent_session_id,
        session_id=sid,
        deps=SessionFactoryDeps(
            storage_provider=storage_provider,
            claim_engine=engine,
            scheduler=scheduler,
            workspace_registry=None,
        ),
    )
    return session


# ===========================================================================
# Task 20 — resume / pause / cancel + top-level list / find / get
# ===========================================================================


_RESUMABLE = {SessionStatus.CREATED, SessionStatus.PAUSED, SessionStatus.WAITING}


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions/{session_id}/resume",
    response_model=WorkspaceSession,
    summary="Idempotent start-or-resume",
    responses=common_responses(404, 409, 500),
)
async def resume_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
) -> WorkspaceSession:
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
        # Notify the ClaimEngine (forward-compat; no-op when not wired).
        if engine is not None:
            from primer.int.claim import ClaimKind
            await engine.upsert(ClaimKind.SESSION, session_id)
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
    response_model=WorkspaceSession,
    summary="Hard cancel — transitions to ENDED/cancelled",
    responses=common_responses(404, 409, 500),
)
async def cancel_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
) -> WorkspaceSession:
    """Hard cancel.

    * For sessions no worker is leasing (CREATED / WAITING / PAUSED) we
      transition directly to ENDED with ``ended_reason='cancelled'``.
    * For RUNNING sessions we set the cancel flag and publish the
      ``session:{sid}:cancel`` event bus key — the engine-path worker's
      ``_cancel_watcher`` (``primer/session/dispatch.py``) listens on
      that key and preempts the running turn. We also call the
      legacy ``scheduler.signal_cancel`` for backward compat with the
      pre-engine claim path.
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
        # Drop the lease — session is gone, no point claiming it.
        if engine is not None:
            from primer.int.claim import ClaimKind
            await engine.delete_lease(ClaimKind.SESSION, session_id)
        return s
    now = datetime.now(timezone.utc)
    s.cancel_requested = True
    s.cancel_requested_at = now
    await sessions.update(s)
    # Publish on the bus so the engine-path worker's _cancel_watcher
    # preempts the running turn. The WS interrupt handler at line ~820
    # publishes the same key.
    if event_bus is not None:
        try:
            await event_bus.publish(f"session:{session_id}:cancel", {})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cancel_session: event_bus.publish failed (legacy path still signalled)",
                extra={"session_id": session_id, "exception": type(exc).__name__},
            )
    await scheduler.signal_cancel(session_id)
    return s


@nested_session_router.delete(
    "/workspaces/{workspace_id}/sessions/{session_id}",
    status_code=204,
    summary="Permanently delete a session (auto-cancels non-RUNNING)",
    responses=common_responses(404, 409, 500),
)
async def delete_session(
    workspace_id: str = Path(...),
    session_id: str = Path(...),
    force: bool = Query(
        False,
        description=(
            "Force-delete a RUNNING session — bypass the 409 gate that "
            "normally protects against a worker writing back to a "
            "deleted row. Use only to evict orphaned / stuck rows where "
            "no worker is actually executing (e.g. after the previous "
            "API process died mid-turn)."
        ),
    ),
    sessions=Depends(get_session_storage),
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
    workspace_registry=Depends(get_workspace_registry),
    event_bus=Depends(get_event_bus),
) -> None:
    """Permanently remove a session row + best-effort cleanup of its
    on-disk slot under ``<workspace>/.state/sessions/<sid>/``.

    For CREATED/WAITING/PAUSED rows we transition to ENDED inline (no
    worker is holding the lease, so the cleanup is safe to do in this
    request). ENDED / FAILED / CANCELLED rows are removed as-is.
    RUNNING rows return 409 — a worker holds the lease and would
    write back to a deleted row; the caller must POST /cancel and
    wait for the worker to land in ENDED first. Pass ``?force=true``
    to override (e.g. when the worker is provably dead).

    The on-disk slot cleanup is best-effort: if the workspace is
    unreachable (e.g. its backing storage was wiped), the row is
    still removed.
    """
    s = await sessions.get(session_id)
    if s is None or s.workspace_id != workspace_id:
        raise NotFoundError(
            f"Session {session_id!r} does not exist on workspace "
            f"{workspace_id!r}"
        )
    if s.status == SessionStatus.RUNNING and not force:
        raise ConflictError(
            f"Session {session_id!r} is running; cancel it first "
            "(POST /cancel) before deleting, or pass ?force=true to "
            "evict an orphaned row"
        )
    if s.status == SessionStatus.RUNNING and force:
        # Publish cancel so any worker actually holding the lease
        # preempts cleanly before its complete_turn CAS. Best-effort —
        # if the bus publish fails we still proceed with the delete
        # (force semantics).
        if event_bus is not None:
            try:
                await event_bus.publish(f"session:{session_id}:cancel", {})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "delete_session(force): event_bus.publish failed",
                    extra={
                        "session_id": session_id,
                        "exception": type(exc).__name__,
                    },
                )
        s.status = SessionStatus.ENDED
        s.ended_reason = "force_deleted"
        s.ended_at = datetime.now(timezone.utc)
        await sessions.update(s)
        if engine is not None:
            from primer.int.claim import ClaimKind
            await engine.delete_lease(ClaimKind.SESSION, session_id)

    # CREATED / WAITING / PAUSED: nobody's holding a lease, so we can
    # transition to ENDED inline. Drop any stale lease and signal the
    # scheduler — symmetric with cancel_session's CREATED/WAITING/PAUSED
    # branch, then the row gets removed below.
    if s.status in {
        SessionStatus.CREATED,
        SessionStatus.WAITING,
        SessionStatus.PAUSED,
    }:
        s.status = SessionStatus.ENDED
        s.ended_reason = "cancelled"
        s.ended_at = datetime.now(timezone.utc)
        await sessions.update(s)
        if engine is not None:
            from primer.int.claim import ClaimKind
            await engine.delete_lease(ClaimKind.SESSION, session_id)
        # Best-effort scheduler notification so any in-flight bookkeeping
        # can react. Don't fail the delete if it raises.
        try:
            await scheduler.signal_cancel(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "delete_session: scheduler.signal_cancel failed",
                extra={
                    "session_id": session_id,
                    "exception": type(exc).__name__,
                },
            )

    # Best-effort: reap the on-disk session slot AND drop the
    # in-memory session handle so list_sessions() stops returning it.
    try:
        live_workspace = await workspace_registry.get_workspace(workspace_id)
        # Unbind the in-memory handle first — the workspace exposes the
        # ABC method since the diagnostic-report follow-up landed.
        try:
            await live_workspace.remove_session(session_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "delete_session: remove_session raised; continuing",
                exc_info=True,
            )
        state_root = getattr(live_workspace, "_state", None)
        state_path = getattr(state_root, "path", None) if state_root else None
        if state_path is not None:
            import shutil
            session_dir = state_path / "sessions" / session_id
            if session_dir.exists():
                await asyncio.to_thread(
                    shutil.rmtree, session_dir, ignore_errors=True
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delete_session: on-disk cleanup failed (row still removed)",
            extra={
                "session_id": session_id,
                "workspace_id": workspace_id,
                "exception": type(exc).__name__,
                "message": str(exc),
            },
        )

    await sessions.delete(session_id)


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
    response_model=WorkspaceSession,
    summary="Get session by id (no workspace context required)",
    responses=common_responses(404, 500),
)
async def get_session_by_id(
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
) -> WorkspaceSession:
    s = await sessions.get(session_id)
    if s is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    return s


@top_session_router.get(
    "/sessions/{session_id}/turn_log",
    summary="Read the session's per-turn structured log",
    responses=common_responses(404, 500),
)
async def get_session_turn_log(
    session_id: str = Path(..., description="Session id"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    since_seq: int | None = Query(default=None, ge=0),
    sessions=Depends(get_session_storage),
    workspace_registry=Depends(get_workspace_registry),
) -> dict:
    """Return the JSONL-encoded turn-log events for this session.

    Reads ``<state_path>/sessions/<session_id>/turns.jsonl`` via the
    workspace runtime's :meth:`read_file`. Pagination is offset-based;
    ``since_seq`` skips events with ``seq <= since_seq`` so polling
    clients can ask for "everything new since the last frame".
    """
    sess = await sessions.get(session_id)
    if sess is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    workspace = await workspace_registry.get_workspace(sess.workspace_id)
    if workspace is None:
        # Workspace gone (deleted, lost). Surface an empty log instead
        # of 5xx so the UI can still render the tab.
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    state_path = getattr(
        getattr(workspace, "_template", None), "state_path", ".state",
    )
    rel = f"{state_path}/sessions/{session_id}/turns.jsonl"
    return await _read_workspace_turn_log(
        workspace=workspace,
        relative_path=rel,
        limit=limit,
        offset=offset,
        since_seq=since_seq,
    )


async def _read_workspace_turn_log(
    *,
    workspace,
    relative_path: str,
    limit: int,
    offset: int,
    since_seq: int | None,
) -> dict:
    """JSONL-parse the file at ``relative_path`` inside ``workspace``.

    Missing file is treated as an empty log (a fresh session that's
    written nothing yet). Bogus lines are skipped silently — the turn
    log is observability data, not a contract.
    """
    try:
        raw = await workspace.read_file(relative_path)
    except Exception:  # noqa: BLE001 — NotFoundError / IO / decode
        raw = b""
    items: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if since_seq is not None and int(obj.get("seq", 0)) <= since_seq:
            continue
        items.append(obj)
    total = len(items)
    window = items[offset:offset + limit]
    return {
        "items": window,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


# ===========================================================================
# WebSocket: live session stream + interrupt / tool_approval / ping
# ===========================================================================


async def _session_replay_since_cursor(
    ws: WebSocket,
    workspace,
    session_id: str,
    cursor: int,
) -> int:
    """Replay all ``messages.jsonl`` records with ``seq > cursor``.

    Reads the session's ``messages.jsonl`` via the workspace's
    ``read_file`` method, line-iterates, and sends each record whose
    ``seq > cursor`` to the WebSocket as JSON.

    Returns the highest seq sent (or ``cursor`` if nothing was sent),
    so the caller knows where live streaming should resume from.

    The ``messages.jsonl`` file may not exist yet (new session) — treat
    a missing file as an empty history and return ``cursor``.
    """
    from primer.model.except_ import NotFoundError as _NotFoundError

    state_path = getattr(getattr(workspace, "_template", None), "state_path", ".state")
    jsonl_path = f"{state_path}/sessions/{session_id}/messages.jsonl"

    try:
        raw = await workspace.read_file(jsonl_path)
    except (_NotFoundError, Exception) as exc:
        # Missing file or any read error → treat as empty history.
        if not isinstance(exc, _NotFoundError):
            logger.debug(
                "session %s: read_file(%s) raised %r; treating as empty",
                session_id, jsonl_path, exc,
            )
        return cursor

    last_emitted = cursor
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        seq = record.get("seq")
        if not isinstance(seq, int) or seq <= cursor:
            continue
        try:
            await ws.send_json(record)
            last_emitted = seq
        except WebSocketDisconnect:
            return last_emitted
    return last_emitted


@nested_session_router.websocket(
    "/workspaces/{workspace_id}/sessions/{session_id}/ws",
)
async def session_ws(
    websocket: WebSocket,
    workspace_id: str,
    session_id: str,
    cursor: int = Query(0, ge=0),
) -> None:
    """Bidirectional session stream — cursor replay + live tick subscription.

    Lifecycle:

    1. Resolve the WorkspaceSession row. Reject (4404) if not found or
       if the row belongs to a different workspace.
    2. Reject (4410) if the session is already ENDED.
    3. Accept the WebSocket upgrade.
    4. Replay ``messages.jsonl`` records with ``seq > cursor`` in order.
    5. Subscribe to ``app.state.session_tick_router`` for the session
       (wired in lifespan; guaranteed present).
    6. Run two concurrent loops:
       - ``_session_recv_loop``: reads client frames.  Handles
         ``interrupt`` (sets ``cancel_requested_at`` + publishes cancel
         event), ``tool_approval_decide`` (mirrors chat), ``ping``
         (→ pong).
       - ``_session_send_loop``: on each tick reads new ``messages.jsonl``
         lines since ``last_sent_seq`` and sends them.
    7. On disconnect: closes the tick subscription.
    """
    import time as _time

    # Auth check: middleware populates websocket.state.user from the
    # session cookie. Close with WS-spec code 4401 if missing.
    from primer.api.deps import require_auth_ws
    if require_auth_ws(websocket) is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="auth_required")
        return

    sp = websocket.app.state.storage_provider
    sessions_storage = sp.get_storage(WorkspaceSession)
    event_bus = getattr(websocket.app.state, "event_bus", None)
    session_tick_router = getattr(websocket.app.state, "session_tick_router", None)

    # 1. Resolve session row.
    session = await sessions_storage.get(session_id)
    if session is None or session.workspace_id != workspace_id:
        await websocket.accept()
        await websocket.close(
            code=4404,
            reason=f"session {session_id!r} not found on workspace {workspace_id!r}",
        )
        return

    # 2. Reject ended sessions.
    if session.status == SessionStatus.ENDED:
        await websocket.accept()
        await websocket.close(code=4410, reason="session ended")
        return

    # 3. Accept upgrade.
    await websocket.accept()

    if event_bus is None:
        await websocket.close(code=4500, reason="event_bus_not_available")
        return

    _tracer = _tracing.get_tracer("primer.ws")
    _t0 = _time.monotonic()
    with _tracer.start_as_current_span("ws.session") as _span:
        _metrics.ws_connections_active.labels("session").inc()
        try:
            # 4. Replay history since cursor.
            workspace_registry = getattr(websocket.app.state, "workspace_registry", None)
            workspace = None
            if workspace_registry is not None:
                try:
                    workspace = await workspace_registry.get_workspace(workspace_id)
                except Exception as exc:
                    logger.warning(
                        "session_ws: could not resolve workspace %s: %r",
                        workspace_id, exc,
                    )

            if workspace is not None:
                try:
                    last_seq = await _session_replay_since_cursor(
                        websocket, workspace, session_id, cursor,
                    )
                except WebSocketDisconnect:
                    return
            else:
                last_seq = cursor

            # 5. Subscribe to tick router (guaranteed present — wired in lifespan).
            tick_sub = session_tick_router.subscribe(session_id)

            try:
                recv_task = asyncio.ensure_future(
                    _session_recv_loop(
                        websocket, session_id, sessions_storage, event_bus,
                    )
                )
                send_task = asyncio.ensure_future(
                    _session_send_loop_instrumented(
                        websocket, session_id, workspace, tick_sub, last_seq,
                    )
                )
                try:
                    done, pending = await asyncio.wait(
                        [recv_task, send_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, WebSocketDisconnect, Exception):
                            pass
                    for task in done:
                        exc = task.exception()
                        if exc is not None and not isinstance(exc, WebSocketDisconnect):
                            logger.debug(
                                "session %s WS task raised: %s", session_id, exc,
                            )
                except WebSocketDisconnect:
                    recv_task.cancel()
                    send_task.cancel()
                    for t in (recv_task, send_task):
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
            finally:
                await tick_sub.aclose()
        finally:
            _metrics.ws_connections_active.labels("session").dec()
            _metrics.ws_session_duration_seconds.labels("session").observe(
                _time.monotonic() - _t0
            )
            _span.set_attribute("ws.frames_sent", 0)  # session loop tracks own frames


async def _session_recv_loop(
    websocket: WebSocket,
    session_id: str,
    sessions_storage,
    event_bus,
) -> None:
    """Read client frames and dispatch them.

    - ``ping`` → immediate pong.
    - ``interrupt`` → set ``cancel_requested_at`` + publish cancel event.
    - ``tool_approval_decide`` → publish on the parked event_key.
    """
    while True:
        try:
            incoming = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        kind = incoming.get("kind")
        if kind == "ping":
            await websocket.send_json({"kind": "pong"})
            continue
        if kind == "interrupt":
            session = await sessions_storage.get(session_id)
            if session is None or session.status == SessionStatus.ENDED:
                continue
            session.cancel_requested_at = datetime.now(timezone.utc)
            await sessions_storage.update(session)
            await event_bus.publish(f"session:{session_id}:cancel", {})
            continue
        if kind == "tool_approval_decide":
            tcid = incoming.get("tool_call_id")
            decision = incoming.get("decision")
            reason = incoming.get("reason")
            if decision not in ("approved", "rejected"):
                await websocket.send_json({
                    "kind": "error",
                    "code": "tool_approval_bad_decision",
                    "message": (
                        f"decision must be approved/rejected; got {decision!r}"
                    ),
                })
                continue
            session = await sessions_storage.get(session_id)
            if session is None:
                continue
            blob = session.parked_state or {}
            yielded = blob.get("yielded") or {}
            expected = (yielded.get("resume_metadata") or {}).get(
                "original_call", {},
            ).get("id")
            if expected != tcid:
                await websocket.send_json({
                    "kind": "error",
                    "code": "tool_approval_mismatch",
                    "message": "tool_call_id does not match the pending approval",
                })
                continue
            event_key = yielded.get("event_key")
            if not event_key:
                await websocket.send_json({
                    "kind": "error",
                    "code": "tool_approval_missing_event_key",
                    "message": "park is missing event_key",
                })
                continue
            await event_bus.publish(
                event_key,
                {"decision": decision, "reason": reason},
            )
            continue
        await websocket.send_json({
            "kind": "error",
            "message": f"unknown client message kind: {kind!r}",
        })


async def _session_send_loop(
    websocket: WebSocket,
    session_id: str,
    workspace,
    tick_sub,
    last_sent_seq: int,
) -> None:
    """Forward new session message records on each tick.

    On each tick, reads ``messages.jsonl`` lines whose ``seq`` is between
    ``last_sent_seq + 1`` and ``tick.seq`` (inclusive) and sends them.

    When ``workspace`` is None (workspace not resolvable) the send loop
    simply drains ticks without emitting anything — the connection
    remains open for the recv loop (e.g. interrupt frames) but no
    historical replay or live stream is possible.
    """
    from primer.model.except_ import NotFoundError as _NotFoundError

    async for tick in tick_sub:
        if tick.seq <= last_sent_seq:
            continue
        if workspace is None:
            last_sent_seq = tick.seq
            continue
        state_path = getattr(
            getattr(workspace, "_template", None), "state_path", ".state"
        )
        jsonl_path = (
            f"{state_path}/sessions/{session_id}/messages.jsonl"
        )
        try:
            raw = await workspace.read_file(jsonl_path)
        except (_NotFoundError, Exception):
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = record.get("seq")
            if not isinstance(seq, int):
                continue
            if seq <= last_sent_seq or seq > tick.seq:
                continue
            try:
                await websocket.send_json(record)
            except WebSocketDisconnect:
                return
            last_sent_seq = seq


async def _session_send_loop_instrumented(
    websocket: WebSocket,
    session_id: str,
    workspace,
    tick_sub,
    last_sent_seq: int,
) -> None:
    """Instrumented variant of :func:`_session_send_loop` — increments frame counter."""
    from primer.model.except_ import NotFoundError as _NotFoundError

    async for tick in tick_sub:
        if tick.seq <= last_sent_seq:
            continue
        if workspace is None:
            last_sent_seq = tick.seq
            continue
        state_path = getattr(
            getattr(workspace, "_template", None), "state_path", ".state"
        )
        jsonl_path = (
            f"{state_path}/sessions/{session_id}/messages.jsonl"
        )
        try:
            raw = await workspace.read_file(jsonl_path)
        except (_NotFoundError, Exception):
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = record.get("seq")
            if not isinstance(seq, int):
                continue
            if seq <= last_sent_seq or seq > tick.seq:
                continue
            try:
                await websocket.send_json(record)
                _metrics.ws_frames_sent_total.labels("session").inc()
            except WebSocketDisconnect:
                return
            last_sent_seq = seq


__all__ = [
    "SessionCreateBody",
    "cancel_session",
    "create_session",
    "delete_session",
    "find_sessions",
    "get_session_by_id",
    "list_sessions",
    "nested_session_router",
    "pause_session",
    "resume_session",
    "session_ws",
    "top_session_router",
]
