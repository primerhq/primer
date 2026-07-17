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

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from primer.api.deps import (
    get_agent_storage,
    get_graph_storage,
    get_llm_provider_storage,
    get_session_storage,
    get_storage_provider,
    get_toolset_storage,
    get_workspace_registry,
)
from primer.api.errors import common_responses
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.api.routers._crud import make_crud_router
from primer.model.agent import Agent
from primer.model.except_ import NotFoundError
from primer.model.graph import Graph
from primer.model.workspace_session import GraphSessionBinding, WorkspaceSession


# ---- Agent router ----------------------------------------------------------

agent_router = make_crud_router(
    model_cls=Agent,
    storage_dep=get_agent_storage,
    plural="agents",
    tag="agents",
    cdc_kind="agent",
    managed_by_field="harness_id",
    search_fields=["id", "description"],
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
) -> dict:
    """Returns ``{"ok": bool, "issues": [...]}`` describing any
    unresolved references on the agent. Does NOT call the live LLM
    or toolset providers — that would belong on a future ``/ready``
    endpoint with stronger semantics.
    """
    agent: Agent | None = await agents.get(agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id!r} does not exist")

    issues: list[str] = []

    provider_id = agent.model.provider_id
    if await llm_providers.get(provider_id) is None:
        issues.append(f"LLMProvider {provider_id!r} does not exist")

    # ``agent.tools`` carries scoped tool ids of the form
    # ``<toolset_id>__<bare_name>`` (or, for tools with no scope prefix,
    # the bare name itself). For each one we want to verify that the
    # owning Toolset row exists. Group by toolset to avoid issuing the
    # same lookup twice when an agent references several tools from one
    # toolset.
    seen_toolset_ids: set[str] = set()
    missing_toolset_ids: set[str] = set()
    for tool_id in agent.tools:
        if "__" in tool_id:
            toolset_id = tool_id.rpartition("__")[0]
        else:
            toolset_id = tool_id
        if toolset_id in seen_toolset_ids:
            continue
        seen_toolset_ids.add(toolset_id)
        # Built-in toolsets (web / search / system / workspaces / misc /
        # harness) are always resolvable by the live registry — they
        # don't have a Toolset storage row. Skip those.
        if toolset_id in RESERVED_TOOLSET_IDS:
            continue
        if await toolsets.get(toolset_id) is None:
            missing_toolset_ids.add(toolset_id)
    for ts_id in sorted(missing_toolset_ids):
        issues.append(
            f"Toolset {ts_id!r} referenced by tools does not exist"
        )

    return {"ok": not issues, "issues": issues}


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
    responses=common_responses(404, 500),
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
    responses=common_responses(404, 500),
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

    items: list[dict] = []
    for node_id, kind in kinds.items():
        ns = state_map.get(node_id) or {}
        items.append(
            _NodeStateOut(
                node_id=node_id,
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
    backend knows ``run_id``."""
    import json

    # 1. WorkspaceSession (workspace-backed graph): read state.json.
    sess: WorkspaceSession | None = await sessions.get(run_id)
    if sess is not None and isinstance(sess.binding, GraphSessionBinding):
        workspace = await workspace_registry.get_workspace(sess.workspace_id)
        if workspace is None:
            return {}
        state_path = getattr(workspace, "state_path", ".state")
        rel = f"{state_path}/graphs/{run_id}/state.json"
        try:
            raw = await workspace.read_file(rel)
        except Exception:  # noqa: BLE001 -- missing file -> empty state
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        node_states = payload.get("node_states")
        return node_states if isinstance(node_states, dict) else {}

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
    base.update(rec.payload or {})
    return base


__all__ = ["agent_router", "graph_router"]
