"""Phase-2 compute entity routers: Agent + Graph.

Each entity follows the standard CRUD + Find shape from
:mod:`primer.api.routers._crud`, plus an entity-specific status check
that validates the row's external references resolve.

Streaming ``call`` endpoints (SSE) and Threads sub-resources are
deferred to a follow-up sub-project; the design spec lists them as
Phase-2 work that the FastAPI ``StreamingResponse`` infrastructure
already supports out of the box.

* Agent.status — checks that the referenced LLMProvider exists and
  that all referenced ``tools`` ids resolve to a Toolset row.
* Graph.status — checks that every agent-node references an Agent
  that exists, every subgraph-node references a Graph that exists.
  Topology validity itself is enforced by the Pydantic validator on
  :class:`Graph` at read time, so it never reaches storage broken.
"""

from __future__ import annotations

import re
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel

from primer.agent.agent_checks import check_agent_on_create, check_agent_on_update, missing_toolset_ids

from primer.api.deps import (
    get_agent_storage,
    get_graph_storage,
    get_llm_provider_storage,
    get_model_profile_storage,
    get_session_storage,
    get_storage_provider,
    get_toolset_storage,
    get_workspace_registry,
)
from primer.api.errors import common_responses
from primer.api.routers._crud import make_crud_router
from primer.common.context_overflow import output_cap_warning
from primer.common.entity_checks import EntityCheckError
from primer.model.agent import Agent
from primer.model.except_ import NotFoundError, PrimerError
from primer.model.graph import Graph
from primer.model.problem_details import record_without_traceback
from primer.model.workspace_session import GraphSessionBinding, WorkspaceSession


# ---- Agent router ----------------------------------------------------------


def _agent_check_as_rest_error(exc: EntityCheckError) -> HTTPException:
    """The 422 the profile and channel routers raise for a refused reference: ``{error, field, message}``."""
    return HTTPException(
        status_code=422, detail={"error": exc.code or "", "field": exc.field or "", "message": exc.message},
    )


# The checks are shared with the system tools and the builder's ``crud`` toolset (primer/agent/agent_checks.py): these adapters call
# the shared check and re-raise it in the router's shape.


async def _agent_pre_create(entity: Agent, request: Request) -> None:
    try:
        await check_agent_on_create(entity, storage_provider=request.app.state.storage_provider)
    except EntityCheckError as exc:
        raise _agent_check_as_rest_error(exc) from exc


async def _agent_pre_update(entity: Agent, existing: Agent, request: Request) -> None:
    try:
        await check_agent_on_update(entity, existing, storage_provider=request.app.state.storage_provider)
    except EntityCheckError as exc:
        raise _agent_check_as_rest_error(exc) from exc


agent_router = make_crud_router(
    model_cls=Agent,
    storage_dep=get_agent_storage,
    plural="agents",
    tag="agents",
    cdc_kind="agent",
    managed_by_field="harness_id",
    search_fields=["id", "description"],
    on_pre_create=_agent_pre_create,
    on_pre_update=_agent_pre_update,
)


