"""Ambient fan-out-instance-qualified node identity for the current task.

01a0518f: a graph node's dispatch (tool call approval gate, streamed
event tagging) happens many call-frames below the superstep loop that
actually knows WHICH fan-out instance is running (``"worker[0]"`` vs the
shared graph-definition id ``"worker"``) - threading that id as an
explicit parameter through every intermediate signature (``run_agent_turn``
-> the agent loop -> ``ToolExecutionManager.execute``, a resolver contract
several unrelated builders/test fakes also implement) would be a much
wider, riskier change than the actual bug warrants. A contextvar is the
established pattern in this codebase for exactly this shape of problem -
see :mod:`primer.session.delegation`'s ``_SINK`` (a turn-scoped recorder)
and :mod:`primer.agent.invoke`'s ``_DEPTH`` (nested-invocation depth) -
and it composes correctly with the same concurrency shape fan-out uses:
``asyncio.create_task`` copies the current context per task, so concurrent
sibling tasks never see each other's ``.set()`` calls.

Set once per node-turn (:func:`primer.graph._node_dispatch._BaseGraphExecutor._stream_node`
for the live/streaming path, and the graph executor's own resume loop for
the resume path), read by anything that needs to distinguish concurrent
fan-out siblings sharing a raw provider tool_call_id: the approval gate's
event_key (primer.agent.tool_manager), and the streamed-event node
tagging (``_wrap_event``, primer.graph._agent_node /
primer.graph._node_dispatch). ``None`` (the default, and every non-graph
call path) means "no ambient graph node identity" - every reader folds
that in as "use the base id / today's behaviour", so this is purely
additive.
"""

from __future__ import annotations

import contextvars

_NODE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "primer_graph_node_instance_id", default=None,
)


class _Entered:
    """What :func:`set_current_graph_node_id` changed, for :func:`reset_current_graph_node_id` to put back."""

    __slots__ = ("node", "toolcall")

    def __init__(self, node: contextvars.Token, toolcall: contextvars.Token) -> None:
        self.node = node
        self.toolcall = toolcall


def set_current_graph_node_id(node_id: str | None) -> _Entered:
    """Publish the fan-out-instance-qualified node id for the current task.

    Entering a graph node also HIDES the identity of a ToolCall node's dispatch that is in progress above it (01a11faa, review of #712): the innermost node is the one a delegated run belongs
    to, and an agent node of a graph that a ToolCall node's tool runs in-process (``invoke_graph``) is inside that dispatch, not the ToolCall node. Leaving the node brings it back.
    """
    return _Entered(_NODE_ID.set(node_id), _TOOLCALL.set(None))


def reset_current_graph_node_id(entered: _Entered) -> None:
    _TOOLCALL.reset(entered.toolcall)
    _NODE_ID.reset(entered.node)


def current_graph_node_id() -> str | None:
    """The ambient graph node instance id, if one is active on this task."""
    return _NODE_ID.get()


# 01a11faa: the same shape for a ToolCall NODE's dispatch, as a SEPARATE value from ``_NODE_ID`` on purpose. ``_NODE_ID`` is also folded into the approval gate's event_key
# (primer.agent.tool_manager), and the channel inbox rebuilds a ToolCall node's key WITHOUT a node scope (``tool_approval:<session>:<call id>``,
# primer.channel.inbox._matching_event_keys), so publishing it for this dispatch would break the approval of a gated ToolCall node. This one is read by two things only: the
# workspace executor's ``_dispatch_toolcall`` (the id of the call the node wrote its row under becomes the ``ToolCallPart``'s id) and the delegation recorder's stamp
# (``run_subagent`` takes ``delegate_node_id`` from it when no graph node identity is active), so a run the tool delegates to nests under the node's call.
_TOOLCALL: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "primer_graph_toolcall_node_call", default=None,
)


def set_current_toolcall(node_id: str, call_id: str) -> contextvars.Token:
    """Publish (fan-out-instance-qualified node id, tool call id) for a ToolCall node's dispatch on the current task."""
    return _TOOLCALL.set((node_id, call_id))


def reset_current_toolcall(token: contextvars.Token) -> None:
    _TOOLCALL.reset(token)


def current_toolcall_id() -> str | None:
    """The id of the call the ToolCall node being dispatched wrote its row under, if one is active on this task."""
    active = _TOOLCALL.get()
    return active[1] if active is not None else None


def current_toolcall_node_id() -> str | None:
    """The node instance id of the ToolCall node being dispatched, if one is active on this task."""
    active = _TOOLCALL.get()
    return active[0] if active is not None else None


__all__ = [
    "current_graph_node_id",
    "current_toolcall_id",
    "current_toolcall_node_id",
    "reset_current_graph_node_id",
    "reset_current_toolcall",
    "set_current_graph_node_id",
    "set_current_toolcall",
]
