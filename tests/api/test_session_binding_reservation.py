"""The binding route does not switch over a steer that landed after its read (S2a PR-12a, plan 3.8 A3).

The route's idle branch applied the switch from the row it read at the top, outside the lifecycle lock: a steer that
landed in between (``wake_session`` appends USER_INPUT at ``last_seq + 1`` and arms a turn) had its seq reused by the
switch's marker and its ``last_seq`` / ``turn_status`` written back over. The companion file for the checkpoint caller is
``tests/session/test_binding_switch_reservation.py``.
"""

from __future__ import annotations

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
