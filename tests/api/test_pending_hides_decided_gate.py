"""A pending read stops offering a gate as soon as its decision is stamped on the row (ticket 01a16ca6).

A respond stamps the decision on the session row (``parked_status='resumable'`` plus the wake under the gate's own event key) and leaves the gate in
``parked_state`` until a worker claims the row. The pending reads used to keep serving the DECIDED gate in that window, so a poller answered it again:
409 ``already_decided`` while the row was still resumable, 404 ``gate_not_pending`` once a worker had claimed it (#877's e2e, run 40068966570).

These tests use the real routers and real SQLite storage, with no worker claiming the resumable row between the respond and the next read: the window a
poller used to see the decided gate in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from tests.api.test_gate_fence import G1, G2, _approval_session, _ask_user_session, _graph_two_gate_session
from tests.api.test_gate_fence_round2 import _graph_ask_user_session


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "pending-decided.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def sqlite_client(sqlite_provider: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    registry = ProviderRegistry(
        sqlite_provider,  # type: ignore[arg-type]
        llm_factory=lambda row: object(),
        embedder_factory=lambda row: object(),
        cross_encoder_factory=lambda row: object(),
        toolset_factory=lambda row: object(),
    )
    app = create_test_app(storage_provider=sqlite_provider, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post("/v1/auth/register", json={"username": "admin", "password": "testpassword"})
        assert registered.status_code in (200, 201), registered.text
        yield client


def _unparked_session(session_id: str) -> WorkspaceSession:
    """A session parked on nothing: what each pending route answers for it is "nothing pending"."""
    return WorkspaceSession(
        id=session_id, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    )


def _shape(response: httpx.Response) -> tuple[int, str | None, str | None]:
    """What a client branches on: the status, the problem type and the ``code`` extension (``gate_not_pending`` and friends)."""
    body = response.json()
    return response.status_code, body.get("type"), (body.get("extensions") or {}).get("code")


async def _assert_nothing_pending(client: httpx.AsyncClient, sessions, *, route: str, session_id: str) -> None:
    """``route`` answers for ``session_id`` exactly as it answers for a session that is parked on nothing (lead ruling 1)."""
    reference_id = f"{session_id}-unparked"
    if await sessions.get(reference_id) is None:
        await sessions.create(_unparked_session(reference_id))
    expected = _shape(await client.get(f"/v1/sessions/{reference_id}/{route}/pending"))
    assert expected[0] == 404 and expected[2] is None, expected
    response = await client.get(f"/v1/sessions/{session_id}/{route}/pending")
    assert _shape(response) == expected, response.text


async def _listed(client: httpx.AsyncClient, url: str, **params: str) -> list[tuple[str, str | None, str | None]]:
    response = await client.get(url, params=params)
    assert response.status_code == 200, response.text
    return [(item["session_id"], item["tool_call_id"], item["gate_id"]) for item in response.json()["items"]]


# ---- a single gate -----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_decided_approval_is_not_served_before_the_worker_claims(
    sqlite_provider: SqliteStorageProvider, sqlite_client: httpx.AsyncClient,
) -> None:
    sessions = sqlite_provider.get_storage(WorkspaceSession)
    await sessions.create(_approval_session(session_id="decided-approval", tool_call_id="call-0", gate_id=G1))

    response = await sqlite_client.post(
        "/v1/sessions/decided-approval/tool_approval/respond",
        json={"tool_call_id": "call-0", "gate_id": G1, "decision": "approved"},
    )

    assert response.status_code == 202, response.text
    row = await sessions.get("decided-approval")
    assert row is not None and row.parked_status == "resumable", "no worker has claimed the row: this is the window"
    await _assert_nothing_pending(sqlite_client, sessions, route="tool_approval", session_id="decided-approval")
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/sessions/decided-approval/yields/pending") == []
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/yields/pending") == []
    assert await _listed(sqlite_client, "/v1/yields/pending", workspace_id="ws-g") == []


@pytest.mark.asyncio
async def test_an_answered_question_is_absent_from_every_pending_read(
    sqlite_provider: SqliteStorageProvider, sqlite_client: httpx.AsyncClient,
) -> None:
    sessions = sqlite_provider.get_storage(WorkspaceSession)
    await sessions.create(_ask_user_session(session_id="answered-question", tool_call_id="ask-0", gate_id=G1))

    response = await sqlite_client.post(
        "/v1/sessions/answered-question/ask_user/respond",
        json={"tool_call_id": "ask-0", "gate_id": G1, "response": "EUR"},
    )

    assert response.status_code == 202, response.text
    row = await sessions.get("answered-question")
    assert row is not None and row.parked_status == "resumable"
    await _assert_nothing_pending(sqlite_client, sessions, route="ask_user", session_id="answered-question")
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/sessions/answered-question/yields/pending") == []
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/yields/pending") == []
    assert await _listed(sqlite_client, "/v1/yields/pending", workspace_id="ws-g") == []


# ---- a graph park with several gates -----------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("same_raw_id", [False, True], ids=["distinct-ids", "shared-raw-id"])
async def test_a_two_gate_park_still_serves_the_undecided_gate(
    sqlite_provider: SqliteStorageProvider, sqlite_client: httpx.AsyncClient, same_raw_id: bool,
) -> None:
    """Deciding the first gate of a two-gate graph park leaves the second one on offer, by its own gate id; deciding it too leaves nothing.

    ``shared-raw-id``: both gates carry the provider id "dup", so the decided one can only be told apart by its event key and gate id."""
    sessions = sqlite_provider.get_storage(WorkspaceSession)
    await sessions.create(_graph_two_gate_session(session_id="decided-sibling", same_raw_id=same_raw_id))
    first, second = ("dup", "dup") if same_raw_id else ("call-0", "call-1")

    response = await sqlite_client.post(
        "/v1/sessions/decided-sibling/tool_approval/respond",
        json={"tool_call_id": first, "gate_id": G1, "decision": "approved"},
    )

    assert response.status_code == 202, response.text
    assert (await sessions.get("decided-sibling")).parked_status == "resumable"
    pending = await sqlite_client.get("/v1/sessions/decided-sibling/tool_approval/pending")
    assert pending.status_code == 200, pending.text
    assert (pending.json()["tool_call_id"], pending.json()["gate_id"]) == (second, G2)
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/sessions/decided-sibling/yields/pending") == [
        ("decided-sibling", second, G2),
    ]
    # The workspace list and the Inbox read only a row that is still ``parked`` (one item per session, the primary gate): the decided gate is not
    # among them. They show the session again once the worker has re-parked it on the undecided gate.
    assert ("decided-sibling", first, G1) not in await _listed(sqlite_client, "/v1/workspaces/ws-g/yields/pending")
    assert ("decided-sibling", first, G1) not in await _listed(sqlite_client, "/v1/yields/pending", workspace_id="ws-g")

    # The gate on offer is a real one: deciding it is accepted, and then nothing is pending.
    response = await sqlite_client.post(
        "/v1/sessions/decided-sibling/tool_approval/respond",
        json={"tool_call_id": second, "gate_id": G2, "decision": "rejected"},
    )

    assert response.status_code == 202, response.text
    await _assert_nothing_pending(sqlite_client, sessions, route="tool_approval", session_id="decided-sibling")
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/sessions/decided-sibling/yields/pending") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["agent", "tool_call"])
async def test_a_graph_park_with_one_answered_question_serves_the_other(
    sqlite_provider: SqliteStorageProvider, sqlite_client: httpx.AsyncClient, via: str,
) -> None:
    """Two prompts of one superstep that share the raw id "dup" (an agent node's, or a tool_call node's behind ``pending_dispatch``)."""
    sessions = sqlite_provider.get_storage(WorkspaceSession)
    await sessions.create(_graph_ask_user_session(session_id="answered-sibling", via=via))

    response = await sqlite_client.post(
        "/v1/sessions/answered-sibling/ask_user/respond",
        json={"tool_call_id": "dup", "gate_id": G1, "response": "red"},
    )

    assert response.status_code == 202, response.text
    assert (await sessions.get("answered-sibling")).parked_status == "resumable"
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/sessions/answered-sibling/yields/pending") == [
        ("answered-sibling", "dup", G2),
    ]
    if via == "tool_call":
        # ask_user/pending reads a graph park's prompts from ``pending_dispatch`` (tool_call nodes only).
        pending = await sqlite_client.get("/v1/sessions/answered-sibling/ask_user/pending")
        assert pending.status_code == 200, pending.text
        assert (pending.json()["gate_id"], pending.json()["prompt"]) == (G2, "question of n1")

    response = await sqlite_client.post(
        "/v1/sessions/answered-sibling/ask_user/respond",
        json={"tool_call_id": "dup", "gate_id": G2, "response": "blue"},
    )

    assert response.status_code == 202, response.text
    assert await _listed(sqlite_client, "/v1/workspaces/ws-g/sessions/answered-sibling/yields/pending") == []
    await _assert_nothing_pending(sqlite_client, sessions, route="ask_user", session_id="answered-sibling")
