"""REST tests for the `usage` / `context_length` fields on
GET /v1/sessions/{id} (01a052a5 item 2 - the console's context meter).

Both fields were computed but never served: session_usage() (primer/
session/usage.py) only had one caller (tap.py's build_usage_frame, itself
never called from anywhere), and ModelProfile.context_length was read
only internally by compaction. Mirrors test_session_messages_route.py's
fake-workspace-with-read_file pattern for the log read, and seeds a real
ModelProfile row (same resolve_model() seam compact_session_endpoint
uses) for the context_length resolution.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


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


async def _seed_session(fake_storage_provider, sid: str, *, binding=None):
    from primer.model.workspace_session import (
        AgentSessionBinding, SessionStatus, WorkspaceSession,
    )
    sess = WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=binding or AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=_now(), turn_status="idle",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)


async def _seed_agent_and_profile(
    fake_storage_provider, *, agent_id="ag1", profile_id="mp-1",
    context_length=128_000, model_name="scripted-model",
):
    from primer.model.agent import Agent, AgentModel
    from primer.model.model_profile import ModelProfile

    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id=agent_id, description="test agent",
            model=AgentModel(profile_id=profile_id), tools=[], system_prompt=[],
        )
    )
    await fake_storage_provider.get_storage(ModelProfile).create(
        ModelProfile(
            id=profile_id, description="test profile",
            provider_id="prov-1", model_name=model_name,
            context_length=context_length,
        )
    )


_DONE_LINES = (
    '{"seq":1,"kind":"user_input","payload":{"text":"hi"}}\n'
    '{"seq":2,"kind":"done","payload":{"usage":{'
    '"input_tokens":1000,"output_tokens":500}}}\n'
)


@pytest.mark.asyncio
async def test_usage_and_context_length_populated(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    await _seed_session(fake_storage_provider, "s-1")
    await _seed_agent_and_profile(fake_storage_provider, context_length=64_000)
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-1/messages.jsonl", _DONE_LINES)

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usage"]["total_input_tokens"] == 1000
    assert body["usage"]["total_output_tokens"] == 500
    assert body["usage"]["turns"] == 1
    assert body["context_length"] == 64_000


@pytest.mark.asyncio
async def test_usage_null_when_log_unreadable(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """No messages.jsonl yet (a session that has not started) must not
    502/500 the whole detail read - the row is still the answer."""
    await _seed_session(fake_storage_provider, "s-2")
    await _seed_agent_and_profile(fake_storage_provider)

    async def _get(wid):
        return None  # workspace itself unavailable
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-2")
    assert r.status_code == 200, r.text
    assert r.json()["usage"] is None


@pytest.mark.asyncio
async def test_context_length_null_for_graph_binding(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A graph-bound session has no single model to report - compaction
    itself refuses these the same way (guard_compactable's 409)."""
    from primer.model.workspace_session import GraphSessionBinding

    await _seed_session(
        fake_storage_provider, "s-3",
        binding=GraphSessionBinding(graph_id="g-1"),
    )
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-3/messages.jsonl", _DONE_LINES)

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-3")
    assert r.status_code == 200, r.text
    assert r.json()["context_length"] is None
    # usage is independent of the binding kind - still populated.
    assert r.json()["usage"]["total_input_tokens"] == 1000


@pytest.mark.asyncio
async def test_context_length_null_when_profile_deleted(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """The agent names a profile that no longer exists - resolve_model()
    raises NotFoundError; the detail read still succeeds with a null."""
    from primer.model.agent import Agent, AgentModel

    await _seed_session(fake_storage_provider, "s-4")
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="ag1", description="test agent",
            model=AgentModel(profile_id="mp-gone"), tools=[], system_prompt=[],
        )
    )
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-4/messages.jsonl", _DONE_LINES)

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-4")
    assert r.status_code == 200, r.text
    assert r.json()["context_length"] is None


@pytest.mark.asyncio
async def test_context_length_null_for_the_legacy_seeded_value(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """Dogfood round 2: a profile whose stored context_length is exactly
    the old discovery-seeded default (providers.py's now-removed
    _DEFAULT_LLM_CONTEXT_LENGTH) is indistinguishable from "an operator
    genuinely typed 32000" - the honest move is to treat it as unknown
    rather than serve it as fact, which is what shipped a real user a
    confident-looking wrong meter denominator."""
    await _seed_session(fake_storage_provider, "s-6")
    await _seed_agent_and_profile(
        fake_storage_provider, context_length=32_000, model_name="llm-openchat-x",
    )
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-6/messages.jsonl", _DONE_LINES)

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-6")
    assert r.status_code == 200, r.text
    assert r.json()["context_length"] is None
    # usage is unaffected - it does not go through this precedence rule.
    assert r.json()["usage"]["total_input_tokens"] == 1000


@pytest.mark.asyncio
async def test_context_length_prefers_known_model_fallback_over_a_stored_value(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    """A curated known-model value (primer/agent/compaction.py's
    MODEL_CONTEXT_FALLBACK) is real by construction and always wins,
    even over a stored value that happens to differ (e.g. a profile
    someone hand-edited to something wrong, or the legacy seed itself -
    this table lookup is checked BEFORE the legacy-seed check)."""
    await _seed_session(fake_storage_provider, "s-7")
    await _seed_agent_and_profile(
        fake_storage_provider, context_length=32_000, model_name="gpt-4o",
    )
    ws = _FakeWorkspace()
    ws.write(".state/sessions/s-7/messages.jsonl", _DONE_LINES)

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-7")
    assert r.status_code == 200, r.text
    assert r.json()["context_length"] == 128_000