@agent_router.get(
    "/agents/{agent_id}/status",
    summary="Validate the agent's external references",
    responses=common_responses(404, 500),
)
async def agent_status(
    agent_id: str = Path(..., description="Agent id"),
    agents=Depends(get_agent_storage),
    llm_providers=Depends(get_llm_provider_storage),
    toolsets=Depends(get_toolset_storage),
    model_profiles=Depends(get_model_profile_storage),
) -> dict:
    """Returns ``{"ok": bool, "issues": [...], "warnings": [...]}``.

    ``issues`` describe unresolved references on the agent (they make ``ok``
    false). ``warnings`` are things that may work but probably do not (01a10c6b):
    an output cap that fills the model's context window. Does NOT call the live
    LLM or toolset providers — that would belong on a future ``/ready``
    endpoint with stronger semantics.
    """
    agent: Agent | None = await agents.get(agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id!r} does not exist")

    issues: list[str] = []
    warnings: list[str] = []

    # The agent names a ModelProfile, which in turn names the provider --
    # OR, for kind="aggregated" (01a067c4), names an ordered pool of
    # member profiles instead, each of which names its own provider. All
    # hops are reported independently so a broken agent says WHICH link
    # is missing rather than just "unhealthy".
    profile_id = agent.model.profile_id
    profile = await model_profiles.get(profile_id)
    window: int | None = None
    if profile is None:
        issues.append(f"ModelProfile {profile_id!r} does not exist")
    elif profile.kind == "single":
        window = profile.context_length
        if await llm_providers.get(profile.provider_id) is None:
            issues.append(
                f"LLMProvider {profile.provider_id!r} referenced by ModelProfile "
                f"{profile_id!r} does not exist"
            )
    else:  # kind == "aggregated": no provider_id of its own -- walk members.
        member_windows: list[int] = []
        for member_id in profile.members or []:
            member = await model_profiles.get(member_id)
            if member is not None and member.kind == "single" and member.context_length:
                member_windows.append(member.context_length)
            if member is None:
                issues.append(
                    f"ModelProfile {profile_id!r} member {member_id!r} "
                    f"does not exist"
                )
            elif member.kind != "single":
                issues.append(
                    f"ModelProfile {profile_id!r} member {member_id!r} is "
                    f"not a single-kind profile (nested aggregation is "
                    f"not supported)"
                )
            elif await llm_providers.get(member.provider_id) is None:
                issues.append(
                    f"LLMProvider {member.provider_id!r} referenced by "
                    f"ModelProfile {profile_id!r} member {member_id!r} "
                    f"does not exist"
                )

    if profile is not None and profile.kind != "single" and member_windows:
        # A cap between the member windows can still be taken by the larger one: warn only when NO member could
        # ever take the call (the executor's own guard reads the MIN window, a separate finding of 01a10c6b item 1).
        window = max(member_windows)
    cap_warning = output_cap_warning(agent.max_output_tokens, window)
    if cap_warning is not None:
        warnings.append(cap_warning)

    # ``agent.tools`` carries scoped tool ids of the form
    # ``<toolset_id>__<bare_name>`` (or, for tools with no scope prefix,
    # the bare name itself). For each one we want to verify that the
    # owning Toolset row exists. Group by toolset to avoid issuing the
    # same lookup twice when an agent references several tools from one
    # toolset.
    # The resolution is the one the create / update check uses
    # (primer/agent/agent_checks.py): a built-in toolset (system /
    # workspaces / misc / web / harness ..., including their retired
    # underscore-prefixed ids) has no Toolset row and is skipped; any
    # other must have one.
    for ts_id in await missing_toolset_ids(agent.tools, toolsets=toolsets):
        issues.append(
            f"Toolset {ts_id!r} referenced by tools does not exist"
        )

    return {"ok": not issues, "issues": issues, "warnings": warnings}


# ---- Graph router ----------------------------------------------------------

graph_router = make_crud_router(
    model_cls=Graph,
    storage_dep=get_graph_storage,
    plural="graphs",
    tag="graphs",
    cdc_kind="graph",
    managed_by_field="harness_id",
    search_fields=["id", "description"],
)


@graph_router.get(
    "/graphs/{graph_id}/status",
    summary="Validate the graph's external references",
    responses=common_responses(404, 500, 503),
)
async def graph_status(
    graph_id: str = Path(..., description="Graph id"),
    graphs=Depends(get_graph_storage),
    agents=Depends(get_agent_storage),
) -> dict:
    """Returns ``{"ok": bool, "issues": [...]}`` describing any
    unresolved references on the graph (agent-node and subgraph-node
    references)."""
    graph: Graph | None = await graphs.get(graph_id)
    if graph is None:
        raise NotFoundError(f"Graph {graph_id!r} does not exist")

    issues: list[str] = []

    for node in graph.nodes:
        agent_ref_id = getattr(node, "agent_id", None)
        subgraph_ref_id = getattr(node, "graph_id", None)

        if agent_ref_id is not None:
            if await agents.get(agent_ref_id) is None:
                issues.append(
                    f"node {node.id!r} references missing Agent {agent_ref_id!r}"
                )
        elif subgraph_ref_id is not None:
            if await graphs.get(subgraph_ref_id) is None:
                issues.append(
                    f"node {node.id!r} references missing Graph {subgraph_ref_id!r}"
                )

    return {"ok": not issues, "issues": issues}


# ---- Graph run turn-log routes ---------------------------------------------


