"""Review 2026-11-23 6.10: the hold depended on one volatile NOTIFY; a lost wake held the caller to the full timeout.

The hold's bus subscription is the fast path, but a completion NOTIFY is
volatile (a Postgres NOTIFY published while the process is not listening,
or a dropped queue entry, never arrives). The session ROW is the durable
state: the hold now re-checks the pending rows on a bounded poll, so a
lost NOTIFY is noticed within one poll interval instead of waiting the
caller's full wait_timeout and answering as a timeout.

This test publishes NOTHING on the bus (the lost NOTIFY): the row alone
carries the terminal. Old behaviour: the hold waits the full wait_timeout
and returns ``timed_out=True``. The bus fast path stays covered by
``tests/trigger/test_hold.py`` (event arrives -> the hold releases on it).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

import primer.trigger.hold as hold_mod
from primer.bus.in_memory import InMemoryEventBus
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.dispatch import FireResult
from primer.trigger.hold import fire_and_hold
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


class _FakeWorkspace:
    state_path = ".state"

    def read_lines(self, session_id: str, filename: str = "messages.jsonl"):
        return [
            '{"seq": 1, "kind": "user_input", "payload": {"text": "hi"}}',
            '{"seq": 2, "kind": "assistant_token", "payload": {"text": "all done"}}',
            '{"seq": 3, "kind": "done", "payload": {}}',
        ]


class _FakeRegistry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace


async def _deps(sp, bus):
    return DispatchDeps(
        storage_provider=sp, claim_engine=object(), scheduler=object(),
        workspace_registry=object(), event_bus=bus,
    )


@pytest.mark.asyncio
async def test_a_lost_notify_is_caught_by_the_row_poll(monkeypatch):
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # The run ends while the fire is still returning -- but the
        # completion NOTIFY is LOST: nothing is published on the bus, so
        # only the row says the run is over.
        await sessions.update(WorkspaceSession(
            id="s1", workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.ENDED, ended_reason="completed",
            created_at=datetime.now(UTC),
        ))
        return FireResult(
            fire_id="fire-1",
            results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)
    wait_timeout = 5.0
    t0 = time.monotonic()
    held = await fire_and_hold(
        trigger_id="tr-1",
        extra_context={},
        deps=await _deps(sp, bus),
        workspace_registry=_FakeRegistry(_FakeWorkspace()),
        wait_timeout=wait_timeout,
    )
    await bus.aclose()
    elapsed = time.monotonic() - t0

    assert held.timed_out is False, (
        "the hold waited the full wait_timeout: the lost NOTIFY left it "
        "depending on the bus alone instead of re-checking the row"
    )
    assert elapsed < wait_timeout - 1.0, (
        f"the hold waited {elapsed:.2f}s of a {wait_timeout:.0f}s cap: a "
        "bounded row poll must notice the terminal well before the timeout"
    )
    assert held.results == [{"artefact_id": "s1", "final_text": "all done"}]


# ---------------------------------------------------------------------------
# Round 1 (review 2026-11-23 lane J): I3, I4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_row_poll_runs_even_while_other_sessions_keep_ending(monkeypatch):
    """I3: every unrelated terminal event re-armed the 2 s wait and
    ``continue``d past the poll, so with other sessions ending every 0.5 s
    the poll never fired and the hold timed out. The poll runs on a fixed
    deadline that events cannot move."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # The run ends immediately and silently: no bus event for s1.
        await sessions.update(WorkspaceSession(
            id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.ENDED, ended_reason="completed",
            created_at=datetime.now(UTC),
        ))
        return FireResult(
            fire_id="fire-3", results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)

    async def _noise():
        i = 0
        while True:
            await asyncio.sleep(0.5)
            i += 1
            await bus.publish(f"session:other{i}:terminal", {"status": "ended"})

    noise = asyncio.create_task(_noise())
    wait_timeout = 6.0
    t0 = time.monotonic()
    try:
        held = await fire_and_hold(
            trigger_id="tr-3",
            extra_context={},
            deps=await _deps(sp, bus),
            workspace_registry=_FakeRegistry(_FakeWorkspace()),
            wait_timeout=wait_timeout,
        )
    finally:
        noise.cancel()
        await bus.aclose()
    elapsed = time.monotonic() - t0

    assert held.timed_out is False, (
        f"the row poll was starved by other sessions' terminal events "
        f"(held {elapsed:.2f}s of the {wait_timeout:.0f}s cap)"
    )
    assert elapsed < wait_timeout - 1.0, (
        f"the hold waited {elapsed:.2f}s of a {wait_timeout:.0f}s cap: the "
        "fixed poll deadline must notice the row well before the timeout"
    )
    assert held.results == [{"artefact_id": "s1", "final_text": "all done"}]


