"""Phase-2 compute entity routers: Agent + Graph.

Each entity follows the standard CRUD + Find shape from
:mod:`matrix.api.routers._crud`, plus an entity-specific status check
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

from fastapi import APIRouter, Depends, Path

from matrix.api.deps import (
    get_agent_storage,
    get_graph_storage,
    get_llm_provider_storage,
    get_toolset_storage,
)
from matrix.api.errors import common_responses
from matrix.api.routers._cdc_hooks import make_cdc_hooks
from matrix.api.routers._crud import make_crud_router
from matrix.api.routers._managed import (
    on_pre_update_reject_if_managed,
    reject_if_body_sets_harness_id,
    reject_if_managed,
)
from matrix.model.agent import Agent
from matrix.model.except_ import NotFoundError
from matrix.model.graph import Graph


# CDC hooks fan out create/update/delete events into the internal
# collections vector store, so semantic search stays current between
# bootstraps. The hooks no-op when the subsystem isn't activated.
_agent_create, _agent_update, _agent_delete = make_cdc_hooks("agent", Agent)
_graph_create, _graph_update, _graph_delete = make_cdc_hooks("graph", Graph)


# ---- Agent router ----------------------------------------------------------

agent_router = make_crud_router(
    model_cls=Agent,
    storage_dep=get_agent_storage,
    plural="agents",
    tag="agents",
    on_create=_agent_create,
    on_update=_agent_update,
    on_delete=_agent_delete,
    on_pre_create=reject_if_body_sets_harness_id,
    on_pre_update=on_pre_update_reject_if_managed,
    on_pre_delete=reject_if_managed,
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
            toolset_id, _, _ = tool_id.partition("__")
        else:
            toolset_id = tool_id
        if toolset_id in seen_toolset_ids:
            continue
        seen_toolset_ids.add(toolset_id)
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
    on_create=_graph_create,
    on_update=_graph_update,
    on_delete=_graph_delete,
    on_pre_create=reject_if_body_sets_harness_id,
    on_pre_update=on_pre_update_reject_if_managed,
    on_pre_delete=reject_if_managed,
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


__all__ = ["agent_router", "graph_router"]
