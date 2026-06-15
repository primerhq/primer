"""Continuation-stack frame model for the unified nested-yield resume flow.

When a yield (an approval gate or a yielding tool) is raised inside a
*nested* invocation - a subagent invoked via ``system__invoke_agent`` or
a child graph invoked via ``system__invoke_graph`` - the worker must be
able to park the whole chain of in-flight callers and later resume it
from the leaf back up to the root. This module owns the **data model**
for that chain: an ordered stack of frames, one per nested invocation,
plus the small outcome objects a resume walk produces.

The stack is ordered root-first (index 0 is the outermost caller, the
last element is the innermost / closest to the leaf yield). Each frame
captures exactly what a single layer needs to be rehydrated:

* :class:`AgentFrame` - a subagent turn-in-progress: its agent id, the
  mid-flight LLM message history, the tool_call_id that invoked it from
  its parent, and an :class:`AgentResumeContext` carrying the ambient
  ids/principal/tool surface needed to rebuild the agent.
* :class:`GraphFrame` - a child-graph invocation: the graph id, the
  child graph-session id (``gsid``), and the graph executor checkpoint.

Behaviour (``apply_leaf``, ``frame.resume``) is intentionally left as a
stub here - this task delivers only the model + JSON serialisation.
Later tasks fill the stubs in.

Style mirrors :mod:`primer.worker.yield_runtime`: plain dataclasses with
``to_jsonable`` / ``from_jsonable`` round-tripping, I/O kept at the edges.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from primer.graph.invoke_graph import resume_invoke_graph
from primer.model.chat import ToolCallPart, ToolResultPart
from primer.model.yield_ import YieldToWorker
from primer.worker.yield_resume_registry import get_resume_hook
from primer.worker.yield_runtime import classify_approval_payload


# ===========================================================================
# Resume context
# ===========================================================================


@dataclass
class AgentResumeContext:
    """Ambient ids + tool surface needed to rebuild a parked subagent turn.

    Carried inside an :class:`AgentFrame` so that, on resume, the worker can
    reconstruct the agent runtime exactly as it stood when the nested yield
    was raised - same session/workspace/chat binding, same principal, same
    registered tools.

    Attributes
    ----------
    session_id
        The session the (sub)agent turn runs under.
    workspace_id
        The workspace the agent is bound to.
    chat_id
        The chat the turn belongs to, or ``None`` for a pure session turn.
    principal
        The acting principal (used for tool authorisation on resume).
    tools
        The agent's registered tool ids (e.g. ``"system__ask_user"``).
    """

    session_id: str
    workspace_id: str
    chat_id: str | None
    principal: str
    tools: list[str]

    def to_jsonable(self) -> dict[str, Any]:
        """Render to a JSON-safe dict."""
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "chat_id": self.chat_id,
            "principal": self.principal,
            "tools": list(self.tools),
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "AgentResumeContext":
        return cls(
            session_id=data["session_id"],
            workspace_id=data["workspace_id"],
            chat_id=data.get("chat_id"),
            principal=data["principal"],
            tools=list(data.get("tools") or []),
        )


# ===========================================================================
# Frames
# ===========================================================================


@dataclass
class AgentFrame:
    """One nested subagent turn-in-progress on the continuation stack.

    The five data fields are positional in order
    ``(agent_id, llm_messages, tool_call_id, depth, context)`` so callers
    can construct positionally; ``kind`` is a non-positional discriminator
    defaulting to ``"agent"`` (it never needs to be passed in).

    Attributes
    ----------
    agent_id
        The subagent's id.
    llm_messages
        Mid-flight LLM message history up to and including the assistant
        message that raised the nested yield. Canonical Primer dict format.
    tool_call_id
        The tool_call_id by which this agent was invoked from its parent
        frame (the ``system__invoke_agent`` call), used to thread the
        child's result back up on resume.
    depth
        Nesting depth (root invocation = 0, deeper = larger). Carried for
        loop-guarding and diagnostics.
    context
        The :class:`AgentResumeContext` for rebuilding this agent.
    """

    agent_id: str
    llm_messages: list[dict[str, Any]]
    tool_call_id: str
    depth: int
    context: AgentResumeContext
    kind: str = field(default="agent")

    def to_jsonable(self) -> dict[str, Any]:
        """Render to a JSON-safe dict for persistence in the park blob."""
        return {
            "kind": self.kind,
            "agent_id": self.agent_id,
            "llm_messages": list(self.llm_messages),
            "tool_call_id": self.tool_call_id,
            "depth": self.depth,
            "context": self.context.to_jsonable(),
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "AgentFrame":
        return cls(
            agent_id=data["agent_id"],
            llm_messages=list(data.get("llm_messages") or []),
            tool_call_id=data["tool_call_id"],
            depth=int(data["depth"]),
            context=AgentResumeContext.from_jsonable(data["context"]),
        )

    async def resume_leaf(
        self, leaf: Any, payload: Any, services: Any
    ) -> "FrameOutcome":
        """Resolve this frame's OWN leaf when it is the innermost frame.

        The leaf belongs to this subagent's own tool call (an approval gate or
        a yielding tool it raised), so it is resolved via :func:`apply_leaf`
        against this frame's tool context.

        If that re-dispatch itself re-parks (an approved gated tool raised a
        fresh yield), THIS subagent's turn has NOT advanced: the same tool_use
        is now awaiting the tool's own event instead of approval, so this
        ``AgentFrame`` must be RETAINED. The re-dispatched tool yields a BARE
        :class:`~primer.model.yield_.YieldToWorker` (its ``.frames`` is empty),
        so we PREPEND ``self`` (unchanged) onto the re-yield's frames before
        propagating the :class:`Reparked`; otherwise the in-flight subagent
        frame would be dropped by the continuation walk. Otherwise
        ``apply_leaf`` returned the resolved
        :class:`~primer.model.chat.ToolResultPart` for the leaf, which is
        threaded straight into :meth:`resume` to continue the subagent turn.
        """
        leaf_result = await apply_leaf(self, leaf, payload, services)
        if isinstance(leaf_result, Reparked):
            ny = leaf_result.new_yield
            ny.frames = [self] + list(getattr(ny, "frames", None) or [])
            return leaf_result
        return await self.resume(leaf_result, services)

    async def resume(self, child_result: Any, services: Any) -> "FrameOutcome":
        """Resume this agent turn with a completed child's result.

        Delegates to ``services.resume_subagent`` - which rehydrates the
        subagent's mid-flight ``llm_messages``, appends ``child_result`` as the
        resolved tool result for this frame's pending ``tool_call_id``, and
        re-runs the turn. On a clean finish it returns the subagent's final
        assistant text; if the turn raises a fresh nested yield it re-raises
        :class:`~primer.model.yield_.YieldToWorker` (with a new ``AgentFrame``
        already prepended).

        Returns a :class:`Reparked` if the subagent re-yielded, else a
        :class:`Completed` carrying a :class:`ToolResultPart` (keyed by this
        frame's ``tool_call_id``) whose JSON ``output`` wraps the final text
        under an ``"output"`` key.
        """
        try:
            text = await services.resume_subagent(
                agent_id=self.agent_id,
                context=self.context,
                llm_messages=self.llm_messages,
                child_result=child_result,
                depth=self.depth,
                invoke_tool_call_id=self.tool_call_id,
            )
        except YieldToWorker as yld:
            return Reparked(new_yield=yld)
        return Completed(
            value=ToolResultPart(
                id=self.tool_call_id,
                output=json.dumps({"output": text}),
                error=False,
            )
        )


@dataclass
class GraphFrame:
    """One nested child-graph invocation on the continuation stack.

    Attributes
    ----------
    graph_id
        The invoked graph's id.
    gsid
        The child graph-session id.
    checkpoint
        The graph executor checkpoint (JSON-able), produced by the graph's
        snapshot so the mid-flight executor can be rehydrated on resume.
    tool_call_id
        The AGENT's ``system__invoke_graph`` call id - the id this graph was
        invoked under from its parent frame. Used as the result
        :class:`~primer.model.chat.ToolResultPart`'s id (so the resumed graph
        output pairs with the agent's tool_use) and as the frame's identity
        for result pairing.
    node_tcid
        The CHILD graph's parked-node tcid - the tool_call_id of the node
        INSIDE the child graph that raised the leaf yield. Passed as
        ``resumed_tcid`` to :func:`resume_invoke_graph` so the child executor
        delivers the resume to the right parked node. Distinct from
        ``tool_call_id`` (the caller-result id).
    """

    graph_id: str
    gsid: str
    checkpoint: dict[str, Any]
    tool_call_id: str
    node_tcid: str | None = None
    kind: str = field(default="graph")

    def to_jsonable(self) -> dict[str, Any]:
        """Render to a JSON-safe dict for persistence in the park blob."""
        return {
            "kind": self.kind,
            "graph_id": self.graph_id,
            "gsid": self.gsid,
            "checkpoint": dict(self.checkpoint),
            "tool_call_id": self.tool_call_id,
            "node_tcid": self.node_tcid,
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "GraphFrame":
        return cls(
            graph_id=data["graph_id"],
            gsid=data["gsid"],
            checkpoint=dict(data.get("checkpoint") or {}),
            tool_call_id=data["tool_call_id"],
            node_tcid=data.get("node_tcid"),
        )

    async def resume_leaf(
        self, leaf: Any, payload: Any, services: Any
    ) -> "FrameOutcome":
        """Resolve this graph's OWN leaf when it is the innermost frame.

        The leaf belongs to a node INSIDE this child graph, so only the graph's
        own resume can resolve it: rehydrate the child executor
        (``services.resolve_graph`` + ``services.build_child_graph_executor``),
        compute the node's ``agent_tool_result`` from the raw ``payload`` via
        ``services.graph_agent_tool_result`` (None for approval/verdict leaves),
        and hand BOTH the raw ``payload`` and that ``agent_tool_result`` to
        :func:`resume_invoke_graph` (mirroring the pool's
        ``_resume_invoke_graph``).

        Returns a :class:`Reparked` if the child graph raised a fresh yield,
        else a :class:`Completed` carrying a :class:`ToolResultPart` (keyed by
        this frame's ``tool_call_id``) whose JSON ``output`` wraps the graph's
        final text under an ``"output"`` key.
        """
        graph = await services.resolve_graph(self.graph_id)
        child = await services.build_child_graph_executor(graph, self.gsid)
        agent_tool_result = await services.graph_agent_tool_result(
            self.checkpoint, self.node_tcid, payload
        )
        out, repark = await resume_invoke_graph(
            child=child,
            checkpoint=self.checkpoint,
            payload=payload,
            resumed_tcid=self.node_tcid,
            agent_tool_result=agent_tool_result,
        )
        if repark is not None:
            return self._repark_with_advanced_frame(repark)
        return Completed(
            value=ToolResultPart(
                id=self.tool_call_id,
                output=json.dumps({"output": out}),
                error=False,
            )
        )

    def _repark_with_advanced_frame(self, repark: Any) -> "Reparked":
        """Re-park the child graph on its NEXT gate, retaining this frame.

        When ``resume_invoke_graph`` returns a re-yield (the child graph hit
        ANOTHER gate), that re-yield carries the child executor's NEW
        ``graph_checkpoint`` and its new gate node id (``tool_call_id``) but its
        ``.frames`` is empty - it does NOT carry THIS ``invoke_graph`` frame. The
        ``GraphFrame`` (the caller's invoke_graph call) must persist across the
        child's gates with its checkpoint ADVANCED, else the continuation walk
        would drop the in-flight child graph. Build an UPDATED ``GraphFrame``
        (advanced checkpoint + new gate node id; same caller-result
        ``tool_call_id``) and PREPEND it onto the re-yield's frames.
        """
        advanced = GraphFrame(
            graph_id=self.graph_id,
            gsid=self.gsid,
            checkpoint=getattr(repark, "graph_checkpoint", None) or self.checkpoint,
            tool_call_id=self.tool_call_id,
            node_tcid=getattr(repark, "tool_call_id", None) or self.node_tcid,
        )
        repark.frames = [advanced] + list(getattr(repark, "frames", None) or [])
        return Reparked(new_yield=repark)

    async def resume(self, child_result: Any, services: Any) -> "FrameOutcome":
        """Resume this child graph with a completed child's result.

        Rehydrates the child graph executor (``services.resolve_graph`` +
        ``services.build_child_graph_executor``), wraps ``child_result`` as a
        tool-role :class:`~primer.model.chat.Message`, and delegates to
        :func:`~primer.graph.invoke_graph.resume_invoke_graph` to deliver it to
        the parked node and drive the graph forward.

        Returns a :class:`Reparked` if the graph raised a fresh yield, else a
        :class:`Completed` carrying a :class:`ToolResultPart` (keyed by this
        frame's ``tool_call_id``) whose JSON ``output`` wraps the graph's final
        text under an ``"output"`` key.
        """
        from primer.model.chat import Message

        graph = await services.resolve_graph(self.graph_id)
        child = await services.build_child_graph_executor(graph, self.gsid)
        agent_tool_result = Message(role="tool", parts=[child_result])
        out, repark = await resume_invoke_graph(
            child=child,
            checkpoint=self.checkpoint,
            payload=None,
            resumed_tcid=self.node_tcid,
            agent_tool_result=agent_tool_result,
        )
        if repark is not None:
            return self._repark_with_advanced_frame(repark)
        return Completed(
            value=ToolResultPart(
                id=self.tool_call_id,
                output=json.dumps({"output": out}),
                error=False,
            )
        )


Frame = AgentFrame | GraphFrame


# ===========================================================================
# Outcomes
# ===========================================================================


@dataclass(frozen=True)
class Completed:
    """A frame's resume produced a final value (the turn/graph finished).

    The ``completed`` discriminator is ``True`` so a resume walk can branch
    on a single boolean without isinstance-checking.
    """

    value: Any
    completed: bool = field(default=True)


@dataclass(frozen=True)
class Reparked:
    """A frame's resume raised a fresh yield (it must be re-parked).

    The ``completed`` discriminator is ``False``; ``new_yield`` carries the
    yield the frame raised, which the walk re-parks upward.
    """

    new_yield: Any
    completed: bool = field(default=False)


# A frame resume yields one of these two outcomes.
FrameOutcome = Completed | Reparked


# ===========================================================================
# Serialisation
# ===========================================================================


def frames_to_jsonable(frames: list[Frame]) -> list[dict[str, Any]]:
    """Serialise an ordered frame stack to a JSON-safe list of dicts.

    Order is preserved (root-first). Each frame is rendered by its own
    ``to_jsonable`` which stamps the ``kind`` discriminator.
    """
    return [f.to_jsonable() for f in frames]


def frames_from_jsonable(blobs: list[dict[str, Any]]) -> list[Frame]:
    """Reconstruct an ordered frame stack from its JSON-able form.

    Dispatches on each blob's ``kind`` discriminator. Order is preserved.
    Raises ``ValueError`` on an unknown ``kind`` rather than silently
    dropping a frame (a corrupt/partial resume is better surfaced loudly).
    """
    out: list[Frame] = []
    for blob in blobs:
        kind = blob.get("kind")
        if kind == "agent":
            out.append(AgentFrame.from_jsonable(blob))
        elif kind == "graph":
            out.append(GraphFrame.from_jsonable(blob))
        else:
            raise ValueError(f"unknown frame kind {kind!r}")
    return out


# ===========================================================================
# Leaf application
# ===========================================================================


async def apply_leaf(
    inner_frame: Frame,
    leaf: Any,
    payload: Any,
    services: Any,
) -> "FrameOutcome":
    """Apply a resume ``payload`` to the leaf yield inside ``inner_frame``.

    Resolves the leaf - the innermost yielding tool or approval gate - with
    ``payload`` and returns either:

    * a :class:`ToolResultPart` carrying the resolved tool result (which the
      resume walk threads back into ``inner_frame`` as the result for its
      pending tool call), or
    * a :class:`Reparked` when an *approved* tool re-dispatch itself raises a
      fresh :class:`YieldToWorker` (the leaf becomes a new park to resolve).

    Two leaf shapes are handled, mirroring the flat-session resume paths in
    :mod:`primer.worker.yield_runtime` (``_resume_tool_approval``) and
    :mod:`primer.worker.pool` (the yielding-tool hook dispatch):

    ``_approval`` leaf
        Classify ``payload`` via :func:`classify_approval_payload`. On
        ``approved`` rebuild the original :class:`ToolCallPart` from
        ``leaf.resume_metadata["original_call"]`` and re-dispatch it through
        the innermost host's tool manager with ``bypass_approval=True``; if
        that raises :class:`YieldToWorker` return a :class:`Reparked`. On any
        non-approved decision synthesise the fail-closed error result.

    yielding-tool leaf (``ask_user`` / ``sleep`` / ``watch_files`` /
    ``subscribe_to_trigger``)
        Look up the tool's resume hook by name, run it (awaiting if it returns
        a coroutine), and wrap its output in a :class:`ToolResultPart` keyed by
        the innermost frame's pending ``tool_call_id``.
    """
    if leaf.tool_name == "_approval":
        original_raw = (leaf.resume_metadata or {}).get("original_call") or {}
        original_call = ToolCallPart(
            id=original_raw.get("id", "unknown"),
            name=original_raw.get("name", "unknown"),
            arguments=original_raw.get("arguments") or {},
        )
        decision, reason = classify_approval_payload(payload)
        if decision == "approved":
            tool_manager = await services.build_subagent_toolmanager(
                inner_frame.context
            )
            try:
                return await tool_manager.execute(
                    original_call, bypass_approval=True
                )
            except YieldToWorker as yld:
                return Reparked(new_yield=yld)
        return ToolResultPart(
            id=original_call.id,
            output=json.dumps({
                "rejected": True,
                "reason": reason or "(no reason supplied)",
                "tool_name": original_call.name,
                "arguments": original_call.arguments,
            }),
            error=True,
        )

    hook = get_resume_hook(leaf.tool_name)
    hook_result = hook(leaf.resume_metadata, payload)
    if asyncio.iscoroutine(hook_result):
        hook_result = await hook_result
    return ToolResultPart(
        id=inner_frame.tool_call_id,
        output=hook_result.output,
        error=hook_result.is_error,
    )


__all__ = [
    "AgentFrame",
    "AgentResumeContext",
    "Completed",
    "Frame",
    "FrameOutcome",
    "GraphFrame",
    "Reparked",
    "apply_leaf",
    "frames_from_jsonable",
    "frames_to_jsonable",
]
