"""Session REST surface - nested create + cancel + top-level routes."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field, model_validator

from primer.model.external_tool import (
    ExternalToolDef,
    validate_external_tool_defs,
)

from primer.api.deps import (
    get_claim_engine,
    get_event_bus,
    get_external_tool_call_storage,
    get_scheduler,
    get_session_storage,
    get_storage_provider,
    get_workspace_registry,
    get_workspace_storage,
)
from primer.api.errors import common_responses
from primer.api.pagination import FindRequest, parse_order_by, parse_page
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.timeline import build_turn_timeline
from primer.model.except_ import (
    ConflictError,
    NotFoundError,
)
from primer.session.default_binding import resolve_initial_binding
from primer.model.workspace_session import (
    PendingSessionMessage,
    WorkspaceSession,
    SessionBinding,
    SessionStatus,
)
from primer.model.storage import (
    FieldRef,
    OffsetPage,
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

    binding: SessionBinding | None = Field(
        default=None,
        description=(
            "Agent or graph this session runs. Omit it to use the system "
            "default agent, which is what lets a caller open a session "
            "without choosing. With no default configured, omitting it "
            "is an error rather than a guess."
        ),
    )
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
    autonomous: bool | None = Field(
        default=None,
        description=(
            "Interactive-vs-autonomous control signal. None => derive from "
            "binding kind (graph autonomous, agent interactive). True marks "
            "an agent self-driving loop autonomous."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    external_tools: list[ExternalToolDef] | None = Field(
        default=None,
        description=(
            "Invoker-supplied tool defs for the initial turn (when "
            "auto_start / initial_instructions trigger one). Gated by the "
            "agent's allow_external_tools; validated for name/caps here."
        ),
    )
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

    @model_validator(mode="after")
    def _check_external_tools(self) -> "SessionCreateBody":
        if self.external_tools:
            validate_external_tool_defs(self.external_tools)
        return self


@nested_session_router.post(
    "/workspaces/{workspace_id}/sessions",
    response_model=WorkspaceSession,
    status_code=201,
    summary="Create a new session attached to an agent or graph",
    responses=common_responses(404, 409, 422, 500),
)
async def create_session(
    body: SessionCreateBody,
    request: Request,
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
       slot whose synthetic agent_id is ``graph:<graph_id>`` - the
       graph executor (primer/worker/pool.py) looks the holder up via
       :meth:`Workspace.get_session` and composes the workspace's
       tools into every per-node ``ToolExecutionManager``. Without the
       holder, graph-bound sessions cannot access workspace tools.
    5. If ``auto_start``: bump status to ``RUNNING``, stamp
       ``started_at``, and enqueue with the scheduler.
    """
    from primer.model.principal import PrincipalRef
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
    # Attribution (spec §8.1): project the request's resolved actor (Layer 1
    # AuthMiddleware, ``request.state.actor``) into the persisted
    # PrincipalRef. ``actor`` is only absent when auth is enabled and the
    # request carries no valid session/token -- require_auth already 401s
    # that case before this handler runs, so the fallback below is a
    # defensive belt-and-braces rather than a reachable production path.
    actor = getattr(request.state, "actor", None)
    initiated_by = (
        PrincipalRef.from_principal(actor)
        if actor is not None
        else PrincipalRef.system()
    )
    # Explicit wins; the default is a fallback, never an override.
    binding = await resolve_initial_binding(
        requested=body.binding, storage_provider=storage_provider,
    )
    return await start_workspace_session(
        workspace_id=workspace_id,
        binding=binding,
        initial_instructions=body.initial_instructions,
        graph_input=body.graph_input,
        auto_start=body.auto_start,
        metadata=body.metadata,
        parent_session_id=body.parent_session_id,
        autonomous=body.autonomous,
        name=body.name,
        initiated_by=initiated_by,
        deps=deps,
        external_tools=body.external_tools,
    )


