"""S6 P2: the interactive webhook hold.

Spec: docs/superpowers/ux-revamp/10-s6-design.md sections 4 and 9. The wait
is an async bus subscription opened BEFORE the fire, so a run that finishes
while fire_trigger is still returning cannot be missed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import primer.trigger.hold as hold_mod
from primer.bus.in_memory import InMemoryEventBus
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.dispatch import FireResult
from primer.trigger.hold import fire_and_hold, held_targets
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def read_lines(self, session_id: str, filename: str = "messages.jsonl"):
        return self._lines


class _FakeRegistry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace


_LOG = [
    '{"seq": 1, "kind": "user_input", "payload": {"text": "hi"}}',
    '{"seq": 2, "kind": "assistant_token", "payload": {"text": "all done"}}',
    '{"seq": 3, "kind": "done", "payload": {}}',
]


def test_held_targets_ignores_skipped_and_failed():
    result = FireResult(fire_id="fire-1", results=[
        {"ok": True, "skipped": False, "artefact_id": "s1"},
        {"ok": True, "skipped": True, "artefact_id": "s2"},
        {"ok": False, "skipped": False, "artefact_id": "s3"},
        {"ok": True, "skipped": False},
    ])
    assert held_targets(result) == ["s1"]


async def _deps(sp, bus):
    return DispatchDeps(
        storage_provider=sp, claim_engine=object(), scheduler=object(),
        workspace_registry=object(), event_bus=bus,
    )


async def test_hold_returns_final_text_when_the_run_terminates(monkeypatch):
    sp = _FakeStorageProvider()
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # Terminate the run while the fire is still returning: the hold's
        # subscription is already open, so the event cannot be missed.
        await bus.publish("session:s1:terminal", {"status": "ended"})
        return FireResult(
            fire_id="fire-1",
            results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)
    held = await fire_and_hold(
        trigger_id="tr-1",
        extra_context={},
        deps=await _deps(sp, bus),
        workspace_registry=_FakeRegistry(_FakeWorkspace(_LOG)),
        wait_timeout=2.0,
    )
    await bus.aclose()

    assert held.timed_out is False
    assert held.fire_result.fire_id == "fire-1"
    assert held.results == [{"artefact_id": "s1", "final_text": "all done"}]


async def test_hold_times_out_when_the_run_never_terminates(monkeypatch):
    sp = _FakeStorageProvider()
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        return FireResult(
            fire_id="fire-2",
            results=[{"ok": True, "skipped": False, "artefact_id": "s9"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)
    held = await fire_and_hold(
        trigger_id="tr-2",
        extra_context={},
        deps=await _deps(sp, bus),
        workspace_registry=_FakeRegistry(_FakeWorkspace([])),
        wait_timeout=0.05,
    )
    await bus.aclose()

    assert held.timed_out is True
    assert held.results == []
    assert held.fire_result.fire_id == "fire-2"


async def test_hold_returns_immediately_when_nothing_was_dispatched(monkeypatch):
    sp = _FakeStorageProvider()
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        return FireResult(skipped=True, fire_id="fire-3", results=[])

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)
    held = await asyncio.wait_for(
        fire_and_hold(
            trigger_id="tr-3",
            extra_context={},
            deps=await _deps(sp, bus),
            workspace_registry=_FakeRegistry(_FakeWorkspace([])),
            wait_timeout=30.0,
        ),
        timeout=2.0,
    )
    await bus.aclose()

    assert held.timed_out is False
    assert held.results == []
