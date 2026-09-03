"""SteerBody.attachments -- workspace-file vision/document attachments.

Resolution path: steer_session -> media_from_workspace_files (same
artifact-backed-Part pipeline ask_user/inform_user already use for
outbound files) -> wake_session(extra_parts=...) ->
AgentSession.append_instruction(extra_parts=...). This module proves the
wire-layer validation (traversal/absolute-path rejection, attachments-
without-instruction rejection) and the resolve-and-fold behaviour through
the real HTTP endpoint, using the same _FakeBackend harness
test_external_tools_steer.py established.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.chat import DocumentPart, ImagePart
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.api.test_workspaces import _FakeBackend, _SP, _provider, _template


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def pr(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def wsr(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_FakeBackend)


@pytest.fixture
async def app(sp, pr, wsr):
    _app = create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        workspace_registry=wsr,
    )
    # Seed the reserved default artifact provider (parity with the
    # lifespan / tests/api/conftest.py's shared app fixture) -- without
    # this, ArtifactStorageRegistry.get_default() has no row to resolve
    # and media_from_workspace_files can never be reached.
    if getattr(_app.state, "seed_artifact_default", None) is not None:
        await _app.state.seed_artifact_default()
    return _app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


async def _setup_ws(client, wsr) -> str:
    await client.post(
        "/v1/workspace_providers", json=_provider().model_dump(mode="json")
    )
    await client.post(
        "/v1/workspace_templates", json=_template().model_dump(mode="json")
    )
    post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
    wid = post.json()["id"]
    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    ws.add_session("sess-1", "agent-att")
    return wid


async def _seed_agent(sp) -> None:
    await sp.get_storage(Agent).create(
        Agent(id="agent-att", description="d", model=AgentModel(profile_id="prof-1"))
    )


async def _seed_session(sp, wid: str) -> WorkspaceSession:
    now = datetime.now(UTC)
    row = WorkspaceSession(
        id="sess-1",
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="agent-att"),
        status=SessionStatus.RUNNING,
        created_at=now,
        started_at=now,
    )
    await sp.get_storage(WorkspaceSession).create(row)
    return row


async def _upload(client, wid: str, path: str, data: bytes) -> None:
    r = await client.put(
        f"/v1/workspaces/{wid}/files",
        params={"path": path},
        json={
            "content": base64.b64encode(data).decode(),
            "encoding": "base64",
        },
    )
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_attachment_resolves_and_folds_into_message(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)
    await _upload(client, wid, "uploads/pic.png", b"PNGBYTES")

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "look at this", "attachments": [{"path": "uploads/pic.png"}]},
    )
    assert r.status_code == 200, r.text

    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    slot = await ws.get_session("sess-1")
    assert slot.appended == ["look at this"]
    parts = slot.appended_extra_parts
    assert parts is not None and len(parts) == 1
    assert isinstance(parts[0], ImagePart)
    # media_from_workspace_files returns artifact-referenced parts (no
    # inline data): see test_attachment_is_artifact_referenced_not_inline
    # for the dedicated assertion on that shape.


@pytest.mark.asyncio
async def test_attachment_is_artifact_referenced_not_inline(client, wsr, sp):
    """media_from_workspace_files stores bytes and returns an
    artifact_id-only Part (no inline data) -- messages.jsonl / the
    session's Message must never carry raw attachment bytes."""
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)
    await _upload(client, wid, "uploads/pic.png", b"PNGBYTES")

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "look", "attachments": [{"path": "uploads/pic.png"}]},
    )
    assert r.status_code == 200, r.text

    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    slot = await ws.get_session("sess-1")
    part = slot.appended_extra_parts[0]
    assert part.artifact_id is not None
    assert part.data is None


@pytest.mark.asyncio
async def test_document_mime_maps_to_document_part(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)
    await _upload(client, wid, "uploads/report.pdf", b"%PDF-1.4")

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "read this", "attachments": [{"path": "uploads/report.pdf"}]},
    )
    assert r.status_code == 200, r.text

    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    slot = await ws.get_session("sess-1")
    assert isinstance(slot.appended_extra_parts[0], DocumentPart)


@pytest.mark.asyncio
async def test_attachment_traversal_escape_is_422(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={
            "instruction": "go",
            "attachments": [{"path": "../../../etc/passwd"}],
        },
    )
    assert r.status_code == 422, r.text
    assert ".." in r.text or "escape" in r.text.lower()

    # Never resolved outside the workspace root: the fake session's
    # append_instruction (which would only run past validation) was
    # never reached.
    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    slot = await ws.get_session("sess-1")
    assert slot.appended == []


@pytest.mark.asyncio
async def test_attachment_absolute_path_is_422(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "go", "attachments": [{"path": "/etc/passwd"}]},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_attachments_without_instruction_is_422(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"attachments": [{"path": "uploads/pic.png"}]},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_missing_attachment_file_is_dropped_not_failed(client, wsr, sp):
    """Best-effort, matching media_from_workspace_files' own tolerance:
    a nonexistent workspace file drops the attachment with a log rather
    than failing the whole steer."""
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp)
    await _seed_session(sp, wid)

    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "go", "attachments": [{"path": "uploads/nope.png"}]},
    )
    assert r.status_code == 200, r.text

    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    slot = await ws.get_session("sess-1")
    assert slot.appended == ["go"]
    assert not slot.appended_extra_parts
