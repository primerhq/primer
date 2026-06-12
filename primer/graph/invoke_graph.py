"""invoke_graph: run a target graph inside the current workspace session,
namespaced under the session's state, and return its output. Reuses the
subgraph machinery (a child WorkspaceGraphExecutor)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from primer.model.chat import Message, TextPart


@dataclass
class GraphInvocationServices:
    """Per-session services invoke_graph needs, built in the worker (where the
    resolvers / state_repo / workspace_session are in scope) and threaded to the
    tool handler via ToolContext.graph_services.

    build_child_executor(graph=..., gsid=...) constructs a child
    WorkspaceGraphExecutor sharing the session's state_repo + resolvers (the
    same construction _build_sub_executor uses), namespaced under ``gsid``.
    """

    resolve_graph: Callable[[str], Awaitable[Any]]
    build_child_executor: Callable[..., Awaitable[Any]]
    session_id: str
    workspace_id: str
    graph_session_id: str


async def run_invoke_graph(
    *,
    graph_id: str,
    graph_input: str,
    services: GraphInvocationServices,
    tool_call_id: str,
) -> str:
    """Run ``graph_id`` to completion inside the session, namespaced under
    ``<graph_session_id>__invoke_<tool_call_id>``, and return its output text.

    (Non-parking happy path; a YieldToWorker from the child is NOT swallowed
    here - parking/resume is a later task.)

    Two output channels are honoured so the returned text matches what the
    session log records for the same graph:

    * ``_GraphEndOutputEvent.text`` - the canonical final output a graph
      executor emits when its End node fires (see
      ``primer.session.persistence`` which records exactly this as the
      assistant token). This is the real path a ``WorkspaceGraphExecutor``
      takes, so it is preferred when present.
    * Raw ``text-delta`` stream events - mirrors ``_stream_subgraph_node``'s
      duck-typed accumulation. Used as a fallback when no end-output event
      is observed (e.g. a stub or a node-level text stream).
    """
    # Imported lazily: these runtime dataclasses live in primer.graph.base,
    # which pulls in jinja2 + jsonschema. Keeping the import local avoids
    # forcing that cost on importers of this thin module.
    from primer.graph.base import _GraphEndOutputEvent

    graph = await services.resolve_graph(graph_id)
    sub_gsid = f"{services.graph_session_id}__invoke_{tool_call_id}"
    child = await services.build_child_executor(graph=graph, gsid=sub_gsid)

    end_text: str | None = None
    delta_buf: list[str] = []
    async for ev in child.invoke(
        [Message(role="user", parts=[TextPart(text=graph_input)])]
    ):
        if isinstance(ev, _GraphEndOutputEvent):
            end_text = ev.text
            continue
        if getattr(ev, "type", None) == "text-delta":
            delta = getattr(ev, "text", None)
            if delta:
                delta_buf.append(delta)

    if end_text is not None:
        return end_text
    return "".join(delta_buf)
