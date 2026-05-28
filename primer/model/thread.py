"""Thread + ThreadMessage Pydantic models for the agent executor.

A *thread* is one persistent chat conversation between a user and an
agent. The ``AgentExecutor`` (sub-project F3) drives turns against
threads; thread + message rows are persisted via the existing
:class:`primer.int.Storage` interface (typically against the Postgres
backend, but any storage that satisfies ``Storage[T]`` works).

See ``docs/superpowers/specs/2026-05-03-agent-executor-design.md``
sections "Thread" and "ThreadMessage" for the surrounding design.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from primer.model.chat import Part
from primer.model.common import Identifiable


class Thread(Identifiable):
    """One persistent chat thread between a user and an agent."""

    agent_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the :class:`primer.model.agent.Agent` this "
            "thread runs against. Snapshot -- if the agent definition "
            "changes later, existing threads keep their original "
            "agent_id but pick up the new agent definition's "
            "system_prompt / tools / model on subsequent turns."
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "Optional human-readable thread title. The executor does "
            "NOT auto-generate one; UIs that want titles supply their "
            "own at open time or update later via "
            ":meth:`primer.int.Storage.update`."
        ),
    )
    created_at: datetime = Field(
        ...,
        description="UTC instant the thread was opened.",
    )
    last_activity_at: datetime = Field(
        ...,
        description=(
            "UTC instant of the most recent message append. Updated "
            "by the executor at the end of every turn."
        ),
    )


class ThreadMessage(Identifiable):
    """One message persisted under a :class:`Thread`."""

    thread_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the parent :class:`Thread` this message belongs to.",
    )
    role: Literal["user", "assistant", "system", "tool"] = Field(
        ...,
        description=(
            "Speaker role for this message. Mirrors "
            ":attr:`primer.model.chat.Message.role`."
        ),
    )
    parts: list[Part] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered content parts of this message. Same shape as "
            ":attr:`primer.model.chat.Message.parts`."
        ),
    )
    created_at: datetime = Field(
        ...,
        description="UTC instant the message was appended to the thread.",
    )
    sequence: int = Field(
        ...,
        ge=0,
        description=(
            "Monotonic per-thread sequence number. Used as the "
            "secondary sort key when paginating thread history; the "
            "executor assigns it incrementally as messages are "
            "appended."
        ),
    )


__all__ = ["Thread", "ThreadMessage"]
