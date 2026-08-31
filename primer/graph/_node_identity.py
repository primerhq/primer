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


def set_current_graph_node_id(node_id: str | None) -> contextvars.Token:
    """Publish the fan-out-instance-qualified node id for the current task."""
    return _NODE_ID.set(node_id)


def reset_current_graph_node_id(token: contextvars.Token) -> None:
    _NODE_ID.reset(token)


def current_graph_node_id() -> str | None:
    """The ambient graph node instance id, if one is active on this task."""
    return _NODE_ID.get()


__all__ = [
    "current_graph_node_id",
    "reset_current_graph_node_id",
    "set_current_graph_node_id",
]
