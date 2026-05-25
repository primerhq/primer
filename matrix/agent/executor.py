"""Direct chat with an :class:`Agent` over a persistent :class:`Thread`.

The :class:`AgentExecutor` is the chat-on-thread executor. It persists
thread + message rows via the existing
:class:`matrix.int.Storage` interface (typically backed by Postgres in
production; any in-memory stand-in works for tests).

A thread is the unit of state. The same agent can run many concurrent
threads; each carries its own history. Threads are not bound to a
workspace -- for workspace-backed execution, use
:class:`matrix.agent.WorkspaceAgentExecutor` (sub-project F4).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from matrix.agent.base import _BaseAgentExecutor
from matrix.agent.compaction import CompactionStrategy
from matrix.agent.tool_manager import ToolExecutionManager
from matrix.model.chat import Message
from matrix.model.except_ import NotFoundError
from matrix.model.storage import (
    CursorPage,
    FieldRef,
    Op,
    OrderBy,
    PageRequest,
    Predicate,
    Value,
)
from matrix.model.thread import Thread, ThreadMessage


if TYPE_CHECKING:
    from matrix.int.llm import LLM
    from matrix.int.storage import Storage
    from matrix.model.agent import Agent
    from matrix.model.provider import LLMModel


logger = logging.getLogger(__name__)


class AgentExecutor(_BaseAgentExecutor):
    """Chat-on-thread executor.

    Constructed against a specific ``thread_id`` -- one executor
    instance drives one thread. Use the static :meth:`open_thread` /
    :meth:`delete_thread` / :meth:`list_threads` helpers to manage the
    thread set without instantiating an executor.
    """

    def __init__(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        llm_model: "LLMModel",
        tool_manager: ToolExecutionManager,
        thread_id: str,
        thread_storage: "Storage[Thread]",
        message_storage: "Storage[ThreadMessage]",
        compaction: CompactionStrategy | None = None,
        principal: str | None = None,
    ) -> None:
        super().__init__(
            agent=agent,
            llm=llm,
            llm_model=llm_model,
            tool_manager=tool_manager,
            compaction=compaction,
            principal=principal,
        )
        self._thread_id = thread_id
        self._threads = thread_storage
        self._messages = message_storage

    @property
    def thread_id(self) -> str:
        return self._thread_id

    # ---- Thread management (static helpers) -----------------------------

    @staticmethod
    async def open_thread(
        *,
        agent: "Agent",
        thread_storage: "Storage[Thread]",
        title: str | None = None,
    ) -> Thread:
        """Open a new thread for ``agent`` and persist it."""
        now = datetime.now(timezone.utc)
        thread = Thread(
            id=f"thread-{uuid.uuid4().hex[:16]}",
            agent_id=agent.id,
            title=title,
            created_at=now,
            last_activity_at=now,
        )
        return await thread_storage.create(thread)

    @staticmethod
    async def delete_thread(
        thread_id: str,
        *,
        thread_storage: "Storage[Thread]",
        message_storage: "Storage[ThreadMessage]",
    ) -> None:
        """Delete a thread and every message persisted under it.

        Idempotent at the thread level -- ``NotFoundError`` from a
        missing row is swallowed. A future revision can replace the
        per-row delete with a bulk-delete primitive once
        :class:`Storage` exposes one.
        """
        cursor: str | None = None
        while True:
            page = await message_storage.find(
                Predicate(
                    left=FieldRef(name="thread_id"),
                    op=Op.EQ,
                    right=Value(value=thread_id),
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
            await thread_storage.delete(thread_id)
        except NotFoundError:
            pass

    @staticmethod
    async def list_threads(
        *,
        thread_storage: "Storage[Thread]",
        page: PageRequest,
        agent_id: str | None = None,
    ):
        """Page through threads, optionally filtered by ``agent_id``.

        Returns whichever response shape matches ``page`` (offset or
        cursor) per the :class:`Storage` ABC contract.
        """
        if agent_id is None:
            return await thread_storage.list(
                page,
                order_by=[OrderBy(field="last_activity_at", direction="desc")],
            )
        return await thread_storage.find(
            Predicate(
                left=FieldRef(name="agent_id"),
                op=Op.EQ,
                right=Value(value=agent_id),
            ),
            page,
            order_by=[OrderBy(field="last_activity_at", direction="desc")],
        )

    # ---- Subclass hooks --------------------------------------------------

    async def _load_history(self) -> list[Message]:
        """Read every :class:`ThreadMessage` for the thread, in sequence order."""
        out: list[Message] = []
        cursor: str | None = None
        # CursorPage.length is bounded at 200 server-side (storage.py:265);
        # paginate at that cap. Loop continues until next_cursor is None.
        page_size = 200
        while True:
            page = await self._messages.find(
                Predicate(
                    left=FieldRef(name="thread_id"),
                    op=Op.EQ,
                    right=Value(value=self._thread_id),
                ),
                CursorPage(cursor=cursor, length=page_size),
                order_by=[OrderBy(field="sequence", direction="asc")],
            )
            for row in page.items:
                out.append(Message(role=row.role, parts=row.parts))
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        return out

    async def _persist_turn(self, turn_messages: list[Message]) -> None:
        """Append ``turn_messages`` to the thread; bump ``last_activity_at``."""
        next_seq = await self._next_sequence()
        now = datetime.now(timezone.utc)
        for i, msg in enumerate(turn_messages):
            await self._messages.create(
                ThreadMessage(
                    id=f"tmsg-{uuid.uuid4().hex[:16]}",
                    thread_id=self._thread_id,
                    role=msg.role,
                    parts=msg.parts,
                    created_at=now,
                    sequence=next_seq + i,
                )
            )
        thread = await self._threads.get(self._thread_id)
        if thread is not None:
            await self._threads.update(
                thread.model_copy(update={"last_activity_at": now}),
            )

    async def _replace_compacted_head(
        self,
        compacted: list[Message],
    ) -> None:
        """Rewrite the thread's persisted history to the compacted form.

        Strategy: load every existing :class:`ThreadMessage`, identify
        the rows that match the tail of ``compacted`` (by structural
        equality, walking from the end), delete the rest, insert one
        new summary row at sequence 0, resequence the surviving tail
        rows starting from 1.

        Not transactional in v1 -- if the process crashes mid-rewrite
        the thread is left in a half-rewritten state. Acceptable
        because ``_load_history`` reads in sequence order and the
        summary message is benign.
        """
        existing_rows = await self._load_thread_rows_full()
        if not compacted:
            for row in existing_rows:
                try:
                    await self._messages.delete(row.id)
                except NotFoundError:
                    pass
            return

        summary_msg, *tail_msgs = compacted

        # Match tail messages to existing rows by structural equality
        # walking from the END of the existing rows, peeling matched
        # rows off the candidate set.
        existing_by_idx: list[tuple[int, ThreadMessage]] = list(
            enumerate(existing_rows)
        )
        tail_row_ids: list[str] = []
        for tail_msg in reversed(tail_msgs):
            for idx, row in reversed(existing_by_idx):
                if row.role == tail_msg.role and row.parts == tail_msg.parts:
                    tail_row_ids.append(row.id)
                    existing_by_idx = [
                        (i, r) for (i, r) in existing_by_idx if i != idx
                    ]
                    break
        tail_row_ids.reverse()  # back to original chronological order

        head_row_ids = {r.id for (_, r) in existing_by_idx}
        for rid in head_row_ids:
            try:
                await self._messages.delete(rid)
            except NotFoundError:
                pass

        # Insert the summary row at sequence 0.
        await self._messages.create(
            ThreadMessage(
                id=f"tmsg-{uuid.uuid4().hex[:16]}",
                thread_id=self._thread_id,
                role=summary_msg.role,
                parts=summary_msg.parts,
                created_at=datetime.now(timezone.utc),
                sequence=0,
            )
        )

        # Resequence the surviving tail rows starting from 1.
        for new_seq, rid in enumerate(tail_row_ids, start=1):
            row = await self._messages.get(rid)
            if row is None:
                continue
            await self._messages.update(
                row.model_copy(update={"sequence": new_seq}),
            )

    # ---- Internals -------------------------------------------------------

    async def _load_thread_rows_full(self) -> list[ThreadMessage]:
        out: list[ThreadMessage] = []
        cursor: str | None = None
        while True:
            page = await self._messages.find(
                Predicate(
                    left=FieldRef(name="thread_id"),
                    op=Op.EQ,
                    right=Value(value=self._thread_id),
                ),
                # CursorPage.length is capped at 200 by the storage
                # spec (matrix/model/storage.py:265). Loop until
                # next_cursor is None to cover threads larger than
                # one page.
                CursorPage(cursor=cursor, length=200),
                order_by=[OrderBy(field="sequence", direction="asc")],
            )
            out.extend(page.items)
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        return out

    async def _next_sequence(self) -> int:
        existing = await self._load_thread_rows_full()
        if not existing:
            return 0
        return max(r.sequence for r in existing) + 1


__all__ = ["AgentExecutor"]
