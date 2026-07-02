"""Session REST surface — nested create + cancel + top-level routes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_scheduler,
    get_session_storage,
    get_storage_provider,
    get_workspace_registry,
)
from primer.api.errors import common_responses
from primer.api.pagination import FindRequest, parse_order_by, parse_page
from primer.session.mutation_lock import session_lifecycle_lock
from primer.model.except_ import (
    ConflictError,
    NotFoundError,
)
from primer.model.workspace_session import (
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
    name: str | None = Field(
        default=None,
        description=(
            "Optional user-supplied friendly name for the session. Persisted "
            "onto both the scheduler row and the on-disk SessionInfo "
            "(session.json) so the console shows it instead of the opaque "
            "session id. Null / empty defaults to the id."
        ),
    )
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
    from primer.workspace.session_factory import (
        SessionFactoryDeps,
        start_workspace_session,
    )

    deps = SessionFactoryDeps(
        storage_provider=storage_provider,
        claim_engine=engine,
        scheduler=scheduler,
        workspace_registry=workspace_registry,
    )
    return await start_workspace_session(
        workspace_id=workspace_id,
        binding=body.binding,
        initial_instructions=body.initial_instructions,
        graph_input=body.graph_input,
        auto_start=body.auto_start,
        metadata=body.metadata,
        parent_session_id=body.parent_session_id,
        name=body.name,
        deps=deps,
    )


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
    # Serialize against a concurrent cancel/pause on the same session: the
    # status read-modify-write and the lease upsert must not interleave with
    # a cancel's ENDED write + delete_lease, or the row can land RUNNING with
    # no lease — a stuck session no worker can claim (T0432). See
    # primer.session.mutation_lock.
    async with session_lifecycle_lock().acquire(session_id):
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
    # Serialize against a concurrent resume/cancel on the same session so the
    # PAUSED write is not clobbered (and does not clobber) a racing
    # transition. See primer.session.mutation_lock / T0432.
    async with session_lifecycle_lock().acquire(session_id):
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
    scheduler=Depends(get_scheduler),
    engine=Depends(get_claim_engine),
    event_bus=Depends(get_event_bus),
    storage_provider=Depends(get_storage_provider),
    workspace_registry=Depends(get_workspace_registry),
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

    Delegates to :func:`primer.workspace.session_factory.cancel_session`
    so the REST route and the ``cancel_workspace_session`` MCP tool share
    one canonical path.
    """
    from primer.workspace.session_factory import (
        SessionCancelDeps,
        cancel_session as _cancel_session_helper,
    )

    deps = SessionCancelDeps(
        storage_provider=storage_provider,
        scheduler=scheduler,
        claim_engine=engine,
        event_bus=event_bus,
        workspace_registry=workspace_registry,
    )
    return await _cancel_session_helper(
        workspace_id=workspace_id, session_id=session_id, deps=deps,
    )


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
                # NB: not "message" -- that is a reserved LogRecord attribute
                # and makeRecord() would raise KeyError, turning this
                # best-effort log into a 500 that skips the row delete below.
                "error": str(exc),
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
    graph_id: Annotated[
        str | None,
        Query(
            description=(
                "Filter by binding.graph_id. Only matches sessions whose "
                "binding kind is 'graph'; agent-bound sessions never "
                "satisfy this filter. Translated by the storage layer to "
                "a nested-JSON path lookup; backends that cannot express "
                "such paths reject the request with 400."
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
    if graph_id is not None:
        # Nested JSONB path; the Postgres backend translates this to
        # ``data->'binding'->>'graph_id'``. Backends that cannot express
        # nested paths will reject the predicate with 400.
        filters.append(
            Predicate(
                left=FieldRef(name="binding.graph_id"),
                op=Op.EQ,
                right=Value(value=graph_id),
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
    state_path = getattr(workspace, "state_path", ".state")
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


@top_session_router.get(
    "/sessions/{session_id}/messages",
    summary="Read the session's recorded message log (paginated)",
    responses=common_responses(404, 500),
)
async def get_session_messages(
    session_id: str = Path(..., description="Session id"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    after_seq: int | None = Query(default=None, ge=0),
    sessions=Depends(get_session_storage),
    workspace_registry=Depends(get_workspace_registry),
) -> dict:
    """Return the recorded ``messages.jsonl`` rows for this session.

    Unlike the WebSocket (which rejects ENDED sessions), this serves the
    full recorded history for any status, so the console can render the
    output of a finished run. Reuses the generic JSONL reader; a missing
    file or absent workspace yields an empty log rather than a 5xx.
    """
    sess = await sessions.get(session_id)
    if sess is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    workspace = await workspace_registry.get_workspace(sess.workspace_id)
    if workspace is None:
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    state_path = getattr(workspace, "state_path", ".state")
    rel = f"{state_path}/sessions/{session_id}/messages.jsonl"
    return await _read_workspace_turn_log(
        workspace=workspace,
        relative_path=rel,
        limit=limit,
        offset=offset,
        since_seq=after_seq,
    )


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
    "top_session_router",
]
