"""Shared fakes for the session-level peer-message tests (pivot P3).

One in-memory "world" is enough to drive the real ``wake_session``, ``deliver_steer``, ``store_pending_steer`` and ``realize_next_pending``
end to end: the storage is the suite's ``_FakeStorageProvider``, the workspace is a recorder (the on-disk slot FIFO and the
``messages.jsonl`` lines each session was given), and the scheduler, the claim engine and the event bus only note what they were told.
No behaviour is stubbed, so a test over this world reads the same rows and records a live server would.

    world = make_world()
    await seed_session(world, sid="ses-b", turn_no=1)
    await deliver_steer(session_id="ses-b", text="hi", parallelism="queue", human_intent=False, **world.deliver_kwargs())
    records(world, "ses-b")          # the decoded messages.jsonl records
    slot(world, "ses-b").appended    # what reached the session's FIFO
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.enqueue import SessionWakeDeps
from tests.conftest import _FakeStorageProvider


class FakeSlot:
    """The on-disk slot of one session: the instructions its FIFO was given and whether it was reopened."""

    def __init__(self) -> None:
        self.appended: list[str] = []
        self.reopened = False

    async def append_instruction(self, content: str, *, extra_parts: Any = None) -> None:
        self.appended.append(content)

    async def reopen(self) -> None:
        self.reopened = True


class FakeWorkspace:
    """A workspace that hands out one :class:`FakeSlot` per session id (created on demand) and keeps the message lines written to it."""

    def __init__(self) -> None:
        self.slots: dict[str, FakeSlot] = {}
        self.lines: dict[str, list[bytes]] = {}

    def slot(self, session_id: str) -> FakeSlot:
        return self.slots.setdefault(session_id, FakeSlot())

    async def get_session(self, session_id: str) -> FakeSlot:
        return self.slot(session_id)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.setdefault(session_id, []).append(line)


class FakeRegistry:
    """One :class:`FakeWorkspace` per workspace id. ``phase`` is what every workspace row reports (a ``workspace_lost`` session reopens
    only when it reads ``running``).

    A workspace is removed the way a delete removes it: ``remove(id)`` takes the row (and so the instance) away, and ``destroying`` holds the ids a
    destroy is running for, where the runtime is already gone and the row is still there (``is_being_destroyed``, as on the real registry)."""

    def __init__(self, *, phase: str = "ready") -> None:
        self.workspaces: dict[str, FakeWorkspace] = {}
        self.phase = phase
        self.removed: set[str] = set()
        self.destroying: set[str] = set()

    def workspace(self, workspace_id: str) -> FakeWorkspace:
        return self.workspaces.setdefault(workspace_id, FakeWorkspace())

    def remove(self, workspace_id: str) -> None:
        self.removed.add(workspace_id)

    def is_being_destroyed(self, workspace_id: str) -> bool:
        return workspace_id in self.destroying

    async def get_workspace(self, workspace_id: str) -> FakeWorkspace:
        if workspace_id in self.removed:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
        return self.workspace(workspace_id)

    async def get_workspace_row(self, workspace_id: str) -> SimpleNamespace:
        if workspace_id in self.removed:
            raise NotFoundError(f"Workspace {workspace_id!r} does not exist")
        return SimpleNamespace(phase=self.phase)


class FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


class FakeEngine:
    """Notes every upsert. ``leased`` holds the session ids that have a lease row (a turn holds them); none do unless a test says so."""

    def __init__(self) -> None:
        self.upserts: list[tuple] = []
        self.leased: set[str] = set()

    async def upsert(self, kind: Any, session_id: str, *, priority: int = 100, next_attempt_at: Any = None) -> None:
        self.upserts.append((kind, session_id))

    async def has_lease(self, kind: Any, session_id: str) -> bool:
        return session_id in self.leased


class FakeBus:
    """Notes every publish. A gate or a wait is answered by a publish on its key, so a test that must see nothing answered looks here."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


@dataclass
class World:
    sp: _FakeStorageProvider
    registry: FakeRegistry
    scheduler: FakeScheduler
    engine: FakeEngine
    bus: FakeBus | None

    @property
    def sessions(self) -> Any:
        return self.sp.get_storage(WorkspaceSession)

    def deliver_kwargs(self) -> dict[str, Any]:
        """The five collaborators ``deliver_steer`` takes."""
        return {
            "storage_provider": self.sp,
            "scheduler": self.scheduler,
            "claim_engine": self.engine,
            "workspace_registry": self.registry,
            "event_bus": self.bus,
        }

    def wake_deps(self) -> SessionWakeDeps:
        return SessionWakeDeps(
            storage_provider=self.sp,
            scheduler=self.scheduler,
            claim_engine=self.engine,
            workspace_registry=self.registry,
            event_bus=self.bus,
        )


def make_world(*, with_bus: bool = False) -> World:
    return World(
        sp=_FakeStorageProvider(),
        registry=FakeRegistry(),
        scheduler=FakeScheduler(),
        engine=FakeEngine(),
        bus=FakeBus() if with_bus else None,
    )


async def seed_session(
    world: World,
    *,
    sid: str,
    workspace_id: str = "ws-1",
    status: SessionStatus = SessionStatus.WAITING,
    binding: Any = None,
    **fields: Any,
) -> WorkspaceSession:
    """Store a session row. ``fields`` are any other ``WorkspaceSession`` fields (``turn_no``, ``turn_status``, ``parked_status``...)."""
    created_at = fields.pop("created_at", datetime.now(UTC))
    row = WorkspaceSession(
        id=sid,
        workspace_id=workspace_id,
        binding=binding if binding is not None else AgentSessionBinding(agent_id="agent-" + sid),
        status=status,
        created_at=created_at,
        **fields,
    )
    await world.sessions.create(row)
    return row


def records(world: World, session_id: str, *, workspace_id: str = "ws-1") -> list[dict[str, Any]]:
    """The decoded ``messages.jsonl`` records written for ``session_id``, in order."""
    out: list[dict[str, Any]] = []
    for blob in world.registry.workspace(workspace_id).lines.get(session_id, []):
        for line in blob.decode().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def slot(world: World, session_id: str, *, workspace_id: str = "ws-1") -> FakeSlot:
    return world.registry.workspace(workspace_id).slot(session_id)


__all__ = [
    "FakeBus",
    "FakeEngine",
    "FakeRegistry",
    "FakeScheduler",
    "FakeSlot",
    "FakeWorkspace",
    "World",
    "make_world",
    "records",
    "seed_session",
    "slot",
]