@pytest.mark.asyncio
async def test_one_failed_poll_read_does_not_abort_the_hold(monkeypatch):
    """I4: an exception from the row read propagated out of fire_and_hold (a
    500 after the fire had already happened). A poll read that fails once
    keeps the session pending and keeps waiting; the next poll sees the
    terminal."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.ENDED, ended_reason="completed",
        created_at=datetime.now(UTC),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # The run is already ended and its NOTIFY was lost: only the row
        # says so, and the first read of it fails.
        return FireResult(
            fire_id="fire-4", results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)

    real_get = sessions.get
    calls = {"n": 0}

    async def flaky_get(id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient storage failure")
        return await real_get(id, **kwargs)

    sessions.get = flaky_get

    wait_timeout = 10.0
    t0 = time.monotonic()
    try:
        held = await fire_and_hold(
            trigger_id="tr-4",
            extra_context={},
            deps=await _deps(sp, bus),
            workspace_registry=_FakeRegistry(_FakeWorkspace()),
            wait_timeout=wait_timeout,
        )
    finally:
        await bus.aclose()
    elapsed = time.monotonic() - t0

    assert held.timed_out is False, (
        f"one failed poll read aborted the hold after the fire "
        f"(held {elapsed:.2f}s of the {wait_timeout:.0f}s cap)"
    )
    assert elapsed < wait_timeout - 2.0, (
        f"the hold waited {elapsed:.2f}s of a {wait_timeout:.0f}s cap: the "
        "failed read must keep the session pending until the next poll"
    )
    assert held.results == [{"artefact_id": "s1", "final_text": "all done"}]


# ---------------------------------------------------------------------------
# X2.1 (J minor M3): the poll treated only ENDED as finished, but the bus's
# ``:terminal`` announcement is a TURN boundary, and
# ``primer/session/dispatch.py::_publish_terminal`` can carry it with a
# non-ENDED status - WAITING (a turn that completed, or rested after a
# transport failure) or PAUSED (a pause that landed while the turn ran). A
# lost NOTIFY on such a boundary left the hold waiting the full timeout.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [SessionStatus.WAITING, SessionStatus.PAUSED])
async def test_a_lost_notify_on_a_non_ended_terminal_is_caught_by_the_row_poll(
    monkeypatch, status,
):
    """A fresh session whose fired turn ends WAITING (an interactive
    completion, or a rested transport failure) or PAUSED (a pause landing
    mid-turn): the bus would announce the terminal, the NOTIFY is lost, and
    the row poll must still catch it within one poll interval."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # The fired turn is still running when the fire returns (the row
        # the hold reads after the fire carries turn_no 0); nothing is
        # published on the bus either way - the completion NOTIFY is lost,
        # so only the row says the run is over.
        return FireResult(
            fire_id="fire-m3",
            results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)

    async def _turn_ends():
        # Let fire_and_hold capture the row first (the fired turn still
        # running), then the turn ends in a non-ENDED state: the fired
        # turn's release bumped turn_no to 1 (the poll's fresh-row arm is
        # gated on exactly that turn).
        await asyncio.sleep(0.2)
        await sessions.update(WorkspaceSession(
            id="s1", workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=status, turn_no=1, created_at=datetime.now(UTC),
        ))

    turn = asyncio.create_task(_turn_ends())
    wait_timeout = 5.0
    t0 = time.monotonic()
    try:
        held = await fire_and_hold(
            trigger_id="tr-m3",
            extra_context={},
            deps=await _deps(sp, bus),
            workspace_registry=_FakeRegistry(_FakeWorkspace()),
            wait_timeout=wait_timeout,
        )
    finally:
        turn.cancel()
        await bus.aclose()
    elapsed = time.monotonic() - t0

    assert held.timed_out is False, (
        f"a {status.value} terminal the bus treats as terminal is invisible "
        "to the row poll: the hold waited the full wait_timeout"
    )
    assert elapsed < wait_timeout - 1.0, (
        f"the hold waited {elapsed:.2f}s of a {wait_timeout:.0f}s cap: a "
        "bounded row poll must notice the terminal well before the timeout"
    )
    assert held.results == [{"artefact_id": "s1", "final_text": "all done"}]


