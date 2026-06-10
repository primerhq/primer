"""Workspace-backed graph executor.

State persistence is git-versioned through the workspace's
:class:`primer.workspace.state.StateRepo`. Every turn end (each
superstep + the final ENDED transition) commits the updated graph
state to the workspace's ``.state/`` repo via
:meth:`StateRepo.commit_arbitrary`. Callers can inspect the history
with standard git tooling::

    git -C .state log -- graphs/<gsid>/
    git -C .state log --grep='X-Primer-Graph: <gsid>'

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
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.base import _BaseGraphExecutor
from primer.graph.router import RouterRegistry
from primer.model.chat import Message, StreamEvent, ToolResultPart
from primer.model.graph import Graph, NodeRuntimeState, _GraphNodeRef, _ToolCallNode
from primer.model.workspace_session import SessionStatus
from primer.observability.turn_log_writer import (
    TurnLogWriter,
    WorkspaceTurnLogWriter,
)


if TYPE_CHECKING:
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.provider import LLMModel
    from primer.workspace.session import AgentSession
    from primer.workspace.local.state import LocalStateRepo


logger = logging.getLogger(__name__)


# Trailer prefix used for graph-state commits so callers can grep for
# "this graph's history" via ``git log --grep='X-Primer-Graph: <gsid>'``.
_TRAILER_GRAPH = "X-Primer-Graph"
_TRAILER_OP = "X-Primer-Op"


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
        graph_input: Any = None,
        tool_manager: ToolExecutionManager | None = None,
        owns_session_lifecycle: bool = False,
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
        # Only the top-level (worker-built) executor owns the on-disk
        # session holder's lifecycle. Subgraph child executors share the
        # parent's holder (see ``_build_sub_executor``) and must NOT end
        # it when their own run terminates, or they would kill the holder
        # out from under the still-running parent graph.
        self._owns_session_lifecycle = owns_session_lifecycle

        # Turn-log writers: bypass the git-backed state_repo.commit
        # path and write directly to .state/graphs/<gsid>/turns.jsonl
        # (graph-level) and .state/graphs/<gsid>/nodes/<nid>/turns.jsonl
        # (per-node). Turn logs are observability data — high write rate,
        # no audit-trail value, so they live outside the git history.
        #
        # LocalStateRepo exposes a ``path`` attribute (a ``pathlib.Path``
        # pointing at the on-host .state directory); the writers below do
        # direct file I/O through it. SandboxStateRepo (container/k8s)
        # only exposes ``state_path`` (a remote path string) — direct
        # host-side file I/O is not applicable. For sandbox backends we
        # fall back to NoopTurnLogWriter; the functional state (commits,
        # session files) still lands correctly via state_repo.commit.
        from primer.observability.turn_log_writer import NoopTurnLogWriter

        _local_path: Path | None = getattr(state_repo, "path", None)

        if _local_path is not None:
            state_root = _local_path / "graphs" / graph_session_id

            def _make_append_line(rel_path: Path):
                target = rel_path

                async def _append(line: bytes) -> None:
                    def _do() -> None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("ab") as fh:
                            fh.write(line)

                    await asyncio.to_thread(_do)

                return _append

            def _make_read_existing(rel_path: Path):
                target = rel_path

                async def _read() -> bytes:
                    def _do() -> bytes:
                        if not target.exists():
                            return b""
                        return target.read_bytes()

                    return await asyncio.to_thread(_do)

                return _read

            def _factory(node_id: str) -> TurnLogWriter:
                target = state_root / "nodes" / node_id / "turns.jsonl"
                return WorkspaceTurnLogWriter(
                    append_line=_make_append_line(target),
                    read_existing=_make_read_existing(target),
                )

            self._turn_log_factory = _factory
            graph_target = state_root / "turns.jsonl"
            self._graph_turn_log: TurnLogWriter = WorkspaceTurnLogWriter(
                append_line=_make_append_line(graph_target),
                read_existing=_make_read_existing(graph_target),
            )
        else:
            # Sandbox (container/k8s) backend: turn-log I/O is not wired
            # for the remote path. Use no-op writers; functional state
            # still commits correctly via state_repo.commit_arbitrary.
            self._turn_log_factory = lambda node_id: NoopTurnLogWriter()
            self._graph_turn_log = NoopTurnLogWriter()
        # Spec §4.3 — when set (typically by pool.py from
        # ``session.metadata['graph_input']``), this overrides the
        # ``messages`` list passed to :meth:`invoke` and becomes
        # :attr:`GraphContext.initial_input`. The Begin node materialises
        # its NodeOutput from this value (dict / str / list / Any).
        self._graph_input: Any = graph_input
        # Cache the original (unwrapped) resolvers so subgraph children
        # can re-wrap them with their own context if needed.
        self._raw_agent_resolver = agent_resolver
        self._raw_tool_manager_resolver = tool_manager_resolver
        # Spec B §2.3 — ToolCall nodes dispatch through this manager.
        # Production callers (pool.py) wire a workspace-bound manager
        # built from the session + provider registry; when absent we
        # fall back to a workspace-only manager constructed lazily from
        # ``workspace_session`` on first ToolCall.
        self._tool_manager: ToolExecutionManager | None = tool_manager

    # ---- Public properties ----------------------------------------------

    @property
    def state_repo(self) -> "LocalStateRepo":
        return self._state_repo

    @property
    def graph_session_id(self) -> str:
        return self._graph_session_id

    @property
    def state_root(self) -> Path | None:
        """Absolute path to the per-graph state subtree, or None for sandbox repos.

        Read-only convenience: ``<state_repo.path>/graphs/<gsid>`` for
        :class:`~primer.workspace.local.state.LocalStateRepo` backends.
        Returns ``None`` for :class:`~primer.workspace.sandbox.state.SandboxStateRepo`
        (container/k8s) backends where the state lives inside the container.
        Tests that only run against local workspaces may still assert on this.
        """
        local_path: Path | None = getattr(self._state_repo, "path", None)
        if local_path is None:
            return None
        return local_path / "graphs" / self._graph_session_id

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
            from primer.model.agent import Agent as _Agent

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

    # ---- Public invoke override ------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
    ) -> AsyncIterator[StreamEvent]:
        """Stream graph execution events.

        Spec §4.3 — when ``graph_input`` was supplied at construction
        time (typically by the worker reading
        ``session.metadata['graph_input']``), it seeds
        :attr:`GraphContext.initial_input` instead of ``messages``. The
        Begin node materialises its NodeOutput from that value;
        callers that pass a non-empty ``messages`` list when
        ``graph_input`` is also set get the metadata value (metadata
        wins — this is the documented precedence in spec §4.2:
        ``graph_input`` is preferred over ``initial_instructions``).
        """
        if self._graph_input is not None:
            seed: Any = self._graph_input
        else:
            seed = messages
        async for ev in super().invoke(seed):  # type: ignore[arg-type]
            yield ev

    # ---- Subclass hooks --------------------------------------------------

    async def _dispatch_toolcall(
        self,
        node: "_ToolCallNode",
        arguments: dict[str, Any],
        *,
        bypass_approval: bool = False,
    ) -> ToolResultPart:
        """Dispatch a ToolCall node via the workspace's ``ToolExecutionManager``.

        Spec B §2.3 step 2. Builds a :class:`ToolCallPart` with a fresh
        uuid id and forwards to :meth:`ToolExecutionManager.execute`.

        ``bypass_approval`` is threaded through to the manager so the
        resume path (Phase 6 Task 6.3) can re-dispatch a previously
        yielded ToolCall without re-firing the approval gate.

        ``self._tool_manager`` is the manager the worker passes in when
        building this executor. When absent, we build a workspace-only
        manager lazily from ``self._workspace_session`` so tests /
        callers that only need workspace tools don't have to construct
        one upfront. When neither is available the call raises so the
        ToolCall fails with ``tool_execution_failed`` rather than
        hanging.
        """
        import uuid
        from primer.model.chat import ToolCallPart

        manager = self._tool_manager
        if manager is None:
            if self._workspace_session is None:
                raise RuntimeError(
                    f"WorkspaceGraphExecutor has neither a tool_manager "
                    f"nor a workspace_session wired; cannot invoke tool "
                    f"{node.tool_id!r}"
                )
            manager = ToolExecutionManager.for_workspace(
                toolset_providers={},
                session=self._workspace_session,
            )
            self._tool_manager = manager

        call = ToolCallPart(
            id=str(uuid.uuid4()),
            name=node.tool_id,
            arguments=arguments,
        )
        return await manager.execute(
            call,
            principal=self._principal,
            bypass_approval=bypass_approval,
        )

    async def _dispatch_toolcall_with_bypass(
        self,
        node: "_ToolCallNode",
        arguments: dict[str, Any],
    ) -> ToolResultPart:
        """Resume-path dispatch with ``bypass_approval=True``.

        Spec B §2.3 step 3 / Phase 6 Task 6.3. After operator approval,
        the resume path drains pending ToolCalls by routing through this
        hook so the underlying :class:`ToolExecutionManager` skips its
        approval gate and runs the tool directly.
        """
        return await self._dispatch_toolcall(
            node, arguments, bypass_approval=True,
        )

    async def _load_node_history(self, node_id: str) -> list[Message]:
        rel_path = self._state_rel(f"nodes/{node_id}/messages.jsonl")
        data = await self._state_repo.read_state_file(rel_path)
        if data is None:
            return []
        out: list[Message] = []
        for line in data.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(Message.model_validate_json(line))
        return out

    async def _persist_node_turn(
        self,
        node_id: str,
        iteration: int,
        new_messages: list[Message],
    ) -> None:
        """Append messages to the node's jsonl AND git-commit the change.

        Each turn becomes one commit so callers can grep history per
        node via ``git log -- graphs/<gsid>/nodes/<node_id>/``.

        Uses :meth:`StateRepo.read_state_file` to read the existing content
        so this works on both local and sandbox (container/k8s) backends.
        """
        rel_path = self._state_rel(f"nodes/{node_id}/messages.jsonl")

        raw_existing = await self._state_repo.read_state_file(rel_path)
        existing = raw_existing.decode("utf-8") if raw_existing else ""
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
                "X-Primer-Graph-Node": node_id,
                "X-Primer-Graph-Iteration": str(iteration),
            },
        )

    async def _save_state(
        self,
        *,
        iteration: int,
        node_states: dict[str, NodeRuntimeState],
        status: SessionStatus,
        ended_reason: str | None = None,
        ended_detail: str | None = None,
    ) -> None:
        """Write graph-level state.json AND git-commit it."""
        rel_state = self._state_rel("state.json")
        payload = {
            "iteration": iteration,
            "status": status.value,
            "ended_reason": ended_reason,
            "ended_detail": ended_detail,
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
            "X-Primer-Graph-Status": status.value,
        }
        if ended_reason:
            trailers["X-Primer-Graph-Ended-Reason"] = ended_reason
        if ended_detail:
            trailers["X-Primer-Graph-Ended-Detail"] = ended_detail
        await self._state_repo.commit_arbitrary(
            summary=(
                f"graph {self._graph_session_id}: state @ iter {iteration} "
                f"({status.value})"
            ),
            files={rel_state: body},
            trailers=trailers,
        )

        # Mirror the agent executor: when the graph reaches its terminal
        # ENDED save, transition the on-disk session holder slot to ENDED
        # too. Without this the workspace-level session views
        # (get/list_workspace_session, which read the holder) report a
        # finished graph as perpetually "running". Only the owning
        # (top-level) executor does this; subgraph children share the
        # holder and must leave it to the parent. Parks save with WAITING
        # (not ENDED), so a parked graph's holder is left intact for
        # resume. Best-effort: a holder write failure must not crash the
        # graph's terminal commit.
        if (
            status == SessionStatus.ENDED
            and self._owns_session_lifecycle
            and self._workspace_session is not None
        ):
            holder_reason = "completed" if ended_reason == "completed" else "failed"
            try:
                await self._workspace_session.set_status(
                    SessionStatus.ENDED,
                    ended_reason=holder_reason,
                )
            except Exception:  # noqa: BLE001 -- best-effort holder sync
                logger.warning(
                    "WorkspaceGraphExecutor: failed to end holder session "
                    "%r on graph terminal (reason=%r)",
                    getattr(self._workspace_session, "session_id", "?"),
                    holder_reason,
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
        rel_path = self._state_rel("state.json")
        data = await self._state_repo.read_state_file(rel_path)
        if data is None:
            return None
        return json.loads(data.decode("utf-8"))

    async def write_graph_binding(self) -> None:
        """Snapshot the graph definition under ``<state_root>/graph.json``.

        Committed in the same way as state writes so the graph
        topology that drove an execution is recoverable from history.
        """
        rel = self._state_rel("graph.json")
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

    def _messages_path(self, node_id: str) -> Path | None:
        """Return the host-FS path for a node's messages.jsonl, or None.

        Returns ``None`` for sandbox (container/k8s) backends where state
        lives inside the container, not on the host filesystem.
        """
        root = self.state_root
        if root is None:
            return None
        return root / "nodes" / node_id / "messages.jsonl"

    def _state_rel(self, filename: str) -> str:
        """Return a state-repo-relative path for ``filename`` under the graph dir.

        Always returns ``graphs/<graph_session_id>/<filename>`` regardless of
        the backend type. Works for both :class:`~primer.workspace.local.state.
        LocalStateRepo` (host-FS) and :class:`~primer.workspace.sandbox.state.
        SandboxStateRepo` (container/k8s).
        """
        return f"graphs/{self._graph_session_id}/{filename}"


__all__ = ["WorkspaceGraphExecutor"]
