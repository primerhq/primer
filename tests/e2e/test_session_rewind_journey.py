"""Journey: session rewind and its compaction guard (S1 P2 Task 16).

Ported from tests/e2e/test_chat_rewind_journey.py. The transport moves
from chats to workspace sessions, and one behaviour genuinely changes:
chat rewind DELETED rows, session rewind appends a marker and lets the
read-time replay walk drop them. The log stays append-only, so the
audit trail survives a cut.

The compaction case is the one amendment C2 exists for: a rewind whose
target lies in a folded span is refused, because the folded rows are
gone from the visible set and the summary that replaced them would drop
too, leaving the next turn to rebuild from nothing.

Runs against the in-process app with fake storage, like the chat
original: no live server, docker or postgres. PRIMER_RUN_E2E=1 lifts
the default skip in tests/e2e/conftest.py.
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

AGENT_ID = "ag-session-rewind"
WID = "ws-rewind"


class _FakeWorkspace:
    """Minimal workspace: the read surface plus the append the writer needs."""

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
                       "created_at": "2026-08-16T00:00:00+00:00"})


async def _seed(app: FastAPI, sid: str, *, lines: list[str], **over):
    await app.state.storage_provider.get_storage(Agent).create(
        Agent(id=AGENT_ID, description="rewind journey",
              model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]),
    )
    fields = {
        "id": sid, "workspace_id": WID,
        "binding": AgentSessionBinding(agent_id=AGENT_ID),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "turn_status": "idle",
        # Message lines are seqless, so only record lines count here.
        "last_seq": max(
            (json.loads(line).get("seq", 0) for line in lines),
            default=0,
        ),
    }
    fields.update(over)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        WorkspaceSession(**fields)
    )
    ws = _FakeWorkspace()
    ws.write(f".state/sessions/{sid}/messages.jsonl", "\n".join(lines) + "\n")

    async def _get(wid):
        return ws if wid == WID else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


_PLAIN = [
    _rec(1, "user_input", text="first"),
    _rec(2, "assistant_token", text="ok"),
    _rec(3, "done"),
    _rec(4, "user_input", text="second"),
    _rec(5, "done"),
]


@pytest.mark.asyncio
class TestSessionRewindJourney:
    async def test_rewind_appends_and_discards_nothing_from_the_log(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The headline difference from the chat original."""
        ws = await _seed(app, "s-rw", lines=_PLAIN)

        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/s-rw/rewind", json={"to_seq": 1},
        )
        assert r.status_code == 200, r.text
        assert r.json()["to_seq"] == 1
        assert r.json()["marker_seq"] == 6

        written = ws.read(".state/sessions/s-rw/messages.jsonl")
        assert "rewind_marker" in written
        # Every original row is still on disk: nothing was deleted.
        for seq in (1, 2, 3, 4, 5):
            assert f'"seq": {seq}' in written or f'"seq":{seq}' in written

        fresh = await app.state.storage_provider.get_storage(
            WorkspaceSession
        ).get("s-rw")
        assert fresh.last_seq == 6

    async def test_rewound_history_is_what_the_next_turn_would_rebuild(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The marker has to actually change the reconstructed prompt."""
        from primer.workspace.session import reconstruct_compacted_history

        ws = await _seed(app, "s-rw2", lines=[
            _rec(1, "user_input", text="keep"),
            json.dumps({"role": "user", "parts": [
                {"type": "text", "text": "keep"}]}),
            _rec(2, "done"),
            _rec(3, "user_input", text="drop"),
            json.dumps({"role": "user", "parts": [
                {"type": "text", "text": "drop"}]}),
            _rec(4, "done"),
        ])

        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/s-rw2/rewind", json={"to_seq": 1},
        )
        assert r.status_code == 200, r.text

        lines = ws.read(".state/sessions/s-rw2/messages.jsonl").splitlines()
        texts = [p.text for m in reconstruct_compacted_history(lines)
                 for p in m.parts]
        assert texts == ["keep"]

    async def test_rewind_into_a_compacted_span_is_refused(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Amendment C2, end to end: refused, and the log is untouched."""
        ws = await _seed(app, "s-rw3", lines=[
            *_PLAIN,
            _rec(6, "compaction_marker", summary="so far", replaced_to_seq=5),
            _rec(7, "user_input", text="after the fold"),
            _rec(8, "done"),
        ])
        before = ws.read(".state/sessions/s-rw3/messages.jsonl")

        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/s-rw3/rewind", json={"to_seq": 1},
        )
        assert r.status_code == 409, r.text
        assert ws.read(".state/sessions/s-rw3/messages.jsonl") == before

        # A target after the fold is still legal.
        ok = await client.post(
            f"/v1/workspaces/{WID}/sessions/s-rw3/rewind", json={"to_seq": 7},
        )
        assert ok.status_code == 200, ok.text

    async def test_running_turn_is_refused(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        ws = await _seed(app, "s-rw4", lines=_PLAIN, turn_status="running")
        before = ws.read(".state/sessions/s-rw4/messages.jsonl")

        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/s-rw4/rewind", json={"to_seq": 1},
        )
        assert r.status_code == 409, r.text
        assert ws.read(".state/sessions/s-rw4/messages.jsonl") == before

    async def test_unknown_session_404s(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "s-rw5", lines=_PLAIN)
        r = await client.post(
            f"/v1/workspaces/{WID}/sessions/nope/rewind", json={"to_seq": 1},
        )
        assert r.status_code == 404, r.text
