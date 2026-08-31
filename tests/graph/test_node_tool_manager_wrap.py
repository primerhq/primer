"""Graph node tool managers must preserve the agent's security state.

Observed live: a graph agent node was offered 138 tools where the same
agent in a session got 18 (every tool in every granted toolset, including
50 mutating tools the agent was never granted), and approval gates that
park a session write went straight through in a node. Both defects share
one cause: _wrap_tool_manager_resolver rebuilt the per-node manager from
scratch, keeping only toolset_providers and dropping the allowlist, the
approval resolver and the provider registry on the floor.
"""
from __future__ import annotations

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.workspace_executor import WorkspaceGraphExecutor
from primer.model.principal import PrincipalRef


class _Session:
    """The one attribute for_workspace reads."""

    workspace_tools: list = []


class _Resolver:
    """Stands in for the approval resolver; identity is all that matters."""


async def _base_resolver(_agent) -> ToolExecutionManager:
    return ToolExecutionManager.for_workspace(
        toolset_providers={"collections": object()},
        session=_Session(),
        approval_resolver=_Resolver(),
        provider_registry={"registry": True},
        tools=["collections__search", "web__web_fetch"],
        initiated_by=PrincipalRef.system(),
    )


async def test_wrapped_node_manager_preserves_the_security_state():
    wrapped = WorkspaceGraphExecutor._wrap_tool_manager_resolver(
        _base_resolver, _Session(), PrincipalRef.system(),
    )
    manager = await wrapped(object())

    # The allowlist is the boundary the node used to lose.
    assert manager._tools_allowlist == frozenset(
        {"collections__search", "web__web_fetch"}
    ), "the agent's tool allowlist must survive the node rebuild"

    # No resolver means the gate check short-circuits and no policy fires.
    assert isinstance(manager._approval_resolver, _Resolver), (
        "the approval resolver must survive, or gates never fire in nodes"
    )
    assert manager._provider_registry == {"registry": True}
    assert "collections" in manager.toolset_providers