async def test_an_existing_idle_session_is_not_completed_by_its_pre_fire_waiting(
    monkeypatch,
):
    """Guard for the M3 arm: a target that ALREADY sat WAITING before the
    fire (an append into an idle session) must not be completed by the
    poll's non-ENDED arm - its WAITING predates the fired run, and its
    terminal is the appended turn's, which the row alone cannot date. Only
    ENDED (or a gone row) completes such a target."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # The appended message is queued; the run has not ended (the row is
        # unchanged) and nothing is published on the bus.
        return FireResult(
            fire_id="fire-guard",
            results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)
    t0 = time.monotonic()
    held = await fire_and_hold(
        trigger_id="tr-guard",
        extra_context={},
        deps=await _deps(sp, bus),
        workspace_registry=_FakeRegistry(_FakeWorkspace()),
        wait_timeout=2.5,
    )
    await bus.aclose()
    elapsed = time.monotonic() - t0

    assert held.timed_out is True, (
        "the poll completed an idle WAITING row that predates the fired run: "
        "the hold answered before the appended turn ran"
    )
    assert elapsed < 4.0
    assert held.results == []


# ---------------------------------------------------------------------------
# X2.1 round 2 (M1): the fresh-row arm completed the hold on ANY WAITING/
# PAUSED boundary of a row born around the fire - including a boundary the
# fired turn never reached (a user append or a pause that ran during the
# fired turn). The arm is now gated on the fired turn itself: the boundary's
# turn_no must be exactly one past the row's turn_no AT FIRE TIME. (A failed
# fired turn or a park does not bump turn_no, so their lost-NOTIFY
# boundaries degrade to the wait cap on this row-only path; the bus fast
# path is unaffected.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_foreign_turns_boundary_is_not_attributed_to_the_fired_turn(
    monkeypatch,
):
    """Fresh session: the fired turn ends, then a USER turn appends and
    reaches WAITING, with the completion NOTIFYs lost. The row alone shows
    the user turn's boundary (turn_no two past the fire's), and the row
    poll must not pass it off as the fired run's result."""
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="s1", workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))
    bus = InMemoryEventBus()
    await bus.initialize()

    async def _fake_fire(**kwargs):
        # The fired turn is still running when the fire returns (the row
        # the hold reads after the fire carries turn_no 0); nothing is
        # published on the bus either way - both completion NOTIFYs are
        # lost.
        return FireResult(
            fire_id="fire-m1",
            results=[{"ok": True, "skipped": False, "artefact_id": "s1"}],
        )

    monkeypatch.setattr(hold_mod, "fire_trigger", _fake_fire)

    async def _turns_end():
        # Let fire_and_hold capture the row first (the fired turn still
        # running), then: the fired turn ends (turn 1), the user's appended
        # turn ends (turn 2). The row alone now says WAITING, turn_no 2.
        await asyncio.sleep(0.2)
        await sessions.update(WorkspaceSession(
            id="s1", workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.WAITING, turn_no=1, created_at=datetime.now(UTC),
        ))
        await asyncio.sleep(0.05)
        await sessions.update(WorkspaceSession(
            id="s1", workspace_id="w1",
            binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.WAITING, turn_no=2, created_at=datetime.now(UTC),
        ))

    turns = asyncio.create_task(_turns_end())
    t0 = time.monotonic()
    try:
        held = await fire_and_hold(
            trigger_id="tr-m1",
            extra_context={},
            deps=await _deps(sp, bus),
            workspace_registry=_FakeRegistry(_FakeWorkspace()),
            wait_timeout=2.5,
        )
    finally:
        turns.cancel()
        await bus.aclose()
    elapsed = time.monotonic() - t0

    assert held.timed_out is True, (
        f"the poll completed the hold on the USER turn's boundary "
        f"(held {elapsed:.2f}s): a row born around the fire that reached "
        "WAITING two turns past the fire's is not the fired run's result"
    )
    assert held.results == []
