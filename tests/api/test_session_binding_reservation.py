"""The binding route does not switch over a steer that landed after its read (S2a PR-12a, plan 3.8 A3).

The route's idle branch applied the switch from the row it read at the top, outside the lifecycle lock: a steer that
landed in between (``wake_session`` appends USER_INPUT at ``last_seq + 1`` and arms a turn) had its seq reused by the
switch's marker and its ``last_seq`` / ``turn_status`` written back over. The companion file for the checkpoint caller is
``tests/session/test_binding_switch_reservation.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession


def _row(**over) -> WorkspaceSession:
    fields = dict(
        id="b-race", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=datetime.now(UTC), turn_status="idle", last_seq=6,
    )
    fields.update(over)
    return WorkspaceSession(**fields)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        return self._files[path]

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line

    def records(self, session_id: str) -> list[dict]:
        raw = self._files.get(f"{self.state_path}/sessions/{session_id}/messages.jsonl", b"").decode()
        return [json.loads(ln) for ln in raw.splitlines() if ln.strip()]


@pytest.mark.asyncio
async def test_the_route_does_not_switch_over_a_steer_that_landed_after_its_read(client, app):
    from primer.model.agent import Agent, AgentModel

    sp = app.state.storage_provider
    for aid in ("agent-a", "agent-b"):
        await sp.get_storage(Agent).create(Agent(id=aid, description=aid, model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_row())
    ws = _FakeWorkspace()

    async def _get_ws(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get_ws  # type: ignore[assignment]

    real_get = sessions.get
    landed = {"done": False}

    async def racing_get(session_id):
        row = await real_get(session_id)
        if session_id == "b-race" and not landed["done"]:
            landed["done"] = True
            seq = row.last_seq + 1
            await ws.append_message_line("b-race", json.dumps({"seq": seq, "kind": "user_input", "payload": {"text": "a steer"}}).encode() + b"\n")
            await sessions.update(row.model_copy(update={"last_seq": seq, "turn_status": "claimable"}))
        return row

    sessions.get = racing_get  # type: ignore[method-assign]

    response = await client.post(
        "/v1/workspaces/ws-1/sessions/b-race/binding", json={"kind": "agent", "agent_id": "agent-b"},
    )

    seqs = [r["seq"] for r in ws.records("b-race") if "seq" in r]
    assert len(seqs) == len(set(seqs)), f"the switch's marker reused a seq in messages.jsonl: {seqs}"
    row = await real_get("b-race")
    assert row.turn_status == "claimable" and row.last_seq == max(seqs), "the route's write erased the steer's turn or its seq"
    assert response.status_code == 409, f"the route answered {response.status_code} for a switch it could not apply: {response.text}"
    assert row.binding.agent_id == "agent-a"


async def _idle_session(app, sid: str, **over):
    from primer.model.agent import Agent, AgentModel

    sp = app.state.storage_provider
    for aid in ("agent-a", "agent-b", "agent-c"):
        if await sp.get_storage(Agent).get(aid) is None:
            await sp.get_storage(Agent).create(
                Agent(id=aid, description=aid, model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[])
            )
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_row(id=sid, **over))
    ws = _FakeWorkspace()

    async def _get_ws(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get_ws  # type: ignore[assignment]
    return sessions, ws


@pytest.mark.asyncio
async def test_the_route_switches_under_the_lifecycle_lock(client, app):
    """The idle branch takes THIS session's lifecycle lock before it reads, reserves or writes anything.

    Deterministic on purpose: the route is seen WAITING on the key (the lock's reference count for the session id reaches two: the
    test's hold and the route's wait), so a route that takes a different key, or none, fails here at once instead of passing a
    sleep. While the test holds the lock nothing is written (no marker, ``last_seq`` unchanged); once it lets go, the route finishes.
    """
    from primer.session.mutation_lock import session_lifecycle_lock

    sessions, ws = await _idle_session(app, "b-lock")
    before = (await sessions.get("b-lock")).last_seq
    lock = session_lifecycle_lock()

    async with asyncio.timeout(10.0):
        async with lock.acquire("b-lock"):
            request = asyncio.create_task(
                client.post("/v1/workspaces/ws-1/sessions/b-lock/binding", json={"kind": "agent", "agent_id": "agent-b"})
            )
            while lock._refs.get("b-lock", 0) < 2:  # noqa: SLF001 - the reference count is how a waiter is observed
                assert not request.done(), "the route finished without ever waiting for the session's lifecycle lock"
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)  # a route that is NOT really blocked would have written by now
            assert not request.done() and ws.records("b-lock") == [], "the route switched while another writer held the lock"
            assert (await sessions.get("b-lock")).last_seq == before, "the route reserved a seq while another writer held the lock"
        response = await request

    assert response.status_code == 200, response.text
    assert [r["kind"] for r in ws.records("b-lock")] == ["agent_marker"]
    stored = await sessions.get("b-lock")
    assert stored.binding.agent_id == "agent-b" and stored.last_seq == before + 1


@pytest.mark.asyncio
async def test_an_unreachable_workspace_gives_a_409_and_frees_the_lock(client, app, monkeypatch):
    import primer.session.mutation_lock as mutation_lock

    monkeypatch.setattr(mutation_lock, "IN_LOCK_IO_TIMEOUT_S", 0.2)
    sessions, ws = await _idle_session(app, "b-hang")

    async def hang(session_id: str, line: bytes) -> None:
        await asyncio.Event().wait()

    ws.append_message_line = hang  # type: ignore[method-assign]

    response = await asyncio.wait_for(
        client.post("/v1/workspaces/ws-1/sessions/b-hang/binding", json={"kind": "agent", "agent_id": "agent-b"}), 3.0,
    )

    assert response.status_code == 409, response.text
    async with asyncio.timeout(1.0):
        async with mutation_lock.session_lifecycle_lock().acquire("b-hang"):
            pass
    stored = await sessions.get("b-hang")
    assert stored.binding.agent_id == "agent-a" and stored.binding_epoch == 0


@pytest.mark.asyncio
async def test_a_timeout_after_the_gate_was_closed_says_the_gate_is_closed(client, app, monkeypatch):
    """The abandon-then-switch branch closes the gate FIRST. If the marker then times out, the 409 must not read like a no-op: the
    parked turn is already rejected and cannot be resumed, only the switch is outstanding."""
    import primer.session.mutation_lock as mutation_lock

    monkeypatch.setattr(mutation_lock, "IN_LOCK_IO_TIMEOUT_S", 0.2)
    sessions, ws = await _idle_session(
        app, "b-gate", parked_status="parked", parked_state={"tool_call_id": "tc-9", "mode": "ask_user"},
    )
    real_append = ws.append_message_line

    async def hang_on_the_marker(session_id: str, line: bytes) -> None:
        if b'"agent_marker"' in line:
            await asyncio.Event().wait()
        await real_append(session_id, line)

    ws.append_message_line = hang_on_the_marker  # type: ignore[method-assign]

    response = await asyncio.wait_for(
        client.post("/v1/workspaces/ws-1/sessions/b-gate/binding", json={"kind": "agent", "agent_id": "agent-b"}), 3.0,
    )

    assert response.status_code == 409, response.text
    assert "gate is already closed" in response.json()["detail"], response.text
    stored = await sessions.get("b-gate")
    assert stored.parked_status is None, "the precondition of the message: the gate really was closed"
    assert stored.binding.agent_id == "agent-a" and stored.binding_epoch == 0


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="S2a PR-12b: the busy-queue branch of switch_session_binding still writes the WHOLE row, outside the lifecycle lock, "
           "from a row read before it, so it can regress last_seq below a marker the checkpoint just wrote",
)
@pytest.mark.asyncio
async def test_the_busy_queue_write_does_not_regress_last_seq_below_a_marker_the_checkpoint_wrote(client, app):
    """The declared residual of PR-12a, as an executable witness (it flips when PR-12b makes the queue write a patch_if).

    A busy session (a turn claimable or running) with a switch A queued. POST /binding B reads the row, then the checkpoint applies A
    under the lock (reserves seq 7, writes the marker at 7, closes with last_seq 7), then the route writes its stale row back whole:
    last_seq is 6 again and A's binding and epoch are erased. The next record any writer appends takes seq 7, the marker's seq.
    """
    from primer.session.dispatch import apply_queued_binding_switch

    request_a = {"kind": "agent", "agent_id": "agent-b", "graph_id": None, "profile_id": None, "actor": "user"}
    sessions, ws = await _idle_session(app, "b-queue", turn_status="claimable", pending_binding_switch=request_a)
    real_get = sessions.get
    ran = {"done": False}

    async def checkpoint_after_the_route_read(session_id):
        row = await real_get(session_id)
        if session_id == "b-queue" and not ran["done"]:
            ran["done"] = True
            await apply_queued_binding_switch(storage_provider=app.state.storage_provider, workspace_io=ws, session_id=session_id)
        return row

    sessions.get = checkpoint_after_the_route_read  # type: ignore[method-assign]

    response = await client.post("/v1/workspaces/ws-1/sessions/b-queue/binding", json={"kind": "agent", "agent_id": "agent-c"})

    assert response.status_code == 200, response.text
    markers = [r for r in ws.records("b-queue") if r["kind"] == "agent_marker"]
    assert len(markers) == 1, "precondition: the checkpoint applied switch A and wrote its marker"
    row = await real_get("b-queue")
    assert row.last_seq >= markers[0]["seq"], (
        f"last_seq is {row.last_seq} but the log already holds a marker at seq {markers[0]['seq']}: the next record collides with it"
    )
