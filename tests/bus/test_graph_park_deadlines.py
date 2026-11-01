"""A graph park's keys end on their own deadlines (GR-4, Task 103).

A graph superstep can park on several waits at once (approval gates, ask_user, a ``sleep``). Each pending entry of the checkpoint carries its
own absolute ``deadline``; ``TimerScheduler`` fires a ``timer:`` key at its deadline and ``TimeoutSweeper`` times out any other key at its
deadline, whichever key of the park is primary, and a fire on a non-primary ``timer:`` key is delivered by the membership fallback.

Characterization (passes before Task 103 and after it): agent parks are swept on the row's ``parked_until`` as before; a graph park written
before entries carried a deadline times out its primary key only, at ``parked_until``; the ask_user and tool_approval membership fallback
answers as before.

The review's probe ``g1_graph_timer_park.py`` (its sweep and flip half) is inverted here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from primer.bus.scheduler_tasks import _find_due_timer_keys, _find_expired_non_timer_keys
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.session.yields import flip_sessions_parked_on
from tests.conftest import _FakeStorageProvider

pytestmark = pytest.mark.asyncio

SID = "s-dl"
TIMER = f"timer:{SID}:sleeper:tc-s"
GATE = f"tool_approval:{SID}:gate:tc-g"
GATE2 = f"tool_approval:{SID}:gate2:tc-g2"
ASK = f"ask_user:{SID}:asker:tc-q"


def _storage():
    return _FakeStorageProvider().get_storage(WorkspaceSession)


def _agent_row(sid: str, key: str, parked_until: datetime) -> WorkspaceSession:
    at = parked_until - timedelta(seconds=30)
    return WorkspaceSession(
        id=sid, workspace_id="ws", binding=AgentSessionBinding(agent_id="ag"), status=SessionStatus.RUNNING, created_at=at,
        parked_status="parked", parked_event_key=key, parked_at=at, parked_until=parked_until,
        parked_state={"yielded": {"tool_name": "sleep", "event_key": key, "timeout": 30.0, "resume_metadata": {}}},
    )


def _graph_row(entries: list[tuple[str, datetime | None]], *, parked_until: datetime, at: datetime) -> WorkspaceSession:
    """A graph park on ``entries`` (key, deadline); the first is primary. Approval keys are pending tool calls, the rest agent yields."""
    toolcalls: list[dict[str, Any]] = []
    agent_yields: list[dict[str, Any]] = []
    for key, deadline in entries:
        node = key.split(":")[2]
        extra = {} if deadline is None else {"deadline": deadline.isoformat()}
        if key.startswith("tool_approval:"):
            toolcalls.append({"node_id": node, "tool_call_id": key.rsplit(":", 1)[-1], "parked_event_key": key, "arguments": {},
                              "tool_name": "_approval", "resume_metadata": {}, **extra})
        else:
            agent_yields.append({"node_id": node, "tool_call_id": key.rsplit(":", 1)[-1], "event_key": key,
                                 "tool_name": key.split(":")[0], "resume_metadata": {}, "llm_messages": [], "iteration": 0, **extra})
    keys = [key for key, _ in entries]
    return WorkspaceSession(
        id=SID, workspace_id="ws", binding=GraphSessionBinding(graph_id="g"), status=SessionStatus.RUNNING, created_at=at,
        parked_status="parked", parked_event_key=keys[0], parked_event_keys=keys, parked_at=at, parked_until=parked_until,
        parked_state={"yielded": {"tool_name": "_approval", "event_key": keys[0], "event_keys": keys},
                      "graph_checkpoint": {"nodes": {}, "pending_toolcalls": toolcalls, "pending_agent_yields": agent_yields}},
    )


def _held(row: WorkspaceSession) -> list[str]:
    return sorted(e["event_key"] for e in ((row.parked_state or {}).get("resume_event_payloads") or {}).values())


# ---- characterization ------------------------------------------------------------------------------------------------------------------------


async def test_an_agent_timer_park_fires_at_its_deadline_and_not_before() -> None:
    storage = _storage()
    now = datetime.now(UTC)
    await storage.create(_agent_row("due", "timer:due:tc", now - timedelta(seconds=1)))
    await storage.create(_agent_row("ahead", "timer:ahead:tc", now + timedelta(seconds=60)))

    assert await _find_due_timer_keys(storage) == ["timer:due:tc"]
    assert await _find_expired_non_timer_keys(storage) == []


async def test_an_agent_wait_times_out_at_its_deadline_and_not_before() -> None:
    storage = _storage()
    now = datetime.now(UTC)
    await storage.create(_agent_row("due", "ask_user:due:tc", now - timedelta(seconds=1)))
    await storage.create(_agent_row("ahead", "evwait:ahead:tc", now + timedelta(seconds=60)))

    assert await _find_expired_non_timer_keys(storage) == ["ask_user:due:tc"]
    assert await _find_due_timer_keys(storage) == []


async def test_a_graph_park_from_before_entries_carried_deadlines_times_out_its_primary_only() -> None:
    storage = _storage()
    at = datetime.now(UTC) - timedelta(hours=2)
    await storage.create(_graph_row([(GATE, None), (GATE2, None), (TIMER, None)], parked_until=at + timedelta(seconds=3600), at=at))

    assert await _find_expired_non_timer_keys(storage) == [GATE]
    assert await _find_due_timer_keys(storage) == []


async def test_the_ask_user_and_tool_approval_membership_fallback_answers_as_before() -> None:
    storage = _storage()
    at = datetime.now(UTC)
    await storage.create(_graph_row([(GATE, None), (ASK, None), (GATE2, None)], parked_until=at + timedelta(seconds=3600), at=at))

    assert await flip_sessions_parked_on(ASK, {"response": "blue"}, session_storage=storage, engine=None) == 1
    assert await flip_sessions_parked_on(GATE2, {"decision": "approved"}, session_storage=storage, engine=None) == 1
    assert await flip_sessions_parked_on(f"tool_approval:{SID}:nobody:tc-x", {"decision": "approved"}, session_storage=storage, engine=None) == 0
    row = await storage.get(SID)
    assert (row.parked_status, _held(row)) == ("resumable", sorted([ASK, GATE2]))


# ---- GR-4 ------------------------------------------------------------------------------------------------------------------------------------


async def test_a_graph_sleep_is_due_at_its_own_deadline() -> None:
    """The probe's first half: 60 s after a ``sleep(30)`` the timer is due, though the row was parked for the old 3600 s."""
    storage = _storage()
    at = datetime.now(UTC) - timedelta(seconds=60)
    await storage.create(_graph_row([(TIMER, at + timedelta(seconds=30))], parked_until=at + timedelta(seconds=3600), at=at))

    assert await _find_due_timer_keys(storage) == [TIMER]


