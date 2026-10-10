"""invoke_graph: run a target graph inside the current workspace session,
namespaced under the session's state, and return its output. Reuses the
subgraph machinery (a child WorkspaceGraphExecutor)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from primer.common.log import redact_credentials
from primer.model.chat import Message, TextPart


class ChildGraphFailed(Exception):
    """The invoked child graph ended ``failed``: the invoking agent gets an ERROR result, not an empty output.

    Raised by :func:`run_invoke_graph` and :func:`resume_invoke_graph` when the child's stream carried a terminal
    ``_GraphErrorEvent`` and the child did not re-park. Each caller turns it into the error result its own surface
    delivers: the ``invoke_graph`` tool handler (``ToolCallResult``) for a first run, ``GraphFrame.resume_leaf`` and
    ``GraphFrame.resume`` (``ToolResultPart``) for a resume. :meth:`result_json` is the one body they all deliver.

    An approval REFUSAL (the operator said no, the reply was unreadable, the gate timed out or was cancelled) has the
    shape the flat agent-session approval gate delivers (``yield_runtime._resume_tool_approval``), so a model reads
    the same text for the same refusal wherever the gate sits. Any other failure (a tool crash, a routing error, a
    failed fan-in) is ``{"error": <code>, "message", "node_id"}``.
    """

    def __init__(self, *, code: str, message: str, node_id: str | None, tool_name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id
        self.tool_name = tool_name

    @property
    def refused(self) -> bool:
        """True when the child failed because its approval gate was refused, timed out or was cancelled."""
        # The codes are read from the exception that produces them, so a new refusal kind cannot drift out of this set.
        from primer.graph.base import _ToolApprovalRejected

        return self.code in {
            _ToolApprovalRejected(kind=kind).ended_detail_code for kind in (None, "rejected", "timeout", "cancelled")
        }

    def result_json(self) -> str:
        if self.refused:
            return json.dumps({
                "rejected": True,
                "reason": self.message or "(no reason supplied)",
                "tool_name": self.tool_name or "unknown",
            })
        # The child's failure text is whatever its node's exception printed: it goes into the parent's tool result and so into the model's context,
        # the session record and an MCP answer (01a11fbc-d6de). A refusal's reason, above, is what a person typed; both consumers deliver the body as an ERROR
        # result, so ``ToolResultPart`` masks the reason too when it is credential-shaped.
        return json.dumps({"error": self.code, "message": redact_credentials(self.message), "node_id": self.node_id})


def _gated_tool_name(checkpoint: dict[str, Any] | None, node_id: str | None) -> str | None:
    """The tool the child's parked node was gating, as the approval park recorded it (``original_call``)."""
    for key in ("pending_toolcalls", "pending_agent_yields"):
        for entry in (checkpoint or {}).get(key) or []:
            if entry.get("node_id") != node_id:
                continue
            original = (entry.get("resume_metadata") or {}).get("original_call") or {}
            if original.get("name"):
                return original["name"]
    return None


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

    A child that ends ``failed`` (its stream carries a terminal
    ``_GraphErrorEvent``) raises :class:`ChildGraphFailed`: its output is not
    the graph's result, and returning it would hand the agent an empty success.
    """
    # Imported lazily: these runtime dataclasses live in primer.graph.base,
    # which pulls in jinja2 + jsonschema. Keeping the import local avoids
    # forcing that cost on importers of this thin module.
    from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent
    from primer.model.yield_ import YieldToWorker

    graph = await services.resolve_graph(graph_id)
    sub_gsid = f"{services.graph_session_id}__invoke_{tool_call_id}"
    child = await services.build_child_executor(graph=graph, gsid=sub_gsid)

    end_text: str | None = None
    delta_buf: list[str] = []
    failure: _GraphErrorEvent | None = None
    try:
        async for ev in child.invoke(
            [Message(role="user", parts=[TextPart(text=graph_input)])]
        ):
            if isinstance(ev, _GraphErrorEvent):
                failure = failure or ev  # the first is the root failure
                continue
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

    if failure is not None:
        raise ChildGraphFailed(code=failure.code, message=failure.message, node_id=failure.node_id)
    if end_text is not None:
        return end_text
    return "".join(delta_buf)


async def resume_invoke_graph(
    *, child, checkpoint, payload, resumed_tcid=None, resumed_event_key=None, agent_tool_result=None,
    resume_session_id=None, resolve_provider=None,
):
    """Resume a parked child graph from its checkpoint, returning
    ``(output_text, repark)``. ``output_text`` is the graph's final text once
    it drains to completion (None if it re-parked first); ``repark`` is the
    child's re-park YieldToWorker if another gate is still pending, else None.
    A child that ends ``failed`` instead (a refused gate, a tool crash, a
    routing error) raises :class:`ChildGraphFailed`.

    Mirrors graph_resume.resume_graph_from_checkpoint's rejection handling but
    also collects the ``_GraphEndOutputEvent`` output text.

    ``resumed_event_key`` is the event key of the child's gate that was answered (the leaf's own): two siblings of the child's superstep can share the
    raw ``resumed_tcid``, and only the key says which of them this reply is for (C-033, ticket 01a11fc6-0cce).

    ``payload`` also reaches the child as ``toolcall_payload``: a
    value-yielding ``tool_call`` node inside the child (``ask_user``, a python
    toolset's tool) takes the operator's reply from it, and the executor
    ignores it for an approval gate. ``resume_session_id`` and
    ``resolve_provider`` are the session being resumed and the provider
    registry's ``get_toolset``, for the ``ResumeContext`` that node's resume
    hook receives. They default ``None`` for a caller with no value-yielding
    node to resume (``GraphFrame.resume`` delivers a finished child result as
    ``agent_tool_result`` instead)."""
    from primer.graph.base import _GraphEndOutputEvent, _GraphErrorEvent, _ToolApprovalRejected
    from primer.model.yield_ import YieldToWorker
    from primer.worker.graph_resume import _decision_from_payload

    decision, reason, kind = _decision_from_payload(payload)
    if decision != "approved" and agent_tool_result is None:
        rejection_reason = reason or "rejected"

        async def _rejecting_dispatch(node, arguments, inner_call=None):
            raise _ToolApprovalRejected(rejection_reason, kind=kind)

        child._dispatch_toolcall_with_bypass = _rejecting_dispatch

    end_text = None
    delta_buf: list[str] = []
    repark = None
    failure: _GraphErrorEvent | None = None
    try:
        async for ev in child.resume_from_checkpoint(
            checkpoint, resumed_tcid=resumed_tcid, resumed_event_key=resumed_event_key,
            agent_tool_result=agent_tool_result,
            toolcall_payload=payload,
            resume_session_id=resume_session_id,
            resolve_provider=resolve_provider,
        ):
            if isinstance(ev, _GraphErrorEvent):
                failure = failure or ev  # the first is the root failure
            elif isinstance(ev, _GraphEndOutputEvent):
                end_text = ev.text
            elif getattr(ev, "type", None) == "text-delta":
                d = getattr(ev, "text", None)
                if d:
                    delta_buf.append(d)
    except YieldToWorker as yld:
        repark = yld

    if failure is not None and repark is None:
        raise ChildGraphFailed(
            code=failure.code, message=failure.message, node_id=failure.node_id,
            tool_name=_gated_tool_name(checkpoint, failure.node_id),
        )
    out = end_text if end_text is not None else "".join(delta_buf)
    return out, repark
