"""Workspace-backed graph executor.

State persistence is git-versioned through the workspace's
:class:`matrix.workspace.state.StateRepo`. Every turn end (each
superstep + the final ENDED transition) commits the updated graph
state to the workspace's ``.state/`` repo via
:meth:`StateRepo.commit_arbitrary`. Callers can inspect the history
with standard git tooling::

    git -C .state log -- graphs/<gsid>/
    git -C .state log --grep='X-Matrix-Graph: <gsid>'

Per-node message histories live at::

    .state/graphs/<gsid>/nodes/<node_id>/messages.jsonl

Graph-level state at::

    .state/graphs/<gsid>/state.json

If a :class:`AgentSession` is supplied via ``workspace_session``,
EVERY agent in the graph is augmented before invocation:

* the session's :attr:`AgentSession.system_prompt_fragment` is
  appended to the agent's ``system_prompt`` (so the LLM sees
  workspace-tool documentation),
* the session's workspace tools are composed into each per-node
  :class:`ToolExecutionManager` (so the LLM can actually call
  ``read``, ``write``, ``exec``, etc).

When ``workspace_session`` is omitted the executor still works as
a plain git-backed graph runner without workspace integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from matrix.agent.tool_manager import ToolExecutionManager
from matrix.graph.base import _BaseGraphExecutor
from matrix.graph.router import RouterRegistry
from matrix.model.chat import Message
from matrix.model.graph import Graph, NodeRuntimeState, _GraphNodeRef
from matrix.model.workspace_session import SessionStatus


if TYPE_CHECKING:
    from matrix.int.llm import LLM
    from matrix.model.agent import Agent
    from matrix.model.provider import LLMModel
    from matrix.workspace.session import AgentSession
    from matrix.workspace.local.state import LocalStateRepo


logger = logging.getLogger(__name__)


# Trailer prefix used for graph-state commits so callers can grep for
# "this graph's history" via ``git log --grep='X-Matrix-Graph: <gsid>'``.
_TRAILER_GRAPH = "X-Matrix-Graph"
_TRAILER_OP = "X-Matrix-Op"


class WorkspaceGraphExecutor(_BaseGraphExecutor):
    """Workspace-backed graph executor with git-versioned state.

    Per-node message histories AND graph-level state are persisted
    under ``<state_repo.path>/graphs/<graph_session_id>/`` and
    committed to the state repo on every superstep boundary.

    Optional workspace augmentation: pass ``workspace_session`` to
    have every agent in the graph receive the session's
    ``system_prompt_fragment`` + workspace tools.
    """

    def __init__(
        self,
        *,
        graph: Graph,
        agent_resolver: Callable[[str], Awaitable["Agent"]],
        llm_resolver: Callable[["Agent"], Awaitable[tuple["LLM", "LLMModel"]]],
        state_repo: "LocalStateRepo",
        graph_session_id: str,
        workspace_session: "AgentSession | None" = None,
        tool_manager_resolver: Callable[
            ["Agent"], Awaitable[ToolExecutionManager]
        ] | None = None,
        graph_resolver: Callable[[str], Awaitable[Graph]] | None = None,
        router_registry: RouterRegistry | None = None,
        principal: str | None = None,
    ) -> None:
        wrapped_agent_resolver = self._wrap_agent_resolver(
            agent_resolver, workspace_session
        )
        wrapped_tool_manager_resolver = self._wrap_tool_manager_resolver(
            tool_manager_resolver, workspace_session
        )
        super().__init__(
            graph=graph,
            agent_resolver=wrapped_agent_resolver,
            llm_resolver=llm_resolver,
            tool_manager_resolver=wrapped_tool_manager_resolver,
            graph_resolver=graph_resolver,
            router_registry=router_registry,
            principal=principal,
        )
        self._state_repo = state_repo
        self._graph_session_id = graph_session_id
        self._workspace_session = workspace_session
        # Cache the original (unwrapped) resolvers so subgraph children
        # can re-wrap them with their own context if needed.
        self._raw_agent_resolver = agent_resolver
        self._raw_tool_manager_resolver = tool_manager_resolver

    # ---- Public properties ----------------------------------------------

    @property
    def state_repo(self) -> "LocalStateRepo":
        return self._state_repo

    @property
    def graph_session_id(self) -> str:
        return self._graph_session_id

    @property
    def state_root(self) -> Path:
        """Absolute path to the per-graph state subtree.

        Read-only convenience: ``<state_repo.path>/graphs/<gsid>``.
        Tests use this to assert on persisted files.
        """
        return self._state_repo.path / "graphs" / self._graph_session_id

    # ---- Augmentation wrappers -------------------------------------------

    @staticmethod
    def _wrap_agent_resolver(
        base: Callable[[str], Awaitable["Agent"]],
        workspace_session: "AgentSession | None",
    ) -> Callable[[str], Awaitable["Agent"]]:
        """Append the workspace's ``system_prompt_fragment`` to every agent."""
        if workspace_session is None:
            return base

        async def _resolve(agent_id: str) -> "Agent":
            from matrix.model.agent import Agent as _Agent

            agent = await base(agent_id)
            composite_system_prompt = list(agent.system_prompt) + [
                workspace_session.system_prompt_fragment
            ]
            return _Agent(
                id=agent.id,
                description=agent.description,
                model=agent.model,
                temperature=agent.temperature,
                tools=list(agent.tools),
                system_prompt=composite_system_prompt,
                compaction_prompt=list(agent.compaction_prompt),
            )

        return _resolve

    @staticmethod
    def _wrap_tool_manager_resolver(
        base: Callable[["Agent"], Awaitable[ToolExecutionManager]] | None,
        workspace_session: "AgentSession | None",
    ) -> Callable[["Agent"], Awaitable[ToolExecutionManager]] | None:
        """Compose the workspace's tools onto every per-node tool manager."""
        if workspace_session is None:
            return base

        async def _resolve(agent: "Agent") -> ToolExecutionManager:
            providers = {}
            if base is not None:
                outer = await base(agent)
                providers = dict(outer.toolset_providers)
            return ToolExecutionManager.for_workspace(
                toolset_providers=providers,
                session=workspace_session,
            )

        return _resolve

    # ---- Subclass hooks --------------------------------------------------

    async def _load_node_history(self, node_id: str) -> list[Message]:
        path = self._messages_path(node_id)

        def _read() -> list[Message]:
            if not path.exists():
                return []
            out: list[Message] = []
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    out.append(Message.model_validate_json(line))
            return out

        return await asyncio.to_thread(_read)

    async def _persist_node_turn(
        self,
        node_id: str,
        iteration: int,
        new_messages: list[Message],
    ) -> None:
        """Append messages to the node's jsonl AND git-commit the change.

        Each turn becomes one commit so callers can grep history per
        node via ``git log -- graphs/<gsid>/nodes/<node_id>/``.
        """
        path = self._messages_path(node_id)
        rel_path = self._repo_rel(path)

        def _read_existing() -> str:
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8")

        existing = await asyncio.to_thread(_read_existing)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        appended = (
            existing
            + "\n".join(m.model_dump_json() for m in new_messages)
            + "\n"
        )

        await self._state_repo.commit_arbitrary(
            summary=(
                f"graph {self._graph_session_id}: node {node_id} turn "
                f"#{iteration}"
            ),
            files={rel_path: appended},
            trailers={
                _TRAILER_GRAPH: self._graph_session_id,
                _TRAILER_OP: "node_turn",
                "X-Matrix-Graph-Node": node_id,
                "X-Matrix-Graph-Iteration": str(iteration),
            },
        )

    async def _save_state(
        self,
        *,
        iteration: int,
        node_states: dict[str, NodeRuntimeState],
        status: SessionStatus,
        ended_reason: str | None = None,
    ) -> None:
        """Write graph-level state.json AND git-commit it."""
        rel_state = self._repo_rel(self.state_root / "state.json")
        payload = {
            "iteration": iteration,
            "status": status.value,
            "ended_reason": ended_reason,
            "node_states": {
                nid: {
                    "status": ns.status.value,
                    "last_run_iteration": ns.last_run_iteration,
                    "last_run_at": (
                        ns.last_run_at.isoformat() if ns.last_run_at else None
                    ),
                    "error": ns.error,
                }
                for nid, ns in node_states.items()
            },
        }
        body = json.dumps(payload, indent=2)
        trailers = {
            _TRAILER_GRAPH: self._graph_session_id,
            _TRAILER_OP: "state",
            "X-Matrix-Graph-Status": status.value,
        }
        if ended_reason:
            trailers["X-Matrix-Graph-Ended-Reason"] = ended_reason
        await self._state_repo.commit_arbitrary(
            summary=(
                f"graph {self._graph_session_id}: state @ iter {iteration} "
                f"({status.value})"
            ),
            files={rel_state: body},
            trailers=trailers,
        )

    async def _build_sub_executor(
        self,
        parent_node: _GraphNodeRef,
        sub_graph: Graph,
    ) -> "WorkspaceGraphExecutor":
        """Build a child executor with its own state subtree.

        Child uses ``<parent_gsid>__<parent_node_id>`` as its
        graph_session_id so its files live at
        ``graphs/<parent_gsid>__<node_id>/`` and commits stay
        attributable. ``::`` is avoided in the path because Windows
        treats colons specially in NTFS streams.
        """
        sub_gsid = f"{self._graph_session_id}__{parent_node.id}"
        return WorkspaceGraphExecutor(
            graph=sub_graph,
            agent_resolver=self._raw_agent_resolver,
            llm_resolver=self._llm_resolver,
            state_repo=self._state_repo,
            graph_session_id=sub_gsid,
            workspace_session=self._workspace_session,
            tool_manager_resolver=self._raw_tool_manager_resolver,
            graph_resolver=self._graph_resolver,
            router_registry=self._router_registry,
            principal=self._principal,
        )

    # ---- Public helpers --------------------------------------------------

    async def load_state(self) -> dict | None:
        """Return the persisted ``state.json`` payload, or ``None`` if absent."""
        path = self.state_root / "state.json"

        def _read() -> dict | None:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

        return await asyncio.to_thread(_read)

    async def write_graph_binding(self) -> None:
        """Snapshot the graph definition under ``<state_root>/graph.json``.

        Committed in the same way as state writes so the graph
        topology that drove an execution is recoverable from history.
        """
        rel = self._repo_rel(self.state_root / "graph.json")
        body = self._graph.model_dump_json(indent=2)
        await self._state_repo.commit_arbitrary(
            summary=(
                f"graph {self._graph_session_id}: bind graph {self._graph.id!r}"
            ),
            files={rel: body},
            trailers={
                _TRAILER_GRAPH: self._graph_session_id,
                _TRAILER_OP: "bind",
            },
        )

    # ---- Internals -------------------------------------------------------

    def _messages_path(self, node_id: str) -> Path:
        return self.state_root / "nodes" / node_id / "messages.jsonl"

    def _repo_rel(self, p: Path) -> str:
        """Return ``p`` as a forward-slash path relative to the state repo root."""
        return p.relative_to(self._state_repo.path).as_posix()


__all__ = ["WorkspaceGraphExecutor"]
