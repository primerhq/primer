"""Agent memory models: the per-agent settings row, the deltas a reflection may propose, and the background job row.

An agent's memory entries are documents in its ``mem-<agent id>`` collection (``primer/memory/``); only the settings and the job queue are rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field

from primer.model.collection import CollectionSearchConfig
from primer.model.common import Identifiable


class AgentMemory(Identifiable):
    """One agent's memory settings. The row id IS the agent id. Memory is off until the owner turns it on."""

    enabled: bool = False
    promotion: Literal["auto", "approve"] = "auto"
    teachers: Literal["owner", "everyone"] = "owner"
    idle_minutes: int = Field(default=10, ge=1, le=1440)
    auto_inject: bool = False
    inject_top_k: int = Field(default=2, ge=1, le=3)
    inject_min_score: float | None = None
    max_index_entries: int = Field(default=40, ge=1, le=200)
    max_active: int = Field(default=200, ge=1, le=2000)
    archive_after_days: int = Field(default=60, ge=1, le=3650)
    daily_reflections: int = Field(default=50, ge=1, le=1000)
    search: CollectionSearchConfig | None = None   # None: the install default
    capture_topics: list[str] = Field(default_factory=list)
    do_not_capture: list[str] = Field(default_factory=list)
    topics_edited: bool = False
    topics_suggestion: list[str] | None = None
    reflection_prompt: str | None = None
    reflections_day: date | None = None
    reflections_count: int = 0
    last_curated_at: datetime | None = None


class MemoryAdd(BaseModel):
    op: Literal["add"]
    name: str
    description: str
    steps: list[str]
    pitfalls: list[str]
    example: str | None = None
    topics: list[str]


class ItemEdit(BaseModel):
    action: Literal["add", "change", "remove"]
    section: Literal["steps", "pitfalls", "example"]
    item_id: str | None = None
    text: str | None = None


class MemoryPatch(BaseModel):
    op: Literal["patch"]
    name: str
    edits: list[ItemEdit]


class MemoryCredit(BaseModel):
    op: Literal["credit"]
    name: str
    verdict: Literal["helpful", "harmful"]
    reason: str


MemoryDelta = Annotated[MemoryAdd | MemoryPatch | MemoryCredit, Field(discriminator="op")]


class ReflectionOutput(BaseModel):
    """What one reflection may return: whether the run succeeded, and at most three deltas."""

    run_succeeded: bool
    deltas: list[MemoryDelta] = Field(max_length=3)


class MemoryJob(Identifiable):
    """A queued background job of the ``memory`` claim kind."""

    _id_prefix: ClassVar[str | None] = "memory-job"

    kind: Literal["reflect", "curate", "topics"]
    agent_id: str
    session_id: str | None = None
    reflected_turn: int = 0
    target_turn: int = 0
    created_at: datetime
    expires_at: datetime


__all__ = [
    "AgentMemory", "ItemEdit", "MemoryAdd", "MemoryCredit", "MemoryDelta", "MemoryJob", "MemoryPatch", "ReflectionOutput",
]
