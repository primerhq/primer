"""Journey: session compaction (S1 P2 Task 16).

Ported from tests/e2e/test_chat_compact_journey.py. Compaction stays
append-only on sessions: the marker carries the summary and the span it
replaces, and the read-time walk folds the rows before it, so the event
log survives while the prompt shrinks.

The happy path drives a stub LLM through the real force_compact path,
then rebuilds the history through the real reader, because a marker
that does not change what the next turn sees would be worthless.

In-process app with fake storage; no live server. PRIMER_RUN_E2E=1
lifts the default skip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.api.conftest import fake_provider_registry  # noqa: F401
from tests.conftest import _FakeStorageProvider  # noqa: F401

AGENT_ID = "ag-session-compact"
WID = "ws-compact"


class _StubLLM:
    """Enough LLM for force_compact: a token count and a stream."""

    def __init__(self, summary: str = "rolled up") -> None:
        self._summary = summary
        self.calls: list[dict[str, Any]] = []

    async def count_tokens(self, *args, **kwargs) -> int:
        return 10_000

    def stream(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": list(messages)})
        return self._stream_impl()

    async def _stream_impl(self):
        yield TextDelta(index=0, text=self._summary)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


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
                       "created_at": "2026-08-16T00:00:00+00:00"})


def _msg(role: str, text: str) -> str:
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


_LOG = [
    _rec(1, "user_input", text="hello"),
    _msg("user", "hello"),
    _rec(2, "assistant_token", text="hi back"),
    _msg("assistant", "hi back"),
    _rec(3, "done"),
]


async def _seed(app: FastAPI, sid: str, *, binding=None, lines=None, **over):
    sp = app.state.storage_provider
    if await sp.get_storage(Agent).get(AGENT_ID) is None:
        await sp.get_storage(Agent).create(
            Agent(id=AGENT_ID, description="compact journey",
                  model=AgentModel(profile_id="p--m"), tools=[],
                  system_prompt=[]),
        )
    lines = lines if lines is not None else _LOG
    fields = {
        "id": sid, "workspace_id": WID,
        "binding": binding or AgentSessionBinding(agent_id=AGENT_ID),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "turn_status": "idle",
        "last_seq": max(
            (json.loads(line).get("seq", 0) for line in lines), default=0,
        ),
    }
    fields.update(over)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(**fields))

    ws = _FakeWorkspace()
    ws.write(f".state/sessions/{sid}/messages.jsonl", "\n".join(lines) + "\n")

    async def _get(wid):
        return ws if wid == WID else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


@pytest.mark.asyncio
class TestSessionCompactJourney:
    async def test_guards_reject_before_any_llm_work(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Every rejection happens before a provider is touched, so a
        misconfigured provider never masks a state error."""
        await _seed(app, "c-run", turn_status="running")
        r = await client.post(f"/v1/workspaces/{WID}/sessions/c-run/compact")
        assert r.status_code == 409, r.text

    async def test_parked_session_is_refused(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "c-parked", parked_status="parked")
        r = await client.post(f"/v1/workspaces/{WID}/sessions/c-parked/compact")
        assert r.status_code == 409, r.text

    async def test_graph_binding_is_refused(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Graph internals see graph state, not session history."""
        await _seed(app, "c-graph", binding=GraphSessionBinding(graph_id="g1"))
        r = await client.post(f"/v1/workspaces/{WID}/sessions/c-graph/compact")
        assert r.status_code == 409, r.text

    async def test_unknown_session_404s(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "c-x")
        r = await client.post(f"/v1/workspaces/{WID}/sessions/nope/compact")
        assert r.status_code == 404, r.text

    async def test_compaction_folds_the_history_the_next_turn_rebuilds(
        self, client: AsyncClient, app: FastAPI, monkeypatch,
    ) -> None:
        """The point of the marker: it changes the reconstructed prompt."""
        from primer.model_profile.resolver import ResolvedModel
        from primer.workspace.session import reconstruct_compacted_history

        ws = await _seed(app, "c-ok")
        stub = _StubLLM(summary="the story so far")

        async def _get_llm(_provider_id):
            return stub

        app.state.provider_registry.get_llm = _get_llm  # type: ignore[assignment]

        async def _resolve(*_a, **_k):
            return ResolvedModel(
                profile_id="p--m", provider_id="prov", model_name="m",
                context_length=128_000, config={},
            )

        monkeypatch.setattr(
            "primer.model_profile.resolve_model", _resolve, raising=False,
        )

        r = await client.post(f"/v1/workspaces/{WID}/sessions/c-ok/compact")
        assert r.status_code == 200, r.text
        body = r.json()
        # force_compact stamps the summary so a reader can tell a folded
        # head from ordinary assistant prose.
        assert "the story so far" in body["summary"]
        assert "earlier conversation compacted on" in body["summary"]
        assert body["compaction_marker_seq"] == 4

        lines = ws.read(".state/sessions/c-ok/messages.jsonl").splitlines()
        texts = [p.text for m in reconstruct_compacted_history(lines)
                 for p in m.parts]
        assert len(texts) == 1, "the fold must collapse the head to one row"
        assert "the story so far" in texts[0]
        # Append-only: the pre-compaction rows are all still on disk.
        assert "hi back" in ws.read(".state/sessions/c-ok/messages.jsonl")
