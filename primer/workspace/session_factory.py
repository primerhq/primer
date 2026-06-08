"""WorkspaceSession factory.

Extracted from ``POST /v1/workspaces/{wid}/sessions``
(:mod:`primer.api.routers.sessions`) so the REST endpoint and the
trigger dispatcher (Phase 4+) share a single canonical session-create
code path. Spec §12.5 (Plan §3.2).

Scope
-----

The factory owns the *persistence* + *auto-start* + *claim/scheduler
registration* steps. It deliberately does NOT do:

* binding validation (agent / graph existence, ``graph_input`` schema
  check) — the REST router has structured 404/422 mapping it must keep.
* on-disk session slot allocation via
  :meth:`Workspace.start_session` — only the router needs that today
  because the trigger dispatcher targets an existing parent session
  (subscriber kind ``existing_session``) or creates a fresh chat row
  rather than a workspace session. When subscription kinds for
  fresh-workspace-sessions land we add slot allocation behind the
  optional ``workspace_registry`` dep.

Callers that need the full router behaviour pre-validate the binding,
then invoke :func:`create_session` with the validated inputs. The
helper handles the rest atomically.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.int.claim import ClaimKind
from primer.model.agent import Agent
from primer.model.except_ import ConflictError, NotFoundError, ValidationError
from primer.model.graph import Graph
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace_session import (
    AgentBinding as OnDiskAgentBinding,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionBinding,
    SessionStatus,
    WorkspaceSession,
)


logger = logging.getLogger(__name__)


@dataclass
class SessionFactoryDeps:
    """Bundle of collaborators :func:`create_session` needs.

    ``workspace_registry`` is optional: when present (today: REST
    router), the factory will allocate the on-disk session slot via the
    backend. When ``None`` (today: trigger dispatcher's existing-session
    target case) the factory just writes the scheduler-visible row.
    """

    storage_provider: Any
    claim_engine: Any
    scheduler: Any
    workspace_registry: Any | None = None


async def start_workspace_session(
    *,
    workspace_id: str,
    binding: SessionBinding,
    initial_instructions: str | None,
    graph_input: Any | None,
    auto_start: bool,
    metadata: dict | None,
    parent_session_id: str | None,
    deps: SessionFactoryDeps,
) -> WorkspaceSession:
    """Full create flow shared by the REST route and the workspaces tool:
    validate workspace + binding (+ graph_input vs Begin.input_schema),
    allocate the on-disk slot via deps.workspace_registry, then persist
    via create_session. Raises NotFoundError (workspace) / ValidationError
    (agent, graph, graph_input).

    Extracted verbatim from ``POST /v1/workspaces/{wid}/sessions``
    (:mod:`primer.api.routers.sessions`) so the REST endpoint and the
    ``create_workspace_session`` MCP tool share one canonical path.
    """
    workspaces = deps.storage_provider.get_storage(WorkspaceRow)
    agents = deps.storage_provider.get_storage(Agent)
    graphs = deps.storage_provider.get_storage(Graph)

    workspace = await workspaces.get(workspace_id)
    if workspace is None:
        raise NotFoundError(f"Workspace {workspace_id!r} does not exist")

    resolved_agent = None
    if isinstance(binding, AgentSessionBinding):
        resolved_agent = await agents.get(binding.agent_id)
        if resolved_agent is None:
            raise ValidationError(
                f"Agent {binding.agent_id!r} does not exist"
            )
    elif isinstance(binding, GraphSessionBinding):
        resolved_graph = await graphs.get(binding.graph_id)
        if resolved_graph is None:
            raise ValidationError(
                f"Graph {binding.graph_id!r} does not exist"
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
            resolved_input: Any | None = graph_input
            if resolved_input is None and initial_instructions:
                # Legacy fallback: parse initial_instructions as JSON so
                # callers that still drive graphs through that field
                # continue to work.
                try:
                    resolved_input = json.loads(initial_instructions)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        "initial_instructions for graph with "
                        "input_schema must be valid JSON (or pass "
                        "graph_input directly)"
                    ) from exc
            if resolved_input is None:
                raise ValidationError(
                    f"graph {binding.graph_id!r} requires graph_input"
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
            graph_input = resolved_input

    # Pre-generate the sid so we can allocate the on-disk slot BEFORE
    # the factory's auto_start path makes the row claimable. Spec §12.5
    # (Plan §3.2): persist + auto-start + claim registration live in
    # create_session so the trigger dispatcher, this helper and the
    # REST handler share one canonical create path.
    sid = f"sess-{uuid.uuid4().hex[:12]}"

    # Allocate the on-disk session slot inside the workspace so the
    # scheduler-visible Session row and the workspace's
    # .state/sessions/<sid>/ directory share the same id (spec §11.4
    # step 5). Both agent and graph bindings get a holder slot;
    # graph bindings use a synthetic agent_id (``graph:<graph_id>``)
    # so the graph executor in primer/worker/pool.py can compose the
    # workspace's tools into every per-node ToolExecutionManager.
    if isinstance(binding, AgentSessionBinding):
        assert resolved_agent is not None  # guarded above
        on_disk_binding = OnDiskAgentBinding(
            agent_id=resolved_agent.id,
            agent_name=resolved_agent.id,
            registered_tool_ids=list(resolved_agent.tools or []),
        )
        live_workspace = await deps.workspace_registry.get_workspace(
            workspace_id,
        )
        await live_workspace.start_session(
            on_disk_binding,
            id=sid,
            instructions=initial_instructions,
            parent_session_id=parent_session_id,
        )
    elif isinstance(binding, GraphSessionBinding):
        # Synthetic AgentBinding for the graph-holder slot. The
        # registered_tool_ids list is informational only; the per-node
        # tool managers register their own toolsets via the worker
        # pool's tool_manager_resolver. The workspace tools (ls/read/
        # write/exec/...) become available to every graph node via the
        # AgentSession the executor consumes.
        on_disk_binding = OnDiskAgentBinding(
            agent_id=f"graph:{binding.graph_id}",
            agent_name=f"graph:{binding.graph_id}",
            registered_tool_ids=[],
        )
        live_workspace = await deps.workspace_registry.get_workspace(
            workspace_id,
        )
        await live_workspace.start_session(
            on_disk_binding,
            id=sid,
            instructions=initial_instructions,
            parent_session_id=parent_session_id,
        )

    # Persist the row + (optionally) auto-start + always register a
    # forward-compat ClaimEngine upsert via the shared service helper.
    # workspace_registry=None because the slot is already allocated above.
    return await create_session(
        workspace_id=workspace_id,
        binding=binding,
        initial_instructions=initial_instructions,
        graph_input=graph_input,
        auto_start=auto_start,
        metadata=metadata,
        parent_session_id=parent_session_id,
        session_id=sid,
        deps=SessionFactoryDeps(
            storage_provider=deps.storage_provider,
            claim_engine=deps.claim_engine,
            scheduler=deps.scheduler,
            workspace_registry=None,
        ),
    )


async def create_session(
    *,
    workspace_id: str,
    binding: SessionBinding,
    initial_instructions: str | None,
    graph_input: Any | None,
    auto_start: bool,
    metadata: dict | None,
    deps: SessionFactoryDeps,
    parent_session_id: str | None = None,
    session_id: str | None = None,
) -> WorkspaceSession:
    """Persist a :class:`WorkspaceSession` row + optionally auto-start.

    Steps mirror :func:`primer.api.routers.sessions.create_session` so
    the two call paths produce identical rows:

    1. Persist the row with ``status=CREATED``.
    2. Fold ``graph_input`` into ``metadata['graph_input']`` for graph
       bindings.
    3. If ``auto_start``: flip to ``RUNNING``, stamp ``started_at``,
       and call ``scheduler.enqueue(sid)`` (best-effort — a broken
       scheduler must not strand the row).
    4. Always upsert with the :class:`ClaimEngine` so the worker pool
       sees the row (forward-compat; no-op when not wired).

    Returns the persisted (and possibly auto-started) session row.

    ``session_id`` lets the caller pre-generate the id so it can run
    its own setup (e.g., on-disk slot allocation) before the row lands
    in storage. When ``None``, a fresh ``sess-<hex>`` id is generated.
    """
    sid = session_id if session_id is not None else f"sess-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)

    md: dict[str, Any] = dict(metadata or {})
    # Graph bindings carry graph_input on metadata so the workspace
    # graph executor can pick it up as the initial input. Mirrors the
    # router's behaviour at primer/api/routers/sessions.py.
    if isinstance(binding, GraphSessionBinding) and graph_input is not None:
        md["graph_input"] = graph_input

    session = WorkspaceSession(
        id=sid,
        workspace_id=workspace_id,
        binding=binding,
        status=SessionStatus.CREATED,
        parent_session_id=parent_session_id,
        initial_instructions=initial_instructions,
        metadata=md,
        created_at=now,
    )
    sessions_storage = deps.storage_provider.get_storage(WorkspaceSession)
    await sessions_storage.create(session)

    if auto_start:
        session.status = SessionStatus.RUNNING
        session.started_at = now
        await sessions_storage.update(session)
        # Best-effort scheduler enqueue: the row is the source of truth.
        # A scheduler outage must not roll back the session.
        try:
            await deps.scheduler.enqueue(sid)
        except Exception as exc:  # noqa: BLE001 — defensive perimeter
            logger.warning(
                "session_factory: scheduler.enqueue(%r) raised: %s",
                sid, exc,
            )

    # Forward-compat ClaimEngine upsert — matches what the REST router
    # has always done. No-op when ``claim_engine is None``.
    if deps.claim_engine is not None:
        try:
            await deps.claim_engine.upsert(ClaimKind.SESSION, sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_factory: claim_engine.upsert(%r) raised: %s",
                sid, exc,
            )

    return session


@dataclass
class SessionCancelDeps:
    """Bundle of collaborators :func:`cancel_session` needs.

    ``event_bus`` is optional: when present, the ``running`` cancel path
    publishes ``session:{id}:cancel`` so the engine-path worker's
    ``_cancel_watcher`` preempts the in-flight turn. When ``None`` the
    publish is skipped (the legacy ``scheduler.signal_cancel`` still
    fires).
    """

    storage_provider: Any
    scheduler: Any
    claim_engine: Any
    event_bus: Any | None = None


async def cancel_session(
    *, workspace_id: str, session_id: str, deps: SessionCancelDeps
) -> WorkspaceSession:
    """Hard-cancel a session (shared by the REST route + the workspaces tool).

    created/waiting/paused -> ended/cancelled inline + drop the claim lease;
    running -> set cancel_requested + publish session:{id}:cancel + signal_cancel.
    Raises NotFoundError (missing/mismatched) / ConflictError (already ended).

    Extracted verbatim from ``POST .../sessions/{sid}/cancel``
    (:mod:`primer.api.routers.sessions`) so the REST endpoint and the
    ``cancel_workspace_session`` MCP tool share one canonical path.
    """
    sessions = deps.storage_provider.get_storage(WorkspaceSession)

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
        # Drop the lease -- session is gone, no point claiming it.
        if deps.claim_engine is not None:
            await deps.claim_engine.delete_lease(ClaimKind.SESSION, session_id)
        return s
    now = datetime.now(timezone.utc)
    s.cancel_requested = True
    s.cancel_requested_at = now
    await sessions.update(s)
    # Publish on the bus so the engine-path worker's _cancel_watcher
    # preempts the running turn. The WS interrupt handler publishes the
    # same key.
    if deps.event_bus is not None:
        try:
            await deps.event_bus.publish(f"session:{session_id}:cancel", {})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cancel_session: event_bus.publish failed "
                "(legacy path still signalled)",
                extra={
                    "session_id": session_id,
                    "exception": type(exc).__name__,
                },
            )
    await deps.scheduler.signal_cancel(session_id)
    return s


__all__ = [
    "SessionCancelDeps",
    "SessionFactoryDeps",
    "cancel_session",
    "create_session",
    "start_workspace_session",
]
