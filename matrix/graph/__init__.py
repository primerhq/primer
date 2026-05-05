"""Agent-graph runtime: declarative graphs of agent nodes.

Public surface added by sub-project G1 (Foundation):

* :class:`RouterRegistry` -- registers callable routers keyed by
  ``callable_id`` (mirrors :class:`matrix.agent.ToolExecutionManager`).
* :func:`match_json_path` -- evaluates a :class:`JsonPathBranch`
  against a parsed structured output dict.
* :func:`render_input_template` -- Jinja2 sandboxed renderer for a
  node's ``input_template`` against a :class:`GraphContext`.

G2/G3 add :class:`_BaseGraphExecutor`, :class:`GraphExecutor`,
:class:`WorkspaceGraphExecutor`, and the :class:`GraphThread` /
:class:`GraphNodeMessage` storage models.

See ``docs/superpowers/specs/2026-05-03-agent-graph-design.md``.
"""

from matrix.graph.base import _BaseGraphExecutor
from matrix.graph.executor import GraphExecutor
from matrix.graph.router import RouterRegistry, match_json_path
from matrix.graph.template import render_input_template
from matrix.graph.workspace_executor import WorkspaceGraphExecutor
from matrix.model.graph import (
    Graph,
    GraphContext,
    GraphEdge,
    GraphNode,
    GraphNodeMessage,
    GraphRouter,
    GraphThread,
    JsonPathBranch,
    NodeOutput,
    NodeRuntimeState,
    NodeRuntimeStatus,
)


__all__ = [
    "Graph",
    "GraphContext",
    "GraphEdge",
    "GraphExecutor",
    "GraphNode",
    "GraphNodeMessage",
    "GraphRouter",
    "GraphThread",
    "JsonPathBranch",
    "NodeOutput",
    "NodeRuntimeState",
    "NodeRuntimeStatus",
    "RouterRegistry",
    "WorkspaceGraphExecutor",
    "_BaseGraphExecutor",
    "match_json_path",
    "render_input_template",
]
