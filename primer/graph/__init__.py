"""Agent-graph runtime: declarative graphs of agent nodes.

Public surface added by sub-project G1 (Foundation):

* :class:`RouterRegistry` -- registers callable routers keyed by
  ``callable_id`` (mirrors :class:`primer.agent.ToolExecutionManager`).
* :func:`first_matching_branch` -- evaluates a list of
  :class:`JsonPathBranch` against a parsed structured output dict
  using the operator-aware :func:`evaluate_branch_condition`.
* :func:`render_input_template` -- Jinja2 sandboxed renderer for a
  node's ``input_template`` against a :class:`GraphContext`.

G2/G3 add :class:`_BaseGraphExecutor`, :class:`GraphExecutor`,
:class:`WorkspaceGraphExecutor`, and the :class:`GraphThread` /
:class:`GraphNodeMessage` storage models.

See ``docs/superpowers/specs/2026-05-03-agent-graph-design.md``.
"""

from primer.graph.base import _BaseGraphExecutor
from primer.graph.executor import GraphExecutor
from primer.graph.router import (
    RouterRegistry,
    evaluate_branch_condition,
    first_matching_branch,
)
from primer.graph.template import render_input_template
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.graph import (
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
    "evaluate_branch_condition",
    "first_matching_branch",
    "render_input_template",
]
