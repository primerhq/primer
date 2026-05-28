"""E2E tests for the M3 yields + M6 chats REST surface.

Covers backlog items:

* T0758 — GET /v1/sessions/{id}/ask_user/pending returns 404 when no park.
* T0762 — POST /v1/sessions/{id}/yields/{tcid}/cancel returns 404 for
  a non-existent session.
* T0763 — POST /v1/chats with unknown agent_id returns 404.
* T0764 — POST /v1/chats then GET /v1/chats/{id} round-trip preserves
  every field the create body carried plus the server-allocated defaults.
* T0765 — DELETE /v1/chats/{id} then DELETE again returns 409.

All five exercise endpoints that ship as part of the yielding-tools
feature (M3 yields surface, M6 chats surface). They do NOT require an
LLM — tests targeting park-state shapes (T0759-T0761) need real LLM
parking and are deferred to a future iteration once LM Studio is wired
into the bringup.
"""

from __future__ import annotations

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared seed helpers
# ---------------------------------------------------------------------------


async def _seed_llm_provider(client: httpx.AsyncClient, pid: str) -> None:
    r = await client.post(
        "/v1/llm_providers",
        json={
            "id": pid,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        },
    )
    assert r.status_code == 201, f"seed LLM failed: {r.text}"


async def _seed_agent(
    client: httpx.AsyncClient, agent_id: str, provider_id: str,
) -> None:
    r = await client.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "e2e yields+chats probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent failed: {r.text}"


async def _seed_workspace(
    client: httpx.AsyncClient,
    wp_id: str,
    tpl_id: str,
    tmp_path,
) -> str:
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert r.status_code == 201, f"seed wp provider failed: {r.text}"
    r = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "e2e tpl",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert r.status_code == 201, f"seed tpl failed: {r.text}"
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201, f"seed ws failed: {r.text}"
    return r.json()["id"]


async def _seed_session_created(
    client: httpx.AsyncClient,
    workspace_id: str,
    agent_id: str,
) -> str:
    """Seed a session in CREATED state — no worker claim yet."""
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": agent_id},
            "auto_start": False,
        },
    )
    assert r.status_code == 201, f"seed session failed: {r.text}"
    return r.json()["id"]


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0758 — GET /v1/sessions/{id}/ask_user/pending returns 404 when no park
# ===========================================================================


@pytest.mark.asyncio
async def test_t0758_ask_user_pending_returns_404_when_no_park(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0758 — Pin the negative envelope for the M3 ask_user pending
    endpoint. A freshly-created session that has never parked must
    return 404 (`/errors/not-found`), never `/errors/internal`.

    Priority area 1 — yielding-tools wire-contract.
    """
    pid = f"llm-t758-{unique_suffix}"
    aid = f"ag-t758-{unique_suffix}"
    wp_id = f"wp-t758-{unique_suffix}"
    tpl_id = f"tpl-t758-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    sid = await _seed_session_created(client, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    try:
        r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        # RFC 7807 envelope shape
        assert body["status"] == 404
        # Must be the not-found type, NOT /errors/internal
        assert body["type"].endswith("/not-found"), body
        assert "title" in body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0762 — POST /v1/sessions/{id}/yields/{tcid}/cancel for non-existent session
# ===========================================================================


@pytest.mark.asyncio
async def test_t0762_yields_cancel_returns_404_for_unknown_session(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0762 — The tool-agnostic cancel-yielded-tool endpoint is a thin
    wrapper on session lookup. A non-existent session id must produce
    a clean 404 envelope, never `/errors/internal`.
    """
    fake_sid = f"sess-does-not-exist-{unique_suffix}"
    fake_tcid = f"tc-{unique_suffix}"
    r = await client.post(
        f"/v1/sessions/{fake_sid}/yields/{fake_tcid}/cancel",
        json={"reason": "test"},
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found"), body


# ===========================================================================
# T0763 — POST /v1/chats with unknown agent_id returns 404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0763_chats_post_unknown_agent_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0763 — M6 chat REST: the agent reference is validated up-front
    via Storage[Agent].get(); a missing agent produces an RFC 7807
    `/errors/not-found` envelope, never `/errors/internal`.
    """
    r = await client.post(
        "/v1/chats",
        json={"agent_id": f"ag-does-not-exist-{unique_suffix}"},
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found"), body


# ===========================================================================
# T0764 — POST /v1/chats + GET /v1/chats/{id} round-trip
# ===========================================================================


@pytest.mark.asyncio
async def test_t0764_chats_create_then_get_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0764 — Create returns 201 with a Chat row carrying:

    * server-allocated id matching the `chat-XXX` shape
    * the supplied agent_id
    * status='active'
    * last_seq=0 (no messages yet)
    * created_at as an ISO timestamp

    GET /v1/chats/{id} returns the same row verbatim.

    Priority area 1 — M6 chat REST happy path.
    """
    pid = f"llm-t764-{unique_suffix}"
    aid = f"ag-t764-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    chat_id: str | None = None
    try:
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201, r.text
        created = r.json()
        chat_id = created["id"]
        assert chat_id.startswith("chat-"), chat_id
        assert created["agent_id"] == aid
        assert created["status"] == "active"
        assert created["last_seq"] == 0
        assert "created_at" in created
        # GET round-trip
        r2 = await client.get(f"/v1/chats/{chat_id}")
        assert r2.status_code == 200, r2.text
        fetched = r2.json()
        assert fetched["id"] == chat_id
        assert fetched["agent_id"] == aid
        assert fetched["status"] == "active"
        assert fetched["last_seq"] == 0
        assert fetched["created_at"] == created["created_at"]
    finally:
        if chat_id is not None:
            cleanup_urls.insert(0, f"/v1/chats/{chat_id}")
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0765 — DELETE /v1/chats/{id} idempotency boundary (409 on second DELETE)
# ===========================================================================


@pytest.mark.asyncio
async def test_t0765_chats_delete_twice_returns_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0765 — DELETE on an already-ended chat must return 409 Conflict,
    not /errors/internal. Defends the explicit ConflictError raise in
    primer/api/routers/chats.py:end_chat against silently degrading to
    no-op or 5xx leak.
    """
    pid = f"llm-t765-{unique_suffix}"
    aid = f"ag-t765-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    try:
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201
        cid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{cid}")
        # First DELETE → 200 + status='ended'
        r1 = await client.delete(f"/v1/chats/{cid}")
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "ended"
        # Second DELETE → 409 conflict
        r2 = await client.delete(f"/v1/chats/{cid}")
        assert r2.status_code == 409, r2.text
        body = r2.json()
        assert body["status"] == 409
        assert body["type"].endswith("/conflict"), body
    finally:
        await _cleanup(client, cleanup_urls)