async def test_a_timer_that_is_not_the_primary_key_fires_at_its_deadline_and_its_fire_is_delivered() -> None:
    storage = _storage()
    at = datetime.now(UTC) - timedelta(seconds=60)
    await storage.create(_graph_row([(GATE, at + timedelta(seconds=3600)), (TIMER, at + timedelta(seconds=30))],
                                    parked_until=at + timedelta(seconds=30), at=at))

    due = await _find_due_timer_keys(storage)
    expired = await _find_expired_non_timer_keys(storage)
    flipped = await flip_sessions_parked_on(TIMER, {}, session_storage=storage, engine=None)

    assert (due, expired, flipped) == ([TIMER], [], 1)
    row = await storage.get(SID)
    assert (row.parked_status, _held(row)) == ("resumable", [TIMER])


async def test_a_gate_that_is_not_the_primary_key_times_out_at_its_own_deadline() -> None:
    storage = _storage()
    at = datetime.now(UTC) - timedelta(seconds=120)
    await storage.create(_graph_row([(ASK, at + timedelta(seconds=3600)), (GATE, at + timedelta(seconds=60))],
                                    parked_until=at + timedelta(seconds=60), at=at))

    assert await _find_expired_non_timer_keys(storage) == [GATE]


async def test_a_fire_on_a_non_primary_timer_key_is_delivered() -> None:
    """The probe's second half: an approval is primary and every deadline is long past; a fire on the timer key wakes the session."""
    storage = _storage()
    at = datetime.now(UTC) - timedelta(hours=2)
    await storage.create(WorkspaceSession(
        id="s2", workspace_id="ws", binding=GraphSessionBinding(graph_id="g"), status=SessionStatus.RUNNING, created_at=at,
        parked_status="parked", parked_event_key="tool_approval:s2:gate:tc-g", parked_event_keys=["tool_approval:s2:gate:tc-g", "timer:s2:sleeper:tc-s"],
        parked_at=at, parked_until=at + timedelta(seconds=10), parked_state={"graph_checkpoint": {"nodes": {}}},
    ))

    assert await flip_sessions_parked_on("timer:s2:sleeper:tc-s", {}, session_storage=storage, engine=None) == 1
    assert (await storage.get("s2")).parked_status == "resumable"


async def test_an_entry_the_row_does_not_wait_on_is_not_swept() -> None:
    """A nested-continuation re-park waits on its leaf only; a sibling entry left in the checkpoint must not be republished every tick."""
    storage = _storage()
    at = datetime.now(UTC) - timedelta(seconds=120)
    row = _graph_row([(ASK, at + timedelta(seconds=3600)), (GATE, at + timedelta(seconds=60)), (TIMER, at + timedelta(seconds=30))],
                     parked_until=at + timedelta(seconds=3600), at=at)
    await storage.create(row.model_copy(update={"parked_event_keys": None}))

    assert (await _find_expired_non_timer_keys(storage), await _find_due_timer_keys(storage)) == ([], [])
