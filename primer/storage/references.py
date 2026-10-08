"""The one query behind a reference-integrity block, and the declaration of what references an agent and a graph.

A parent entity must not be deleted while a child row still references it. The REST routers
(:mod:`primer.api.routers._references`) and the system CRUD tools (:mod:`primer.toolset._system_guards`) both ask the same
question, "is there at least one row of the child kind that references the parent id", so the query lives here once and neither
surface can drift from the other on how a reference is looked for.

Most references are a field that equals, or contains, the parent id (:func:`first_referencing_row`). Two kinds are not, and have their
own lookup (:data:`Lookup`): a SESSION references an agent or a graph only while it is not ended (an ended session is history), and a
GRAPH references an agent or another graph through its ``nodes`` list, an array of objects the predicate language cannot index into.

:data:`AGENT_REFERENCES` and :data:`GRAPH_REFERENCES` are the single declaration of what blocks an agent's and a graph's deletion. The
REST router for each builds its ``ReferenceCheck`` list from them and the system tools build their ``ToolReference`` tuple from them,
so the route and the tool cannot disagree about WHAT blocks, any more than about how it is looked for.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from primer.model.graph import Graph
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.trigger import Subscription
from primer.model.workspace_session import SessionStatus, WorkspaceSession

# ``(storage of the child kind, parent id) -> a referencing row or None``, for references the field query cannot express.
Lookup = Callable[[Any, str], Awaitable[Any | None]]

# The storage layer caps a page at this many rows; a walk over every row asks for full pages.
_PAGE = 200


async def first_referencing_row(
    storage: Any, *, field: str, op: Op, parent_id: str, also: Predicate | None = None,
) -> Any | None:
    """Return one child row whose ``field`` matches ``parent_id`` under ``op``, or ``None`` when nothing references it.

    ``op`` is :attr:`~primer.model.storage.Op.EQ` for a plain foreign-key field and ``Op.CONTAINS`` for a JSON array field
    (an aggregate profile's ``members``). ``also`` is an extra condition the row must meet as well (AND-ed on). Only one row is
    fetched: callers treat the answer as "at least one child exists".
    """
    match: Predicate = Predicate(left=FieldRef(name=field), op=op, right=Value(value=parent_id))
    if also is not None:
        match = Predicate(left=match, op=Op.AND, right=also)
    page = await storage.find(match, OffsetPage(offset=0, length=1))
    return page.items[0] if page.items else None


# A session counts as a reference while it is anything but ENDED. Spelled as "not ended", not "in this list of statuses", so a status
# added later is live by default: the safe direction for a check whose job is to refuse a delete.
_NOT_ENDED = Predicate(left=FieldRef(name="status"), op=Op.NE, right=Value(value=SessionStatus.ENDED.value))


async def first_live_session_bound_to_agent(storage: Any, parent_id: str) -> Any | None:
    return await first_referencing_row(
        storage, field="binding.agent_id", op=Op.EQ, parent_id=parent_id, also=_NOT_ENDED,
    )


async def first_live_session_bound_to_graph(storage: Any, parent_id: str) -> Any | None:
    return await first_referencing_row(
        storage, field="binding.graph_id", op=Op.EQ, parent_id=parent_id, also=_NOT_ENDED,
    )


async def _first_graph_with_node(
    storage: Any, parent_id: str, *, node_kind: str, id_attr: str, skip_own_row: bool,
) -> Any | None:
    """Walk EVERY graph and return the first whose ``nodes`` holds a node of ``node_kind`` whose ``id_attr`` is ``parent_id``.

    ``nodes`` is a list of objects, which the predicate language cannot match into, so the graphs are read a page at a time. The
    lookup is exact on the node's ``kind``: a sub-graph node whose ``graph_id`` happens to equal an agent id is not a reference to
    that agent. ``skip_own_row`` leaves out the parent's own row: a graph that names itself must stay deletable.
    """
    offset = 0
    while True:
        page = await storage.list(OffsetPage(offset=offset, length=_PAGE))
        for graph in page.items:
            if skip_own_row and graph.id == parent_id:
                continue
            for node in graph.nodes:
                if getattr(node, "kind", None) == node_kind and getattr(node, id_attr, None) == parent_id:
                    return graph
        if len(page.items) < _PAGE:
            return None
        offset += _PAGE


async def first_graph_with_agent_node(storage: Any, parent_id: str) -> Any | None:
    return await _first_graph_with_node(storage, parent_id, node_kind="agent", id_attr="agent_id", skip_own_row=False)


async def first_graph_with_subgraph_node(storage: Any, parent_id: str) -> Any | None:
    return await _first_graph_with_node(storage, parent_id, node_kind="graph", id_attr="graph_id", skip_own_row=True)


@dataclass(frozen=True)
class ReferenceSpec:
    """One child kind that blocks a parent's deletion, declared once for the REST route and the system tool.

    ``child_field`` is the field the REST ``ReferenceCheck`` and the tool's ``ToolReference`` query when ``lookup`` is ``None``; with a
    ``lookup`` it only documents where the reference lives (``nodes[].agent_id`` is not a path the storage layer can follow).
    """

    child_kind: str
    child_model: type
    child_field: str
    op: Op = Op.EQ
    lookup: Lookup | None = None


AGENT_REFERENCES: tuple[ReferenceSpec, ...] = (
    ReferenceSpec("graph", Graph, "nodes[].agent_id", lookup=first_graph_with_agent_node),
    ReferenceSpec("session", WorkspaceSession, "binding.agent_id", lookup=first_live_session_bound_to_agent),
    ReferenceSpec("trigger subscription", Subscription, "config.agent_id"),
)

GRAPH_REFERENCES: tuple[ReferenceSpec, ...] = (
    ReferenceSpec("graph", Graph, "nodes[].graph_id", lookup=first_graph_with_subgraph_node),
    ReferenceSpec("session", WorkspaceSession, "binding.graph_id", lookup=first_live_session_bound_to_graph),
    ReferenceSpec("trigger subscription", Subscription, "config.graph_id"),
)


__all__ = [
    "AGENT_REFERENCES",
    "GRAPH_REFERENCES",
    "Lookup",
    "ReferenceSpec",
    "first_graph_with_agent_node",
    "first_graph_with_subgraph_node",
    "first_live_session_bound_to_agent",
    "first_live_session_bound_to_graph",
    "first_referencing_row",
]
