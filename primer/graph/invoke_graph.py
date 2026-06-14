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


def _restamp_as_invoke_graph(
    child_yld: "Any", *, sub_gsid: str, graph_id: str,
    agent_tool_call_id: str,
) -> "Any":
    """RETIRED for the descent path (Task 5.1): ``run_invoke_graph`` no longer
    re-stamps - it pushes a ``GraphFrame`` and re-raises the child's real leaf
    so the worker routes resume through the generic continuation walk. Kept
    importable because ``primer.worker.pool._repark_invoke_graph_outcome``
    (the now-dead legacy ``tool_name=='invoke_graph'`` resume branch) still
    imports it; that branch is harmless and untouched.

    Re-wrap a child-graph park as an ``invoke_graph`` park on the AGENT
    session: preserve the child's event_key(s) (so the human's reply targets
    the right gate) + checkpoint, but stamp tool_name='invoke_graph' (so the
    worker routes resume to _resume_invoke_graph) and carry the child's
    identity (sub_gsid, graph_id, child_tcid) in resume_metadata. The
    tool_call_id is the AGENT's invoke_graph call id so the resumed
    tool_result pairs with the agent's tool_use. llm_messages is left None so
    the agent executor stamps the agent turn's messages on the way out."""
    from primer.model.yield_ import Yielded, YieldToWorker

    cy = child_yld.yielded
    md = dict(cy.resume_metadata or {})
    md.update({
        "invoke_graph": True,
        "sub_gsid": sub_gsid,
        "graph_id": graph_id,
        "child_tcid": child_yld.tool_call_id,
    })
    repark = YieldToWorker(
        Yielded(
            tool_name="invoke_graph",
            event_key=cy.event_key,
            timeout=cy.timeout,
            resume_metadata=md,
            event_keys=cy.event_keys,
        ),
        tool_call_id=agent_tool_call_id,
    )
    repark.graph_checkpoint = getattr(child_yld, "graph_checkpoint", None)
    return repark


async def run_invoke_graph(
    *,
    graph_id: str,
    graph_input: str,
    services: GraphInvocationServices,
    tool_call_id: str,
) -> str:
    """Run ``graph_id`` to completion inside the session, namespaced under
    ``<graph_session_id>__invoke_<tool_call_id>``, and return its output text.

    When the child graph hits a HITL gate it raises a ``YieldToWorker``
    carrying its OWN leaf (an ``_approval`` / ``ask_user`` / ... gate). The
    descent pushes a :class:`~primer.worker.frames.GraphFrame` (carrying the
    child's identity + checkpoint) onto that yield's ``frames`` stack and
    re-raises the child's real leaf unchanged, so the worker parks the AGENT
    session and routes the eventual resume through the generic continuation
    walk (``GraphFrame.resume_leaf``) rather than the legacy
    ``tool_name=='invoke_graph'`` switch. The frame carries two distinct ids:
    ``tool_call_id`` (the AGENT's invoke_graph call id, the caller-result id)
    and ``node_tcid`` (the child graph's parked-node id, the resumed_tcid).

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
    from primer.model.yield_ import YieldToWorker

    graph = await services.resolve_graph(graph_id)
    sub_gsid = f"{services.graph_session_id}__invoke_{tool_call_id}"
    child = await services.build_child_executor(graph=graph, gsid=sub_gsid)

    end_text: str | None = None
    delta_buf: list[str] = []
    try:
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
    except YieldToWorker as child_yld:
        # Descend: push a GraphFrame onto the child yield's frame stack
        # (root-first) and re-raise the child's REAL leaf unchanged. The
        # worker then routes the park through the generic continuation walk.
        from primer.worker.frames import GraphFrame

        gf = GraphFrame(
            graph_id=graph_id,
            gsid=sub_gsid,
            checkpoint=getattr(child_yld, "graph_checkpoint", None),
            # The AGENT's invoke_graph call id (caller-result id).
            tool_call_id=tool_call_id,
            # The CHILD graph's parked-node id (resumed_tcid).
            node_tcid=child_yld.tool_call_id,
        )
        child_yld.frames = [gf] + list(getattr(child_yld, "frames", []))
        raise

    if end_text is not None:
        return end_text
    return "".join(delta_buf)


async def resume_invoke_graph(
    *, child, checkpoint, payload, resumed_tcid=None, agent_tool_result=None,
):
    """Resume a parked child graph from its checkpoint, returning
    ``(output_text, repark)``. ``output_text`` is the graph's final text once
    it drains to completion (None if it re-parked first); ``repark`` is the
    child's re-park YieldToWorker if another gate is still pending, else None.

    Mirrors graph_resume.resume_graph_from_checkpoint's rejection handling but
    also collects the ``_GraphEndOutputEvent`` output text."""
    from primer.graph.base import _GraphEndOutputEvent, _ToolApprovalRejected
    from primer.model.yield_ import YieldToWorker
    from primer.worker.graph_resume import _decision_from_payload

    decision, reason = _decision_from_payload(payload)
    if decision != "approved" and agent_tool_result is None:
        rejection_reason = reason or "rejected"

        async def _rejecting_dispatch(node, arguments):
            raise _ToolApprovalRejected(rejection_reason)

        child._dispatch_toolcall_with_bypass = _rejecting_dispatch

    end_text = None
    delta_buf: list[str] = []
    repark = None
    try:
        async for ev in child.resume_from_checkpoint(
            checkpoint, resumed_tcid=resumed_tcid,
            agent_tool_result=agent_tool_result,
        ):
            if isinstance(ev, _GraphEndOutputEvent):
                end_text = ev.text
            elif getattr(ev, "type", None) == "text-delta":
                d = getattr(ev, "text", None)
                if d:
                    delta_buf.append(d)
    except YieldToWorker as yld:
        repark = yld

    out = end_text if end_text is not None else "".join(delta_buf)
    return out, repark
