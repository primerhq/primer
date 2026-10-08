"""The switch reserves its seq under the lifecycle lock before it appends its marker (S2a PR-12a, plan 3.8 A3).

``apply_binding_switch`` appended its AGENT_MARKER from the ``last_seq`` of a row it had read earlier and then wrote the
whole row back, and its checkpoint caller ran outside the lifecycle lock with no bound on its workspace I/O. A steer that
landed between the read and the write (``wake_session`` appends USER_INPUT at ``last_seq + 1`` and arms a turn) therefore
got its seq REUSED by the marker (a repeated seq in ``messages.jsonl``), had its ``last_seq`` and ``turn_status`` written
back over, and an unreachable workspace held the checkpoint (and, once it takes the lock, Cancel's) forever.

Driven through the two public entry points, so the tests do not know the protocol's internals: the checkpoint helper
(``apply_queued_binding_switch``, the body of every terminal exit's checkpoint and of the pool's ``_end_session``) and the
HTTP route. The workspace is an in-memory ``messages.jsonl``; the race is injected the way it happens: a steer lands right
after the code under test has read the row.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import apply_queued_binding_switch
from primer.session.mutation_lock import session_lifecycle_lock

from tests._support.in_lock_deadline import InLockDeadline
from tests.conftest import _FakeStorageProvider

BODY_BOUND_S = 30.0   # a hang fails the test; a slow runner must not

SID = "s-switch-reserve"
REQUEST = {"kind": "agent", "agent_id": "agent-b", "graph_id": None, "profile_id": None, "actor": "user"}


class _IO:
    """The workspace's ``messages.jsonl`` as the shim sees it (``append_message_line``), in memory."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.hang = False

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        if self.hang:
            # an unreachable workspace: never answers. `hang` is True (waits for ever) or an InLockDeadline (which first expires the
            # in-lock deadline that bounds the caller, so the test needs no wall-clock).
            await (self.hang.hang() if isinstance(self.hang, InLockDeadline) else asyncio.Event().wait())
        self.lines.append(line)

    def records(self) -> list[dict]:
        return [json.loads(ln) for ln in self.lines]

    def seqs(self) -> list[int]:
        return [r["seq"] for r in self.records() if "seq" in r]


def _row(**over) -> WorkspaceSession:
    fields = dict(
        id=SID, workspace_id="w1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), turn_status="idle", last_seq=6, pending_binding_switch=dict(REQUEST),
    )
    fields.update(over)
    return WorkspaceSession(**fields)


async def _seeded() -> tuple[_FakeStorageProvider, object, _IO]:
    provider = _FakeStorageProvider()
    sessions = provider.get_storage(WorkspaceSession)
    await sessions.create(_row())
    return provider, sessions, _IO()


def _land_a_steer_after_the_first_read(sessions, io: _IO):
    """After the FIRST ``get`` of the session returns, a steer lands: its USER_INPUT takes ``last_seq + 1`` and its turn is
    armed, as ``wake_session`` does."""
    real_get = sessions.get
    state = {"landed": False}

    async def racing_get(session_id):
        row = await real_get(session_id)
        if not state["landed"]:
            state["landed"] = True
            seq = row.last_seq + 1
            io.lines.append(json.dumps({"seq": seq, "kind": "user_input", "payload": {"text": "a steer"}}).encode() + b"\n")
            await sessions.update(row.model_copy(update={"last_seq": seq, "turn_status": "claimable"}))
        return row

    sessions.get = racing_get  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_a_steer_that_lands_after_the_checkpoints_read_keeps_its_seq_and_its_turn():
    provider, sessions, io = await _seeded()
    _land_a_steer_after_the_first_read(sessions, io)

    await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)

    seqs = io.seqs()
    assert len(seqs) == len(set(seqs)), f"the switch's marker reused a seq in messages.jsonl: {seqs}"
    row = await sessions.get(SID)
    assert row.turn_status == "claimable", "the checkpoint's write erased the steer's armed turn"
    assert row.last_seq == max(seqs), f"last_seq {row.last_seq} is not the log's highest seq {max(seqs)}"
    assert row.pending_binding_switch == REQUEST and row.binding.agent_id == "agent-a", (
        "a switch whose reservation was rejected must change nothing and stay pending for the next checkpoint"
    )
    assert not [r for r in io.records() if r["kind"] == "agent_marker"], "an orphan marker was written"


@pytest.mark.asyncio
async def test_the_checkpoint_switch_waits_for_the_lifecycle_lock():
    provider, sessions, io = await _seeded()

    async with session_lifecycle_lock().acquire(SID):
        task = asyncio.create_task(apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID))
        await asyncio.sleep(0.1)
        assert io.lines == [] and not task.done(), "the checkpoint switch wrote while another writer held the lifecycle lock"
    await asyncio.wait_for(task, BODY_BOUND_S)

    assert [r["kind"] for r in io.records()] == ["agent_marker"]
    assert (await sessions.get(SID)).binding.agent_id == "agent-b"


@pytest.mark.asyncio
async def test_the_checkpoint_switch_times_out_without_holding_the_lock(monkeypatch, caplog):
    """An unreachable workspace must not hold the lock Cancel and every steer need: the in-lock I/O has a bound; the
    switch stays pending for the next checkpoint."""
    deadline = InLockDeadline(monkeypatch)       # the in-lock deadline expires when the write hangs, not after a wall-clock
    provider, sessions, io = await _seeded()
    io.hang = deadline

    with caplog.at_level(logging.ERROR):
        async with asyncio.timeout(BODY_BOUND_S):    # generous: it only turns a hang into a failure, it never races the code
            await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)

            async with session_lifecycle_lock().acquire(SID):
                pass  # the lock is free: Cancel's C1 would proceed
    # apply_queued_binding_switch swallows every failure (it logs and leaves the switch queued), so a green run proves nothing by itself:
    # the deadline must really have fired, and the log must be the TIMEOUT's, not that of some other swallowed exception.
    deadline.assert_fired()
    messages = [r.getMessage() for r in caplog.records]
    assert any("timed out after" in m and SID in m for m in messages), f"no timeout was logged: {messages}"
    assert not any("queued binding switch failed" in m for m in messages), f"the switch failed for another reason: {messages}"
    row = await sessions.get(SID)
    assert row.pending_binding_switch == REQUEST and row.binding.agent_id == "agent-a", "a timed-out switch must stay pending"
