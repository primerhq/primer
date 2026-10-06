"""A retried binding switch does not reuse the epoch of a marker an earlier attempt left behind (S2a PR-12a, plan 3.8 A3, N148).

The switch appends its AGENT_MARKER (epoch ``row.binding_epoch + 1``) and only then writes the binding fields. A timeout
or a cancellation between the two (the local workspace appends through ``asyncio.to_thread``, which keeps writing after
its await is abandoned) leaves the marker in the log with the row still on the old binding and epoch. The NEXT attempt
used to mint the same epoch again: two markers with one epoch, and a transcript that says the switch happened twice.

The retry reads the log whole (through the io shim) and looks for AGENT_MARKER records above the row's applied epoch:

* an orphan AT THE TIP (the record at the row's ``last_seq``, same epoch ``+ 1``, same target) is COMPLETED: only the
  closing write is made, no second marker;
* any other orphan (the normal case: a full turn ran since, so the tip moved; or a different target) is MINTED PAST: the
  new marker takes ``max(every marker epoch in the log, the row's epoch) + 1``, and the orphan stays a structural record
  that was never applied. Completing from an orphan below the tip would write ``next_unprocessed_seq = seq + 1`` and move
  the drain cursor BACKWARDS.

Driven through the checkpoint helper; the first attempt is made to fail between the marker and the closing write.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import apply_queued_binding_switch

from tests.conftest import _FakeStorageProvider

SID = "s-orphan"


def _switch_to(agent_id: str) -> dict:
    return {"kind": "agent", "agent_id": agent_id, "graph_id": None, "profile_id": None, "actor": "user"}


class _IO:
    """``messages.jsonl`` in memory, with the reader the io shim has."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)

    async def read_state_file(self, workspace_id: str, state_relative_path: str) -> bytes:
        return b"".join(self.lines)

    def records(self) -> list[dict]:
        return [json.loads(ln) for ln in self.lines]

    def markers(self) -> list[dict]:
        return [r for r in self.records() if r["kind"] == "agent_marker"]

    def add(self, seq: int, kind: str, **payload) -> None:
        self.lines.append(json.dumps({"seq": seq, "kind": kind, "payload": payload}).encode() + b"\n")


async def _world(pending: dict) -> tuple[_FakeStorageProvider, object, _IO]:
    provider = _FakeStorageProvider()
    sessions = provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id=SID, workspace_id="w1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), turn_status="idle", last_seq=6, next_unprocessed_seq=7,
        pending_binding_switch=pending,
    ))
    return provider, sessions, _IO()


def _fail_the_closing_write_once(sessions) -> None:
    """The first attempt dies between the marker and the closing write (a timeout there leaves exactly this)."""
    real = sessions.patch_if
    state = {"failed": False}

    async def patch_if(session_id, patch=None, *, where, **kwargs):
        if "binding" in (patch or {}) and not state["failed"]:
            state["failed"] = True
            raise OSError("the attempt was abandoned before its closing write")
        return await real(session_id, patch, where=where, **kwargs)

    sessions.patch_if = patch_if  # type: ignore[method-assign]


async def _first_attempt_leaves_an_orphan(provider, sessions, io) -> None:
    _fail_the_closing_write_once(sessions)
    await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)
    row = await sessions.get(SID)
    assert [m["seq"] for m in io.markers()] == [7] and io.markers()[0]["payload"]["binding_epoch"] == 1, "no orphan was left"
    assert (row.binding.agent_id, row.binding_epoch, row.last_seq) == ("agent-a", 0, 7), "the first attempt got further than planned"


@pytest.mark.asyncio
async def test_a_retried_checkpoint_switch_completes_from_the_orphan_marker_instead_of_appending_another():
    provider, sessions, io = await _world(_switch_to("agent-b"))
    await _first_attempt_leaves_an_orphan(provider, sessions, io)

    await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)

    assert len(io.markers()) == 1, f"the retry appended another marker: {[m['payload']['binding_epoch'] for m in io.markers()]}"
    row = await sessions.get(SID)
    assert (row.binding.agent_id, row.binding_epoch, row.last_seq, row.next_unprocessed_seq) == ("agent-b", 1, 7, 8)
    assert row.pending_binding_switch is None


@pytest.mark.asyncio
async def test_a_retried_checkpoint_switch_after_a_full_turn_mints_past_the_orphan_epoch():
    """The REALISTIC variant: the retry comes at the end of the NEXT turn, after USER_INPUT, assistant and DONE records
    advanced ``last_seq``. No epoch is reused and the drain cursor never moves backwards."""
    provider, sessions, io = await _world(_switch_to("agent-b"))
    await _first_attempt_leaves_an_orphan(provider, sessions, io)
    io.add(8, "user_input", text="next question")
    io.add(9, "assistant_token", text="an answer")
    io.add(10, "done")
    row = await sessions.get(SID)
    await sessions.update(row.model_copy(update={"last_seq": 10, "next_unprocessed_seq": 11}))

    await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)

    epochs = [m["payload"]["binding_epoch"] for m in io.markers()]
    assert epochs == [1, 2], f"the retry reused an epoch: {epochs}"
    row = await sessions.get(SID)
    assert (row.binding.agent_id, row.binding_epoch, row.last_seq) == ("agent-b", 2, 11)
    assert row.next_unprocessed_seq == 12, "the drain cursor moved backwards or stayed behind the new marker"


@pytest.mark.asyncio
async def test_an_orphan_for_another_target_at_the_tip_is_minted_past_not_completed():
    provider, sessions, io = await _world(_switch_to("agent-b"))
    await _first_attempt_leaves_an_orphan(provider, sessions, io)
    row = await sessions.get(SID)
    await sessions.update(row.model_copy(update={"pending_binding_switch": _switch_to("agent-c")}))  # the user changed their mind

    await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)

    assert [m["payload"]["binding_epoch"] for m in io.markers()] == [1, 2]
    row = await sessions.get(SID)
    assert (row.binding.agent_id, row.binding_epoch, row.last_seq) == ("agent-c", 2, 8)


@pytest.mark.asyncio
async def test_a_log_that_cannot_be_read_leaves_the_switch_queued_and_writes_nothing():
    """Not knowing whether an earlier attempt left a marker, the switch must not append one (it could reuse its epoch)."""
    provider, sessions, io = await _world(_switch_to("agent-b"))

    async def unreadable(workspace_id: str, state_relative_path: str) -> bytes:
        raise OSError("the workspace volume is not answering")

    io.read_state_file = unreadable  # type: ignore[method-assign]

    await apply_queued_binding_switch(storage_provider=provider, workspace_io=io, session_id=SID)

    row = await sessions.get(SID)
    assert io.lines == [] and row.last_seq == 6
    assert row.pending_binding_switch == _switch_to("agent-b") and row.binding.agent_id == "agent-a"
