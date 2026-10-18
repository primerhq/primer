"""An in-memory world for the canvas service tests (pivot P5, task 6 on).

One world is enough to drive the real ``CanvasService`` end to end: the storage is the suite's ``_FakeStorageProvider`` (its
``patch_if`` is the pure-Python statement of the real one, so a fenced write is fenced here too), the workspace is a recorder (the
``messages.jsonl`` lines each session was given and the instructions its slot was handed), and the bus notes what it was told.

    w = make_world()
    await seed_session(w)                       # session "s1" on workspace "ws-1"
    await w.service.apply(session_id="s1", workspace_id="ws-1", ...)
    await w.canvas("s1")                        # the stored SessionCanvas (None without one)
    w.records("s1", "canvas_op")                # the decoded messages.jsonl records written for the session (not async)
    await w.events("canvas.updated")            # the platform events of one type
    w.bus.published                             # [(key, payload)] of every bus publish
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from primer.canvas.service import CanvasService
from primer.model.canvas import SessionCanvas
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.conftest import _FakeStorageProvider


class FakeBus:
    """Notes every publish. ``fail`` makes it behave like a degraded bus: a publish raises and is not noted."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.fail = False

    async def publish(self, key: str, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("bus down")
        self.published.append((key, payload))


class FakeSlot:
    """The on-disk slot of one session: the instructions its FIFO was handed, each with the extra parts that rode along."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, list]] = []
        self.reopened = False

    async def append_instruction(self, content: str, *, extra_parts: Any = None) -> None:
        self.appended.append((content, list(extra_parts or [])))

    async def reopen(self) -> None:
        self.reopened = True


class FakeWorkspace:
    """A workspace that keeps the message lines written to it and hands out one :class:`FakeSlot` per session id."""

    def __init__(self) -> None:
        self.slots: dict[str, FakeSlot] = {}
        self._lines: dict[str, list[bytes]] = {}
        self.fail_writes = False

    async def get_session(self, session_id: str) -> FakeSlot:
        return self.slots.setdefault(session_id, FakeSlot())

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        if self.fail_writes:
            raise OSError("the workspace did not take the write")
        self._lines.setdefault(session_id, []).append(line)

    def lines(self, session_id: str) -> list[dict[str, Any]]:
        """The decoded ``messages.jsonl`` records written for ``session_id``, in order."""
        out: list[dict[str, Any]] = []
        for blob in self._lines.get(session_id, []):
            for line in blob.decode().splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out


class FakeRegistry:
    """One :class:`FakeWorkspace` per workspace id, created on demand."""

    def __init__(self) -> None:
        self.workspaces: dict[str, FakeWorkspace] = {}

    def workspace(self, workspace_id: str) -> FakeWorkspace:
        return self.workspaces.setdefault(workspace_id, FakeWorkspace())

    async def get_workspace(self, workspace_id: str) -> FakeWorkspace:
        return self.workspace(workspace_id)


class StoreSpy:
    """Wraps the canvas storage of a world: notes every ``patch_if`` (its patch and its guard) and refuses any whole-row write.

    The canvas is written whole once (the ``create`` of a session's first batch) and after that only by fenced ``patch_if``; a test
    that installs the spy fails if anything calls ``update`` or ``update_unless``.
    """

    def __init__(self, world: "World") -> None:
        self.store = world.sp.get_storage(SessionCanvas)
        self.patches: list[dict[str, Any]] = []
        self.real_patch_if = self.store.patch_if

        async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
            self.patches.append({"patch": patch, "where": where})
            return await self.real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

        async def whole_row(*args, **kwargs):
            raise AssertionError("the canvas row was written whole; every write after the first is one fenced patch_if")

        self.store.patch_if = patch_if
        self.store.update = whole_row
        self.store.update_unless = whole_row


@dataclass
class World:
    sp: _FakeStorageProvider
    bus: FakeBus
    registry: FakeRegistry
    service: CanvasService

    @property
    def sessions(self) -> Any:
        return self.sp.get_storage(WorkspaceSession)

    async def canvas(self, session_id: str) -> SessionCanvas | None:
        return await self.sp.get_storage(SessionCanvas).get(session_id)

    def records(self, session_id: str, kind: str | None = None, *, workspace_id: str = "ws-1") -> list[dict[str, Any]]:
        found = self.registry.workspace(workspace_id).lines(session_id)
        return [r for r in found if kind is None or r["kind"] == kind]

    async def events(self, event_type: str) -> list[Any]:
        """The platform events of ``event_type`` the world's event store holds, oldest first."""
        return [e for e in await self.sp.get_event_store().read_after(0, limit=10_000) if e.event_type == event_type]


def make_world(**service_kwargs: Any) -> World:
    """A fresh world. ``service_kwargs`` go to :class:`CanvasService` (``script_check=``, ``clock=``)."""
    sp, bus, registry = _FakeStorageProvider(), FakeBus(), FakeRegistry()
    return World(sp, bus, registry, CanvasService(sp, event_bus=bus, workspace_registry=registry, **service_kwargs))


async def seed_session(world: World, sid: str = "s1", workspace_id: str = "ws-1", **fields: Any) -> WorkspaceSession:
    """Store a session row. ``fields`` are any other ``WorkspaceSession`` fields (``turn_status``, ``last_seq``, ``status``...)."""
    created_at = fields.pop("created_at", datetime.now(UTC))
    row = WorkspaceSession(
        id=sid,
        workspace_id=workspace_id,
        binding=fields.pop("binding", None) or AgentSessionBinding(agent_id="agent-" + sid),
        status=fields.pop("status", SessionStatus.WAITING),
        created_at=created_at,
        **fields,
    )
    await world.sessions.create(row)
    return row


__all__ = [
    "FakeBus",
    "FakeRegistry",
    "FakeSlot",
    "FakeWorkspace",
    "StoreSpy",
    "World",
    "make_world",
    "seed_session",
]
