"""Storage-backed graph executor (standalone, non-workspace).

Persists the graph-level :class:`GraphThread` row plus per-node
:class:`GraphNodeMessage` rows. Each node's invocation builds an
in-memory message list from prior persisted rows + the freshly
rendered input, calls the LLM directly via the agent's bound
:class:`LLM`, and persists the resulting messages back.

Threads survive process restart; re-instantiating the executor
against the same ``graph_thread_id`` resumes the graph from its
last persisted state (currently a fresh execution; full resumption
is a follow-up).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from matrix.agent.tool_manager import ToolExecutionManager
from matrix.graph.base import _BaseGraphExecutor
from matrix.graph.router import RouterRegistry
from matrix.model.chat import Message
from matrix.model.except_ import NotFoundError
from matrix.model.graph import (
    Graph,
    GraphNodeMessage,
    GraphThread,
    NodeRuntimeState,
    _GraphNodeRef,
)
from matrix.model.session import SessionStatus
from matrix.model.storage import (
    CursorPage,
    FieldRef,
    Op,
    OrderBy,
    PageRequest,
    Predicate,
    Value,
)


if TYPE_CHECKING:
    from matrix.int.llm import LLM
    from matrix.int.storage import Storage
    from matrix.model.agent import Agent
    from matrix.model.provider import LLMModel


logger = logging.getLogger(__name__)


class GraphExecutor(_BaseGraphExecutor):
    """Storage-backed graph executor."""

    def __init__(
        self,
        *,
        graph: Graph,
        agent_resolver: Callable[[str], Awaitable["Agent"]],
        llm_resolver: Callable[["Agent"], Awaitable[tuple["LLM", "LLMModel"]]],
        thread_storage: "Storage[GraphThread]",
        message_storage: "Storage[GraphNodeMessage]",
        graph_thread_id: str,
        router_registry: RouterRegistry | None = None,
        tool_manager_resolver: Callable[
            ["Agent"], Awaitable[ToolExecutionManager]
        ] | None = None,
        graph_resolver: Callable[[str], Awaitable[Graph]] | None = None,
        principal: str | None = None,
    ) -> None:
        super().__init__(
            graph=graph,
            agent_resolver=agent_resolver,
            llm_resolver=llm_resolver,
            tool_manager_resolver=tool_manager_resolver,
            graph_resolver=graph_resolver,
            router_registry=router_registry,
            principal=principal,
        )
        self._thread_id = graph_thread_id
        self._threads = thread_storage
        self._messages = message_storage

    @property
    def thread_id(self) -> str:
        return self._thread_id

    # ---- Thread management (static helpers) -----------------------------

    @staticmethod
    async def open_thread(
        *,
        graph: Graph,
        thread_storage: "Storage[GraphThread]",
        title: str | None = None,
    ) -> GraphThread:
        """Open a new graph thread and persist it."""
        now = datetime.now(timezone.utc)
        thread = GraphThread(
            id=f"gt-{uuid.uuid4().hex[:16]}",
            graph_id=graph.id,
            title=title,
            created_at=now,
            last_activity_at=now,
        )
        return await thread_storage.create(thread)

    @staticmethod
    async def delete_thread(
        graph_thread_id: str,
        *,
        thread_storage: "Storage[GraphThread]",
        message_storage: "Storage[GraphNodeMessage]",
    ) -> None:
        """Delete a graph thread + every per-node message under it.

        Idempotent at the thread level -- :class:`NotFoundError`
        from a missing row is swallowed.
        """
        cursor: str | None = None
        while True:
            page = await message_storage.find(
                Predicate(
                    left=FieldRef(name="graph_thread_id"),
                    op=Op.EQ,
                    right=Value(value=graph_thread_id),
                ),
                CursorPage(cursor=cursor, length=200),
                order_by=[OrderBy(field="sequence", direction="asc")],
            )
            for row in page.items:
                try:
                    await message_storage.delete(row.id)
                except NotFoundError:
                    pass
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        try:
            await thread_storage.delete(graph_thread_id)
        except NotFoundError:
            pass

    @staticmethod
    async def list_threads(
        *,
        thread_storage: "Storage[GraphThread]",
        page: PageRequest,
        graph_id: str | None = None,
    ):
        """Page through graph threads, optionally filtered by ``graph_id``."""
        order_by = [OrderBy(field="last_activity_at", direction="desc")]
        if graph_id is None:
            return await thread_storage.list(page, order_by=order_by)
        return await thread_storage.find(
            Predicate(
                left=FieldRef(name="graph_id"),
                op=Op.EQ,
                right=Value(value=graph_id),
            ),
            page,
            order_by=order_by,
        )

    # ---- Subclass hooks --------------------------------------------------

    async def _load_node_history(self, node_id: str) -> list[Message]:
        out: list[Message] = []
        for row in await self._load_node_messages_full(node_id):
            out.append(Message(role=row.role, parts=row.parts))
        return out

    async def _persist_node_turn(
        self,
        node_id: str,
        iteration: int,
        new_messages: list[Message],
    ) -> None:
        next_seq = await self._next_sequence(node_id)
        now = datetime.now(timezone.utc)
        for i, msg in enumerate(new_messages):
            await self._messages.create(
                GraphNodeMessage(
                    id=f"gnm-{uuid.uuid4().hex[:16]}",
                    graph_thread_id=self._thread_id,
                    node_id=node_id,
                    role=msg.role,
                    parts=msg.parts,
                    created_at=now,
                    iteration=iteration,
                    sequence=next_seq + i,
                )
            )

    async def _save_state(
        self,
        *,
        iteration: int,
        node_states: dict[str, NodeRuntimeState],
        status: SessionStatus,
        ended_reason: str | None = None,
    ) -> None:
        thread = await self._threads.get(self._thread_id)
        if thread is None:
            return  # silently noop if the thread was deleted out-from-under us
        updated = thread.model_copy(
            update={
                "iteration": iteration,
                "node_states": dict(node_states),
                "status": status,
                "ended_reason": ended_reason,
                "last_activity_at": datetime.now(timezone.utc),
            }
        )
        await self._threads.update(updated)

    # ---- Internals -------------------------------------------------------

    async def _next_sequence(self, node_id: str) -> int:
        existing = await self._load_node_messages_full(node_id)
        if not existing:
            return 0
        return max(r.sequence for r in existing) + 1

    async def _build_sub_executor(
        self,
        parent_node: _GraphNodeRef,
        sub_graph: Graph,
    ) -> "GraphExecutor":
        """Open a fresh nested thread and build a child :class:`GraphExecutor`.

        The sub-thread reuses the same storage handles as the parent so
        sub-graph messages live in the same backend; the
        ``parent_session_id``-style scoping isn't modelled at the
        :class:`GraphThread` row level today, but the row's ``title``
        records the parent thread + node so callers can correlate.
        """
        sub_thread = await GraphExecutor.open_thread(
            graph=sub_graph,
            thread_storage=self._threads,
            title=f"sub:{self._thread_id}/{parent_node.id}",
        )
        return GraphExecutor(
            graph=sub_graph,
            agent_resolver=self._agent_resolver,
            llm_resolver=self._llm_resolver,
            thread_storage=self._threads,
            message_storage=self._messages,
            graph_thread_id=sub_thread.id,
            router_registry=self._router_registry,
            tool_manager_resolver=self._tool_manager_resolver,
            graph_resolver=self._graph_resolver,
            principal=self._principal,
        )

    async def _load_node_messages_full(
        self,
        node_id: str,
    ) -> list[GraphNodeMessage]:
        out: list[GraphNodeMessage] = []
        cursor: str | None = None
        while True:
            page = await self._messages.find(
                Predicate(
                    left=Predicate(
                        left=FieldRef(name="graph_thread_id"),
                        op=Op.EQ,
                        right=Value(value=self._thread_id),
                    ),
                    op=Op.AND,
                    right=Predicate(
                        left=FieldRef(name="node_id"),
                        op=Op.EQ,
                        right=Value(value=node_id),
                    ),
                ),
                CursorPage(cursor=cursor, length=1000),
                order_by=[
                    OrderBy(field="iteration", direction="asc"),
                    OrderBy(field="sequence", direction="asc"),
                ],
            )
            out.extend(page.items)
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        return out


__all__ = ["GraphExecutor"]