# ===========================================================================
# Task 20 - resume / pause / cancel + top-level list / find / get
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
    # no lease - a stuck session no worker can claim (T0432). See
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
    * For RUNNING sessions we set ``pause_requested=True`` and return -
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
    summary="Hard cancel - transitions to ENDED/cancelled",
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
      ``session:{sid}:cancel`` event bus key - the engine-path worker's
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
    result = await _cancel_session_helper(
        workspace_id=workspace_id, session_id=session_id, deps=deps,
    )
    # Hard cancel abandons any invoker-supplied tool calls still pending;
    # resolve their audit rows so orchestrators polling the global list
    # see the terminal state.
    from primer.model.external_tool import ExternalToolCall
    from primer.session.external_tools import cancel_pending_external

    await cancel_pending_external(
        call_storage=storage_provider.get_storage(ExternalToolCall),
        session_id=session_id,
        reason="session cancelled",
    )
    return result


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
            "Force-delete a RUNNING session - bypass the 409 gate that "
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
    call_storage=Depends(get_external_tool_call_storage),
    storage_provider=Depends(get_storage_provider),
) -> None:
    """Permanently remove a session row + best-effort cleanup of its
    on-disk slot under ``<workspace>/.state/sessions/<sid>/``.

    For CREATED/WAITING/PAUSED rows we transition to ENDED inline (no
    worker is holding the lease, so the cleanup is safe to do in this
    request). ENDED / FAILED / CANCELLED rows are removed as-is.
    RUNNING rows return 409 - a worker holds the lease and would
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
    # Resolve any still-pending invoker-supplied tool calls before the
    # row goes away (the audit rows outlive the session).
    from primer.session.external_tools import cancel_pending_external

    await cancel_pending_external(
        call_storage=call_storage,
        session_id=session_id,
        reason="session deleted",
    )
    if s.status == SessionStatus.RUNNING and not force:
        raise ConflictError(
            f"Session {session_id!r} is running; cancel it first "
            "(POST /cancel) before deleting, or pass ?force=true to "
            "evict an orphaned row"
        )
    if s.status == SessionStatus.RUNNING and force:
        # Publish cancel so any worker actually holding the lease
        # preempts cleanly before its complete_turn CAS. Best-effort -
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
    # scheduler - symmetric with cancel_session's CREATED/WAITING/PAUSED
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

    # Best-effort: drop the in-memory session handle AND reap the persisted
    # on-disk slot so list_sessions() stops returning it. Each backend reaps
    # its OWN slot inside remove_session (LocalWorkspace rmtrees
    # .state/sessions/<sid>/; SandboxWorkspace git-rm's the slot in the pod's
    # runtime state repo). The previous local-only host rmtree here silently
    # skipped the sandbox backend -- which keeps its slot via _state_repo,
    # not _state -- so the pod slot survived and rehydrated the "deleted"
    # session on the next list.
    try:
        live_workspace = await workspace_registry.get_workspace(workspace_id)
        await live_workspace.remove_session(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delete_session: session-slot cleanup failed (row still removed)",
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

    # Drop the thread mappings that pointed here. Best-effort: the row must
    # still be deleted if the correlation table is unreachable, but a leaked
    # mapping would steer a session that no longer exists (S6 section 9).
    try:
        from primer.channel.correlation import CorrelationStore

        await CorrelationStore(storage_provider).clear_for_session(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delete_session: correlation cleanup failed (row still removed)",
            extra={"session_id": session_id, "error": str(exc)},
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


class RecentSession(WorkspaceSession):
    """Response-only shape: one live session plus resolved display
    context for a cross-workspace "recent sessions" feed (the palette
    and rail).  Never persisted -- ``workspace_name``/``last_activity_at``
    are computed fresh on every call, a read-time join, not a
    denormalization (01a06431 c-1)."""

    workspace_name: str | None = Field(
        default=None,
        description=(
            "Resolved display name of the owning workspace. Falls back "
            "to the workspace id in the console, same convention as "
            "Workspace.name itself."
        ),
    )
    last_activity_at: datetime = Field(
        ...,
        description=(
            "last_turn_at when set, else created_at -- resolved "
            "server-side so callers (palette, rail) don't each "
            "re-derive the same fallback."
        ),
    )


_RECENT_SCAN_WINDOW = 200
_RECENT_DEFAULT_LIMIT = 20
_RECENT_MAX_LIMIT = 100


@top_session_router.get(
    "/sessions/recent",
    summary="Recently-active sessions across every live workspace",
    responses=common_responses(400, 422, 500),
)
async def recent_sessions(
    limit: int = Query(default=_RECENT_DEFAULT_LIMIT, ge=1, le=_RECENT_MAX_LIMIT),
    sessions=Depends(get_session_storage),
    workspaces=Depends(get_workspace_storage),
) -> list[RecentSession]:
    """Cross-workspace "recent sessions", scoped to live workspaces only.

    ``GET /sessions/find`` (unscoped) is a generic, complete finder --
    deliberately including sessions whose workspace was later destroyed
    (``ended_reason="workspace_lost"``/``"failed"``), a tombstone an
    audit caller may want. This route exists because the palette/rail
    "recent sessions" feed is NOT that caller: surfacing tombstoned
    sessions with no workspace/agent context made rows indistinguishable
    ("triplicate 'main' rows", uiv2 Phase-2 finding). Excludes any
    session whose workspace_id no longer names a live workspace, resolves
    workspace_name via a read-time join (no denormalization), and orders
    by last_activity_at desc. The bound agent/graph qualifier is already
    on ``binding`` (WorkspaceSession's own field) -- Agent/Graph ids ARE
    their display names in this codebase (Describeable has no separate
    name field), so no extra per-row lookup is needed for that part.

    Scans the ``_RECENT_SCAN_WINDOW`` most-recently-created sessions
    (not the whole table) as the candidate pool, then filters/sorts/caps
    to ``limit`` in memory -- storage's generic Storage[T] has no
    order-by-expression support, so this mirrors the shape the UI's own
    client-side normalisation already did (SH_api.allSessions), just
    moved server-side where the workspace join can happen too.
    """
    live_workspaces: dict[str, str] = {}
    offset = 0
    while True:
        page = await workspaces.list(OffsetPage(offset=offset, length=200))
        for ws in page.items:
            live_workspaces[ws.id] = ws.name or ws.id
        if len(page.items) < 200:
            break
        offset += 200

    candidates = await sessions.list(
        OffsetPage(offset=0, length=_RECENT_SCAN_WINDOW),
        order_by=[OrderBy(field="created_at", direction="desc")],
    )

    rows: list[RecentSession] = []
    for session in candidates.items:
        workspace_name = live_workspaces.get(session.workspace_id)
        if workspace_name is None:
            continue
        rows.append(RecentSession(
            **session.model_dump(),
            workspace_name=workspace_name,
            last_activity_at=session.last_turn_at or session.created_at,
        ))

    rows.sort(key=lambda r: r.last_activity_at, reverse=True)
    return rows[:limit]


class SessionDetail(WorkspaceSession):
    """A session row plus the follow-ups it has not run yet.

    Subclasses rather than wraps, so every existing field stays a
    literal sibling and no client reading WorkspaceSession today has to
    change. Only ever constructed for a response, never stored.
    """

    pending_messages: list[PendingSessionMessage] = Field(
        default_factory=list,
        description=(
            "Steers that arrived while a turn was running and have not "
            "been realized yet, oldest first. Each carries parts rather "
            "than a flattened string, matching what the drain joins back "
            "out when it realizes one."
        ),
    )
    usage: dict[str, int] | None = Field(
        default=None,
        description=(
            "Token totals folded from the visible DONE records (see "
            "primer.session.usage.session_usage) - turns, "
            "total_input_tokens, total_output_tokens, etc. Null when the "
            "log could not be read."
        ),
    )
    context_length: int | None = Field(
        default=None,
        description=(
            "Context window size of the currently bound model, resolved "
            "the same way compaction budgets it. Null for a graph-bound "
            "session or one whose agent/model can no longer be resolved."
        ),
    )


# A pathological queue must not make a detail read unbounded.
_PENDING_PAGE = 100


async def _session_usage_totals(
    session: WorkspaceSession, workspace_registry,
) -> dict[str, int] | None:
    """Best-effort token totals for ``session``, or ``None`` if the log
    is unreadable - mirrors build_usage_frame's own shape (tap.py), the
    only other place session_usage() is assembled into a response."""
    from primer.api.routers.tap import build_usage_frame

    workspace = await workspace_registry.get_workspace(session.workspace_id)
    if workspace is None:
        return None
    state_path = getattr(workspace, "state_path", ".state")
    rel = f"{state_path}/sessions/{session.id}/messages.jsonl"
    raw = await workspace.read_file(rel)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return build_usage_frame(text.splitlines())


# Dogfood round 2: providers.py's discovery probes used to seed exactly
# this value onto every model whose endpoint didn't report a real
# window (removed there now - see _probe_openai_compatible_models /
# _probe_ollama_models). A profile already carrying it from before that
# fix is indistinguishable from "an operator genuinely typed 32000", so
# this path treats it as "never learned" rather than serving it as fact
# - it is the one number that shipped a real user a confident-looking
# wrong meter denominator ("98k / 32k" against an actual 131k model).
_LEGACY_SEEDED_CONTEXT_LENGTH = 32000


async def _session_context_length(
    session: WorkspaceSession, storage_provider,
) -> int | None:
    """Best-effort context window size for session's bound model, or
    None when nothing resolvable/trustworthy is available (graph-bound,
    unbound, a deleted agent/profile, or the legacy-seeded fake above) -
    the caller treats None as "unknown", not an error.

    Deliberately does NOT call primer.agent.compaction.lookup_context_length:
    that helper's fallback chain (configured value wins, else the curated
    table) stays exactly as-is for compaction's own budget math, where a
    stale/fake number is low-stakes (a slightly early or late summarise).
    Serving a number to the user's face is higher-stakes, so this applies
    a stricter, different precedence: a curated known-model value always
    wins (it is real by construction), then the stored value UNLESS it is
    the exact legacy seed.
    """
    from primer.agent.compaction import MODEL_CONTEXT_FALLBACK
    from primer.model.agent import Agent
    from primer.model_profile import resolve_model

    binding = session.binding
    if getattr(binding, "kind", None) != "agent":
        return None
    agent = getattr(binding, "agent_snapshot", None)
    if agent is None:
        agent_id = getattr(binding, "agent_id", None)
        if not agent_id:
            return None
        agent = await storage_provider.get_storage(Agent).get(agent_id)
    if agent is None:
        return None
    llm_model = await resolve_model(
        storage_provider,
        default_profile_id=agent.model.profile_id,
        override_profile_id=getattr(binding, "profile_id", None),
    )
    if llm_model.model_name in MODEL_CONTEXT_FALLBACK:
        return MODEL_CONTEXT_FALLBACK[llm_model.model_name]
    if llm_model.context_length == _LEGACY_SEEDED_CONTEXT_LENGTH:
        return None
    return llm_model.context_length


@top_session_router.get(
    "/sessions/{session_id}",
    response_model=SessionDetail,
    summary="Get session by id (no workspace context required)",
    responses=common_responses(404, 500),
)
async def get_session_by_id(
    session_id: str = Path(...),
    sessions=Depends(get_session_storage),
    storage_provider=Depends(get_storage_provider),
    workspace_registry=Depends(get_workspace_registry),
) -> SessionDetail:
    s = await sessions.get(session_id)
    if s is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")

    pending: list[PendingSessionMessage] = []
    try:
        page = await storage_provider.get_storage(PendingSessionMessage).find(
            Predicate(
                left=FieldRef(name="session_id"), op=Op.EQ,
                right=Value(value=session_id),
            ),
            OffsetPage(offset=0, length=_PENDING_PAGE),
            order_by=[
                OrderBy(field="enqueued_at", direction="asc"),
                OrderBy(field="id", direction="asc"),
            ],
        )
        pending = list(page.items)
    except Exception:  # noqa: BLE001 - the row is the answer; the queue is extra
        logger.exception(
            "session detail: reading pending messages failed for %s",
            session_id,
        )

    usage: dict[str, int] | None = None
    try:
        usage = await _session_usage_totals(s, workspace_registry)
    except Exception:  # noqa: BLE001 - the row is the answer; usage is extra
        logger.exception(
            "session detail: computing usage failed for %s", session_id,
        )

    context_length: int | None = None
    try:
        context_length = await _session_context_length(s, storage_provider)
    except Exception:  # noqa: BLE001 - same as usage above
        logger.exception(
            "session detail: resolving context_length failed for %s",
            session_id,
        )

    return SessionDetail(
        **s.model_dump(), pending_messages=pending,
        usage=usage, context_length=context_length,
    )


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


def _extract_legacy_text(obj: dict) -> str | None:
    """Join every text part of a legacy ``{role,parts}`` Message dict.

    Mirrors WorkspaceAgentExecutor._fetch_last_assistant_text's own
    part-walk (primer/agent/workspace_executor.py), just over a raw
    dict instead of a validated Message. Returns None when the line
    carries no text part (nothing to reconcile).
    """
    texts: list[str] = []
    for part in obj.get("parts") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            t = part.get("text")
            if t:
                texts.append(t)
    return "".join(texts) if texts else None


def _dedupe_legacy_user_input(
    items: list[dict], *, fallback_created_at: str | None,
) -> list[dict]:
    """Reconcile messages.jsonl's dual-write asymmetry (01a04dde-b331).

    messages.jsonl deliberately interleaves two shapes for two different
    consumers: legacy ``{role,parts}`` Message lines feed LLM context
    reconstruction (WorkspaceAgentExecutor._read_messages_jsonl), and
    modern seq/kind SessionMessageRecord lines feed this API + the live
    tap. primer.session.enqueue.wake_session writes BOTH for every
    steer; primer.workspace.session_factory.start_workspace_session now
    does the same for a session's opening initial_instructions (the
    01a04dde-b331 write-side fix) - but a session created BEFORE that
    fix shipped has its opening instruction ONLY as a legacy line, and
    nothing ever back-fills the missing modern counterpart for
    already-persisted history. Dropping it would erase a real user
    message from the transcript this route serves.

    Scoped to role="user" legacy lines ONLY - every other legacy shape
    (assistant text/tool_call, tool tool_result) is passed through
    completely unchanged. A NORMAL (non-parked) turn already writes a
    matching modern record for those during live streaming (confirmed:
    ToolCallEnd's TOOL_CALL record lands before dispatch, the
    ExtendedEvent(_ExecutorToolResult) that becomes TOOL_RESULT lands
    right after), but a PARKED-then-resumed turn's rehydrated
    tool-result does not - WorkspaceAgentExecutor.inject_resume_messages
    -> _persist_turn writes only the legacy line for that content, and
    nothing (not park time, not resume time) ever writes a matching
    modern TOOL_RESULT record for it. That is a separate, not-yet-fixed
    write-side gap (reported alongside this one); reconciling it here
    without a real modern record to key off risks synthesizing the
    wrong shape or, worse, silently deciding a line is "covered" when
    it isn't and dropping real content the raw passthrough never lost.
    Dev-Prime's UI-side legacy-line tolerance stays load-bearing for
    that case.

    For each role="user" legacy line: if a modern USER_INPUT record
    ANYWHERE in this file already carries the exact same text, drop the
    legacy line (redundant - the write-side fix means this is now the
    common case for every NEW instruction). Otherwise synthesize a
    USER_INPUT-shaped item so the message is never lost. Synthesized
    items count DOWN from seq 0 (0, -1, -2, ...) in file order: never
    collides with a real record (those start at seq 1), sorts before
    all real content (correct - this is always older, backfilled
    history), and is naturally excluded from any since_seq-filtered
    poll (any since_seq >= 0 already excludes seq <= 0) - exactly the
    "not new" status this content actually has.

    Known simplification: matching is by exact text equality anywhere
    in the file, not file-position proximity to a specific candidate
    counterpart. Two genuinely distinct user messages that happen to
    share identical text could dedupe against each other's counterpart
    instead of their own - the failure mode is a duplicate line
    rendering once instead of twice, never data loss of distinct
    content, so the simpler global check was chosen over a
    proximity-windowed one.
    """
    covered_texts = {
        obj["payload"]["text"]
        for obj in items
        if obj.get("kind") == "user_input"
        and isinstance(obj.get("payload"), dict)
        and isinstance(obj["payload"].get("text"), str)
    }
    out: list[dict] = []
    synthetic_seq = 0
    for obj in items:
        if "kind" in obj:
            out.append(obj)
            continue
        if obj.get("role") != "user":
            out.append(obj)  # non-user legacy line: pass through unchanged
            continue
        text = _extract_legacy_text(obj)
        if text is None:
            out.append(obj)  # no text part to reconcile; leave as-is
            continue
        if text in covered_texts:
            continue  # redundant - a modern USER_INPUT already covers it
        out.append({
            "seq": synthetic_seq,
            "kind": "user_input",
            "payload": {"text": text},
            "created_at": fallback_created_at,
            "node_id": None,
        })
        synthetic_seq -= 1
    return out


async def _read_workspace_turn_log(
    *,
    workspace,
    relative_path: str,
    limit: int,
    offset: int,
    since_seq: int | None,
    tail: bool = False,
    visible: bool = False,
    dedupe_legacy_user_input: bool = False,
    fallback_created_at: str | None = None,
) -> dict:
    """JSONL-parse the file at ``relative_path`` inside ``workspace``.

    Missing file is treated as an empty log (a fresh session that's
    written nothing yet). Bogus lines are skipped silently - the turn
    log is observability data, not a contract.

    ``tail`` flips the window to the *end* of the log: the console loads a
    session transcript newest-page-first (most-recent ``limit`` rows) and pages
    older rows on demand, instead of pulling the whole file at once (#3/#7).
    With ``tail`` the ``offset`` counts rows from the tail - ``offset=0`` is the
    most-recent ``limit`` rows, ``offset=limit`` the next-older page - so
    paging is anchored to the end of the log, not a shifting start. Rows are
    always returned in ascending ``seq`` order.

    ``visible`` folds the log through the replay walk first, so the
    caller sees what the conversation currently shows: rewound rows
    disappear and a compacted span collapses to its marker. It defaults
    off because the audit and trace views need the raw stream, and a
    rewound span has to stay fetchable to render as a collapsed region.

    ``dedupe_legacy_user_input`` (01a04dde-b331): reconciles the
    messages.jsonl dual-write asymmetry (see :func:`_dedupe_legacy_user_input`)
    before paging. Defaults off - this helper is SHARED with
    turns.jsonl (get_session_turn_log) and other JSONL logs
    (primer/api/routers/compute.py) that carry no such dual-shape
    concept at all; only get_session_messages (the one reader of
    messages.jsonl specifically) opts in. Folded BEFORE paging, same
    reasoning as ``visible`` - offsets must describe the reconciled
    conversation, not the raw file underneath it.
    """
    try:
        raw = await workspace.read_file(relative_path)
    except Exception:  # noqa: BLE001 - NotFoundError / IO / decode
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
    if dedupe_legacy_user_input:
        # Before the visible fold: visible_records/_parse (primer/
        # session/replay.py) only keeps kind+seq shaped lines, so
        # reconciling legacy lines into that shape FIRST means a
        # visible=true request also correctly sees a synthesized
        # backfilled instruction instead of silently losing it the
        # same way the raw legacy line would have.
        items = _dedupe_legacy_user_input(
            items, fallback_created_at=fallback_created_at,
        )
    if visible:
        # Folded BEFORE paging, so offsets describe the conversation the
        # caller asked to see rather than the raw file underneath it.
        from primer.session.replay import visible_records

        visible_seqs = {
            rec.get("seq")
            for rec in visible_records([json.dumps(obj) for obj in items])
        }
        items = [obj for obj in items if obj.get("seq") in visible_seqs]
    total = len(items)
    if tail:
        end = max(0, total - offset)
        start = max(0, end - limit)
        window = items[start:end]
    else:
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
    visible: bool = Query(
        False,
        description=(
            "Fold the log through the replay walk before paging: rewound "
            "rows disappear and a compacted span collapses to its marker. "
            "Off by default, because the audit and trace views need the "
            "raw stream and a rewound span must stay fetchable to render "
            "as a collapsed region."
        ),
    ),
    tail: bool = Query(
        default=False,
        description=(
            "Return the most-recent `limit` rows (newest page) instead of the "
            "oldest. With `tail`, `offset` counts rows from the end, so the "
            "console can load a long transcript's tail immediately and page "
            "older rows lazily rather than fetching the whole log at once."
        ),
    ),
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
        tail=tail,
        visible=visible,
        dedupe_legacy_user_input=True,
        fallback_created_at=(
            sess.created_at.isoformat() if sess.created_at else None
        ),
    )


@top_session_router.get(
    "/sessions/{session_id}/turns/{turn_no}/timeline",
    summary="Derive one turn's execution timeline",
    responses=common_responses(404, 500),
)
async def get_session_turn_timeline(
    session_id: str = Path(..., description="Session id"),
    turn_no: int = Path(..., ge=0, description="Turn index (0-based)"),
    sessions=Depends(get_session_storage),
    workspace_registry=Depends(get_workspace_registry),
) -> dict:
    """Fold this turn's records into a tree: model calls, tool round-trips,
    graph nodes, delegated subagent calls, and any wait segment.

    Pure derivation (12-s7-design.md section 6): no trace system, no new
    write path. ``turn_no`` is the window ordinal produced by terminal
    counting over every record in messages.jsonl, and it selects the
    turn-log envelope run at the same ordinal. Counting is deliberately
    done on the UNFOLDED log so a compaction or a rewind cannot retarget
    a turn_no already in circulation; the response echoes the window's
    ``terminal_seq`` for callers that want to re-resolve it. Works on any
    historical session.
    """
    sess = await sessions.get(session_id)
    if sess is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    workspace = await workspace_registry.get_workspace(sess.workspace_id)
    if workspace is None:
        raise NotFoundError(
            f"Workspace {sess.workspace_id!r} for session {session_id!r} "
            "is unavailable"
        )
    state_path = getattr(workspace, "state_path", ".state")

    async def _lines(name: str) -> list[str]:
        try:
            raw = await workspace.read_file(
                f"{state_path}/sessions/{session_id}/{name}"
            )
        except Exception:  # noqa: BLE001 - NotFoundError / IO / decode
            return []
        return raw.decode("utf-8", errors="replace").splitlines()

    timeline = build_turn_timeline(
        message_lines=await _lines("messages.jsonl"),
        turn_log_lines=await _lines("turns.jsonl"),
        turn_no=turn_no,
    )
    if timeline is None:
        raise NotFoundError(
            f"Session {session_id!r} has no turn {turn_no}"
        )
    return {"session_id": session_id, **timeline}


__all__ = [
    "SessionCreateBody",
    "cancel_session",
    "create_session",
    "delete_session",
    "find_sessions",
    "get_session_by_id",
    "get_session_turn_timeline",
    "list_sessions",
    "nested_session_router",
    "pause_session",
    "resume_session",
    "top_session_router",
]
