"""S7 section 6: GET /v1/sessions/{id}/turns/{n}/timeline.

Pure derivation over the on-disk record, so it works on any historical
session with no new write path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]


@pytest.fixture
async def timeline_setup(app, fake_storage_provider):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id="sess-tl-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)

    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/sess-tl-1/messages.jsonl",
        "\n".join([
            '{"seq":1,"kind":"user_input","payload":{"text":"hi"},'
            '"created_at":"2026-08-16T10:00:00+00:00"}',
            '{"seq":2,"kind":"llm_call","payload":{"profile_id":"prof-1",'
            '"provider_id":"prov-1","model":"m-1","input_tokens":11,'
            '"output_tokens":7,"duration_ms":900,"status":"ok"},'
            '"created_at":"2026-08-16T10:00:01+00:00"}',
            '{"seq":3,"kind":"done","payload":{"stop_reason":"stop"},'
            '"created_at":"2026-08-16T10:00:02+00:00"}',
            '{"seq":4,"kind":"user_input","payload":{"text":"again"},'
            '"created_at":"2026-08-16T10:00:03+00:00"}',
            '{"seq":5,"kind":"done","payload":{"stop_reason":"stop"},'
            '"created_at":"2026-08-16T10:00:04+00:00"}',
        ]) + "\n",
    )
    ws.write(
        ".state/sessions/sess-tl-1/turns.jsonl",
        '{"seq":1,"kind":"started","ts":"2026-08-16T10:00:00+00:00","turn_no":0}\n'
        '{"seq":2,"kind":"completed","ts":"2026-08-16T10:00:02+00:00",'
        '"turn_no":0,"duration_ms":2000,"finish_reason":"stop"}\n',
    )

    async def _get(workspace_id: str):
        return ws if workspace_id == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return sess, ws


@pytest.mark.asyncio
async def test_timeline_returns_the_turn_tree(
    client: httpx.AsyncClient, timeline_setup,
):
    r = await client.get("/v1/sessions/sess-tl-1/turns/0/timeline")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "sess-tl-1"
    assert body["turn_no"] == 0
    assert body["status"] == "completed"
    assert body["duration_ms"] == 2000
    assert [c["kind"] for c in body["children"]] == ["llm_call"]
    assert body["children"][0]["profile_id"] == "prof-1"


@pytest.mark.asyncio
async def test_second_turn_is_addressable(
    client: httpx.AsyncClient, timeline_setup,
):
    r = await client.get("/v1/sessions/sess-tl-1/turns/1/timeline")
    assert r.status_code == 200, r.text
    assert r.json()["turn_no"] == 1


@pytest.mark.asyncio
async def test_out_of_range_turn_is_404(
    client: httpx.AsyncClient, timeline_setup,
):
    r = await client.get("/v1/sessions/sess-tl-1/turns/9/timeline")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unknown_session_is_404(client: httpx.AsyncClient):
    r = await client.get("/v1/sessions/sess-missing/turns/0/timeline")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_missing_workspace_is_404(
    client: httpx.AsyncClient, timeline_setup, app,
):
    async def _none(workspace_id: str):
        return None

    app.state.workspace_registry.get_workspace = _none  # type: ignore[assignment]
    r = await client.get("/v1/sessions/sess-tl-1/turns/0/timeline")
    assert r.status_code == 404