@graph_router.get(
    "/graphs/{graph_id}/runs/{run_id}/turn_log",
    summary="Read graph-level turn log (superstep events)",
    responses=common_responses(404, 500, 503),
)
async def get_graph_run_turn_log(
    graph_id: str = Path(..., description="Graph id"),
    run_id: str = Path(..., description="Run id (WorkspaceSession or GraphThread id)"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    since_seq: int | None = Query(default=None, ge=0),
    sessions=Depends(get_session_storage),
    workspace_registry=Depends(get_workspace_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Read the per-run graph-level turn log.

    Resolution order:
    1. ``run_id`` matches a WorkspaceSession bound to a graph → read
       ``<state_path>/graphs/<run_id>/turns.jsonl`` via the workspace.
    2. ``run_id`` matches a GraphThread → query TurnLogRecord storage
       for ``run_id == run_id`` AND ``node_id IS NULL``.
    3. Neither → 404.
    """
    return await _serve_graph_turn_log(
        run_id=run_id,
        node_id=None,
        limit=limit,
        offset=offset,
        since_seq=since_seq,
        sessions=sessions,
        workspace_registry=workspace_registry,
        storage_provider=storage_provider,
    )


@graph_router.get(
    "/graphs/{graph_id}/runs/{run_id}/nodes/{node_id}/turn_log",
    summary="Read a single node's turn log within a graph run",
    responses=common_responses(404, 500),
)
async def get_graph_node_turn_log(
    graph_id: str = Path(..., description="Graph id"),
    run_id: str = Path(..., description="Run id (WorkspaceSession or GraphThread id)"),
    node_id: str = Path(..., description="Node id"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    since_seq: int | None = Query(default=None, ge=0),
    sessions=Depends(get_session_storage),
    workspace_registry=Depends(get_workspace_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Same dispatch as the graph-level route but scoped to a single
    node. For workspace runs this reads
    ``<state_path>/graphs/<run_id>/nodes/<node_id>/turns.jsonl``.
    For storage runs it filters on ``node_id == node_id``."""
    return await _serve_graph_turn_log(
        run_id=run_id,
        node_id=node_id,
        limit=limit,
        offset=offset,
        since_seq=since_seq,
        sessions=sessions,
        workspace_registry=workspace_registry,
        storage_provider=storage_provider,
    )


# _FanoutInstance.synthesized_id's broadcast/map format (primer/graph/
# _node_refs.py): "{target}[{i}]" -- e.g. "worker[2]". tee's synthesized
# id has no bracket (it's a distinct node id already, no base-id
# ambiguity), so it never matches this and is treated as its own base.
_FANOUT_INSTANCE_RE = re.compile(r"^(?P<base>.+)\[(?P<idx>\d+)\]$")


def _base_node_id(node_id: str) -> str:
    """Strip a fan-out instance suffix ("worker[2]" -> "worker").
    Returns node_id unchanged when it isn't instance-qualified."""
    m = _FANOUT_INSTANCE_RE.match(node_id)
    return m.group("base") if m else node_id


def _instance_sort_key(node_id: str) -> tuple[str, int]:
    m = _FANOUT_INSTANCE_RE.match(node_id)
    if m:
        return (m.group("base"), int(m.group("idx")))
    return (node_id, -1)


class _NodeStateOut(BaseModel):
    """One node's runtime snapshot for the run-view canvas + inspector."""

    node_id: str
    kind: str
    status: str
    iteration: int | None = None
    last_run_at: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None
    error: str | None = None


@graph_router.get(
    "/graphs/{graph_id}/runs/{run_id}/node_states",
    summary="Per-node runtime status snapshot for a graph run",
    responses=common_responses(404, 500),
)
async def get_graph_run_node_states(
    graph_id: str = Path(..., description="Graph id"),
    run_id: str = Path(..., description="Run id (WorkspaceSession or GraphThread id)"),
    graphs=Depends(get_graph_storage),
    sessions=Depends(get_session_storage),
    workspace_registry=Depends(get_workspace_registry),
    storage_provider=Depends(get_storage_provider),
) -> dict:
    """Project the persisted per-node ``NodeRuntimeState`` for a run,
    joined with the graph definition so every node id carries its
    ``kind``. Nodes that have not run yet (absent from the persisted
    state map) surface as ``pending``.

    A fan-out target (broadcast/map) runs as N instances keyed
    ``"{base}[{i}]"`` in the state map (``_FanoutInstance.synthesized_id``),
    not under its bare graph-definition id -- each live instance is
    surfaced as its own row (``node_id="worker[0]"`` etc.), grouped
    under the base node's declared ``kind``, rather than one row per
    graph-definition node that a bare-id lookup would never match (the
    lookup always missed, so every fan-out node showed "pending"
    forever regardless of its real status -- 01a05935 item 1).

    Resolution mirrors the graph turn-log routes:
    1. ``run_id`` is a WorkspaceSession bound to a graph -> read
       ``<state_path>/graphs/<run_id>/state.json`` via the workspace.
    2. ``run_id`` is a GraphThread -> read ``thread.node_states``.
    3. Neither -> 404.
    """
    graph: Graph | None = await graphs.get(graph_id)
    if graph is None:
        raise NotFoundError(f"Graph {graph_id!r} does not exist")
    kinds: dict[str, str] = {node.id: node.kind for node in graph.nodes}

    state_map = await _load_run_node_states(
        run_id=run_id,
        sessions=sessions,
        workspace_registry=workspace_registry,
        storage_provider=storage_provider,
    )

    by_base_id: dict[str, list[str]] = defaultdict(list)
    for state_key in state_map:
        by_base_id[_base_node_id(state_key)].append(state_key)
    for instance_keys in by_base_id.values():
        instance_keys.sort(key=_instance_sort_key)

    items: list[dict] = []
    for node_id, kind in kinds.items():
        instance_keys = by_base_id.get(node_id) or [None]
        for key in instance_keys:
            ns = (state_map.get(key) or {}) if key is not None else {}
            items.append(
                _NodeStateOut(
                    node_id=key if key is not None else node_id,
                    kind=kind,
                    status=ns.get("status", "pending"),
                    iteration=ns.get("last_run_iteration"),
                    last_run_at=ns.get("last_run_at"),
                    error=ns.get("error"),
                ).model_dump()
            )
    return {"items": items, "run_id": run_id, "graph_id": graph_id}


async def _load_run_node_states(
    *,
    run_id: str,
    sessions,
    workspace_registry,
    storage_provider,
) -> dict[str, dict]:
    """Return ``{node_id: {status, last_run_iteration, last_run_at, error}}``
    for ``run_id``, dispatching WorkspaceSession vs GraphThread exactly
    like :func:`_serve_graph_turn_log`. Raises NotFoundError when neither
    backend knows ``run_id``.

    ``{}`` (every node renders ``pending``) is returned ONLY when the run
    genuinely has no state yet: the state file does not exist. A missing
    workspace, a failed read, and corrupt JSON are different facts that
    must not look like "has not started" -- an operator watching a
    crashed run would otherwise see a board of pending nodes and wait
    indefinitely -- so each raises instead."""
    import json

    # 1. WorkspaceSession (workspace-backed graph): read state.json.
    sess: WorkspaceSession | None = await sessions.get(run_id)
    if sess is not None and isinstance(sess.binding, GraphSessionBinding):
        workspace = await workspace_registry.get_workspace(sess.workspace_id)
        if workspace is None:
            raise NotFoundError(
                f"Workspace {sess.workspace_id!r} for graph run {run_id!r} "
                "no longer exists; its node state cannot be read"
            )
        state_path = getattr(workspace, "state_path", ".state")
        rel = f"{state_path}/graphs/{run_id}/state.json"
        try:
            raw = await workspace.read_file(rel)
        except NotFoundError:
            # Both workspace backends raise NotFoundError for an absent
            # file: the run has not written any state yet.
            return {}
        except PrimerError:
            raise
        except Exception as exc:  # noqa: BLE001 -- surfaced, not swallowed
            raise PrimerError(
                f"Could not read node state for graph run {run_id!r}: "
                f"{type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:  # JSONDecodeError + UnicodeDecodeError
            raise PrimerError(
                f"Node state for graph run {run_id!r} is corrupt "
                f"({type(exc).__name__}: {exc})",
                cause=exc,
            ) from exc
        node_states = payload.get("node_states") if isinstance(payload, dict) else None
        if not isinstance(node_states, dict):
            raise PrimerError(
                f"Node state for graph run {run_id!r} is malformed: "
                "expected an object with a 'node_states' mapping"
            )
        return node_states

    # 2. GraphThread (storage-backed graph): read thread.node_states.
    from primer.model.graph import GraphThread

    thread_storage = storage_provider.get_storage(GraphThread)
    thread = await thread_storage.get(run_id)
    if thread is not None:
        return {
            nid: {
                "status": ns.status.value,
                "last_run_iteration": ns.last_run_iteration,
                "last_run_at": (
                    ns.last_run_at.isoformat() if ns.last_run_at else None
                ),
                "error": ns.error,
            }
            for nid, ns in thread.node_states.items()
        }

    raise NotFoundError(f"Graph run {run_id!r} does not exist")


async def _serve_graph_turn_log(
    *,
    run_id: str,
    node_id: str | None,
    limit: int,
    offset: int,
    since_seq: int | None,
    sessions,
    workspace_registry,
    storage_provider,
) -> dict:
    # 1. WorkspaceSession (workspace-backed graph)
    sess: WorkspaceSession | None = await sessions.get(run_id)
    if sess is not None and isinstance(sess.binding, GraphSessionBinding):
        # Lazy import to defer the sessions-router dependency.
        from primer.api.routers.sessions import _read_workspace_turn_log
        workspace = await workspace_registry.get_workspace(sess.workspace_id)
        if workspace is None:
            return {
                "items": [], "total": 0,
                "offset": offset, "limit": limit,
            }
        state_path = getattr(workspace, "state_path", ".state")
        if node_id is None:
            rel = f"{state_path}/graphs/{run_id}/turns.jsonl"
        else:
            rel = f"{state_path}/graphs/{run_id}/nodes/{node_id}/turns.jsonl"
        return await _read_workspace_turn_log(
            workspace=workspace,
            relative_path=rel,
            limit=limit,
            offset=offset,
            since_seq=since_seq,
            workspace_id=sess.workspace_id,
            session_id=run_id,
        )

    # 2. GraphThread (storage-backed graph)
    from primer.model.graph import GraphThread
    from primer.model.turn_log import TurnLogRecord

    thread_storage = storage_provider.get_storage(GraphThread)
    thread = await thread_storage.get(run_id)
    if thread is not None:
        log_storage = storage_provider.get_storage(TurnLogRecord)
        return await _query_storage_turn_log(
            storage=log_storage,
            run_id=run_id,
            node_id=node_id,
            limit=limit,
            offset=offset,
            since_seq=since_seq,
        )

    raise NotFoundError(f"Graph run {run_id!r} does not exist")


async def _query_storage_turn_log(
    *,
    storage,
    run_id: str,
    node_id: str | None,
    limit: int,
    offset: int,
    since_seq: int | None,
) -> dict:
    """Build a Predicate for (run_id [, node_id [, since_seq]]) and
    fetch one offset page of TurnLogRecord rows."""
    from primer.model.storage import (
        FieldRef,
        OffsetPage,
        Op,
        Predicate,
        Value,
    )

    predicate: Predicate = Predicate(
        left=FieldRef(name="run_id"),
        op=Op.EQ,
        right=Value(value=run_id),
    )
    # node_id filter: graph-level events have node_id NULL; the SQL
    # `= NULL` predicate is always UNKNOWN, so use IS_NULL explicitly.
    if node_id is None:
        predicate = Predicate(
            left=predicate,
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="node_id"),
                op=Op.IS_NULL,
                right=Value(value=None),
            ),
        )
    else:
        predicate = Predicate(
            left=predicate,
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="node_id"),
                op=Op.EQ,
                right=Value(value=node_id),
            ),
        )
    if since_seq is not None:
        predicate = Predicate(
            left=predicate,
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="seq"),
                op=Op.GT,
                right=Value(value=since_seq),
            ),
        )
    page = OffsetPage(offset=offset, length=limit)
    response = await storage.find(predicate, page)
    items = [_record_to_event_dict(r) for r in response.items]
    return {
        "items": items,
        "total": response.total,
        "offset": offset,
        "limit": limit,
    }


def _record_to_event_dict(rec) -> dict:
    """Flatten a TurnLogRecord back into a TurnLogEvent-shaped dict.

    The storage row's flat columns (seq, kind, ts/created_at, node_id,
    iteration, superstep_id) plus the payload blob round-trip to the
    same wire shape the JSONL writer emits, so the UI renderer doesn't
    need to know which backend served the row.
    """
    base = {
        "seq": rec.seq,
        "kind": rec.kind.value if hasattr(rec.kind, "value") else rec.kind,
        "ts": rec.created_at.isoformat()
            if hasattr(rec.created_at, "isoformat") else rec.created_at,
        "node_id": rec.node_id,
        "iteration": rec.iteration,
        "superstep_id": rec.superstep_id,
    }
    # A legacy FAILED row's error.extensions.traceback is never served.
    base.update(record_without_traceback(rec.payload or {}))
    return base


__all__ = ["agent_router", "graph_router"]
