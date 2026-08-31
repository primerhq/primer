"""Journey: switching which agent runs a session (S1 P3 Task 18).

Ported from tests/e2e/test_chat_agent_switch_journey.py. The shape
carries over, but the meaning is stronger: on chats the agent was a
mutable field, while on sessions a switch is a versioned event with an
epoch and an attribution marker that lands IN the transcript between
the two agents' turns.

The queued case is the one chats had no analogue for: a switch asked
for mid-turn does not touch the binding until the drain checkpoint, so
the running turn finishes under the agent it started with.

In-process app with fake storage; no live server. PRIMER_RUN_E2E=1
lifts the default skip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.api.conftest import fake_provider_registry  # noqa: F401
from tests.conftest import _FakeStorageProvider  # noqa: F401

WID = "ws-switch"


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

    def read(self, path: str) -> str:
        return self._files.get(path, b"").decode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry) -> FastAPI:
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=False,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:  # noqa: BLE001
            pass
        yield c


def _rec(seq: int, kind: str, **payload) -> str:
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-17T00:00:00+00:00"})


_FIRST_TURN = [
    _rec(1, "user_input", text="ask agent a"),
    _rec(2, "assistant_token", text="answer from a"),
    _rec(3, "done"),
]


async def _seed(app: FastAPI, sid: str, **over) -> _FakeWorkspace:
    sp = app.state.storage_provider
    for aid in ("agent-a", "agent-b"):
        if await sp.get_storage(Agent).get(aid) is None:
            await sp.get_storage(Agent).create(
                Agent(id=aid, description=aid,
                      model=AgentModel(profile_id="p--m"), tools=[],
                      system_prompt=[]),
            )
    fields = {
        "id": sid, "workspace_id": WID,
        "binding": AgentSessionBinding(agent_id="agent-a"),
        "status": SessionStatus.WAITING, "created_at": datetime.now(UTC),
        "turn_status": "idle", "last_seq": 3,
    }
    fields.update(over)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(**fields))

    ws = _FakeWorkspace()
    ws.write(f".state/sessions/{sid}/messages.jsonl",
             "\n".join(_FIRST_TURN) + "\n")

    async def _get(wid):
        return ws if wid == WID else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


async def _row(app: FastAPI, sid: str) -> WorkspaceSession:
    return await app.state.storage_provider.get_storage(
        WorkspaceSession
    ).get(sid)


def _records(ws: _FakeWorkspace, sid: str) -> list[dict]:
    raw = ws.read(f".state/sessions/{sid}/messages.jsonl")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@pytest.mark.asyncio
class TestSessionBindingSwitchJourney:
    async def test_marker_lands_between_the_two_agents_turns(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The transcript keeps both turns and records the hand-off."""
        ws = await _seed(app, "sw-1")
        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/sw-1/binding",
            json={"kind": "agent", "agent_id": "agent-b"},
        )
        assert r.status_code == 200, r.text

        records = _records(ws, "sw-1")
        kinds = [rec["kind"] for rec in records]
        assert kinds == [
            "user_input", "assistant_token", "done", "agent_marker",
        ]

        marker = records[-1]
        assert marker["payload"]["from_binding"]["agent_id"] == "agent-a"
        assert marker["payload"]["to_binding"]["agent_id"] == "agent-b"
        assert marker["payload"]["binding_epoch"] == 1
        assert marker["seq"] == 4

    async def test_the_next_turn_would_run_under_the_new_binding(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "sw-2")
        await client.post(
            f"/v1/workspaces/{WID}/sessions/sw-2/binding",
            json={"kind": "agent", "agent_id": "agent-b"},
        )
        fresh = await _row(app, "sw-2")
        assert fresh.binding.agent_id == "agent-b"
        assert fresh.binding_epoch == 1
        assert fresh.pending_binding_switch is None

    async def test_a_mid_turn_switch_does_not_disturb_the_running_turn(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """What chats had no analogue for."""
        ws = await _seed(app, "sw-3", turn_status="running",
                         status=SessionStatus.RUNNING)
        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/sw-3/binding",
            json={"kind": "agent", "agent_id": "agent-b"},
        )
        assert r.status_code == 200, r.text

        fresh = await _row(app, "sw-3")
        assert fresh.binding.agent_id == "agent-a"  # still the running one
        assert fresh.binding_epoch == 0
        assert fresh.pending_binding_switch["agent_id"] == "agent-b"
        # Nothing written to the transcript yet: the marker belongs at
        # the checkpoint, after the turn's own terminal record.
        assert [rec["kind"] for rec in _records(ws, "sw-3")] == [
            "user_input", "assistant_token", "done",
        ]

    async def test_switching_back_bumps_the_epoch_again(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Epochs count switches, not distinct agents."""
        ws = await _seed(app, "sw-4")
        for target in ("agent-b", "agent-a"):
            r = await client.post(
                f"/v1/workspaces/{WID}/sessions/sw-4/binding",
                json={"kind": "agent", "agent_id": target},
            )
            assert r.status_code == 200, r.text

        fresh = await _row(app, "sw-4")
        assert fresh.binding.agent_id == "agent-a"
        assert fresh.binding_epoch == 2

        markers = [rec for rec in _records(ws, "sw-4")
                   if rec["kind"] == "agent_marker"]
        assert [m["payload"]["binding_epoch"] for m in markers] == [1, 2]
