"""E2E tests: health envelope + find/null predicate + cancel defensive + chats cursor.

Four wire-contract / yielding-tools tests:

* T0820 — GET /v1/health returns the documented envelope shape:
  ``{status:"ok", version, scheduler:{alive, metrics}, worker_pool:{...}}``.
  Smoke pin for the operator-facing health surface.
* T0821 — POST /v1/sessions/find with predicate=null returns 200 +
  paginated empty (or non-empty) list. Confirms the bare-find path
  doesn't require a predicate.
* T0822 — POST /v1/sessions/{id}/yields/{tcid}/cancel on a session
  whose parked_state column is explicitly NULL returns 404
  cleanly (defensive parsing — _parked_blob() returns None when
  parked_state is missing).
* T0823 — GET /v1/chats with limit=2 over 5 seeded rows returns
  the documented offset envelope shape (kind="offset", total=5,
  length=2). Sister pagination pin for the chats list.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest


# ---------------------------------------------------------------------------
# Postgres + seed helpers
# ---------------------------------------------------------------------------


async def _pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", port=5432,
        user="primer", password="primer", database="primer_e2e",
    )


async def _seed_llm_provider(client: httpx.AsyncClient, pid: str) -> None:
    r = await client.post(
        "/v1/llm_providers",
        json={
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        },
    )
    assert r.status_code == 201


async def _seed_agent(
    client: httpx.AsyncClient, agent_id: str, provider_id: str,
) -> None:
    r = await client.post(
        "/v1/agents",
        json={
            "id": agent_id, "description": "probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201


async def _seed_workspace(
    client: httpx.AsyncClient, wp_id: str, tpl_id: str, tmp_path,
) -> str:
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        },
    )
    assert r.status_code == 201
    r = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id, "description": "tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        },
    )
    assert r.status_code == 201
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201
    return r.json()["id"]


async def _seed_session(
    client: httpx.AsyncClient, workspace_id: str, agent_id: str,
) -> str:
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": agent_id},
            "auto_start": False,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _force_parked_status_without_blob(session_id: str) -> None:
    """Flip parked_status to 'parked' WITHOUT writing a parked_state
    blob — leaves parked_state = JSON null. Forces the
    _parked_blob() path that returns None and the endpoint's 404
    branch.
    """
    sql = """
        UPDATE sessions
        SET data = jsonb_set(jsonb_set(data,
                     '{parked_status}', to_jsonb('parked'::text)),
                   '{parked_event_key}', to_jsonb('ask_user:malformed:tc-xx'::text)),
            updated_at = now()
        WHERE id = $1
    """
    conn = await _pg()
    try:
        await conn.execute(sql, session_id)
    finally:
        await conn.close()


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0820 — GET /v1/health envelope shape
# ===========================================================================


@pytest.mark.asyncio
async def test_t0820_health_returns_documented_envelope_shape(
    client: httpx.AsyncClient,
) -> None:
    """T0820 — GET /v1/health returns the documented envelope shape:
        {
          status: "ok",
          version: "...",
          scheduler: {alive: bool, metrics: {...}},
          worker_pool: {in_flight, capacity, metrics: {...}}
        }
    Smoke pin for the operator-facing health endpoint.
    """
    r = await client.get("/v1/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", body
    assert "version" in body, body
    # scheduler block.
    sched = body.get("scheduler", {})
    assert isinstance(sched, dict), body
    assert "alive" in sched, body
    # worker_pool block.
    pool = body.get("worker_pool")
    if pool is not None:  # may be absent when runtime_mode=api-only
        assert isinstance(pool, dict), body
        assert "in_flight" in pool, body
        assert "capacity" in pool, body


# ===========================================================================
# T0821 — POST /v1/sessions/find with predicate=null
# ===========================================================================


@pytest.mark.asyncio
async def test_t0821_sessions_find_with_null_predicate_returns_200(
    client: httpx.AsyncClient,
) -> None:
    """T0821 — POST /v1/sessions/find with ``predicate: null`` (the
    documented "list everything" shape) returns 200 with the paginated
    list envelope. Pins the predicate=None path in the find handler.
    """
    body = {
        "predicate": None,
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    r = await client.post("/v1/sessions/find", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "items" in payload, payload
    assert isinstance(payload["items"], list), payload


# ===========================================================================
# T0822 — Cancel-yielded-tool on session with NULL parked_state
# ===========================================================================


@pytest.mark.asyncio
async def test_t0822_cancel_yielded_tool_on_null_parked_state_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0822 — Force a session into parked_status='parked' WITHOUT
    writing the parked_state blob (NULL JSONB column). The cancel
    endpoint's _parked_blob() helper returns None for missing blobs;
    the endpoint must surface 404 cleanly, NOT /errors/internal from
    a NoneType.get() crash.

    Defensive-parsing pin for the yields router against partially-
    populated park rows.
    """
    pid = f"llm-822-{unique_suffix}"
    aid = f"ag-822-{unique_suffix}"
    wp_id = f"wp-822-{unique_suffix}"
    tpl_id = f"tpl-822-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    sid = await _seed_session(client, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    try:
        await _force_parked_status_without_blob(sid)

        r = await client.post(
            f"/v1/sessions/{sid}/yields/tc-xx/cancel",
            json={"reason": "probe"},
        )
        # Acceptable: 404 (no in-flight yield — parked_state is null
        # so _tool_call_id_for can't return a match). NEVER 500.
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["status"] == 404
        assert "internal" not in body.get("type", ""), body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0823 — GET /v1/chats cursor pagination
# ===========================================================================


@pytest.mark.asyncio
async def test_t0823_chats_list_offset_pagination_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0823 — Seed 5 chats. GET /v1/chats?agent_id=X with limit=2
    returns the offset-mode envelope: kind="offset", length=2,
    total=5, items has 2 rows. Pins the offset paginated envelope
    shape for the chats list endpoint.

    (Cursor pagination over the GET endpoint requires a real cursor
    seed — first call MUST be offset mode per parse_page in
    primer/api/pagination.py. Pure-cursor flow would need either
    the /find body endpoint or an empty-cursor convention which
    the Postgres backend doesn't currently accept. Out of scope.)
    """
    pid = f"llm-823-{unique_suffix}"
    aid = f"ag-823-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    chat_ids: list[str] = []
    try:
        for _ in range(5):
            r = await client.post("/v1/chats", json={"agent_id": aid})
            assert r.status_code == 201, r.text
            chat_ids.append(r.json()["id"])
        for cid in chat_ids:
            cleanup_urls.insert(0, f"/v1/chats/{cid}")

        r = await client.get(
            f"/v1/chats?agent_id={aid}&limit=2",
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("kind") == "offset", body
        assert body.get("length") == 2, body
        assert body.get("total") == 5, body
        assert body.get("offset") == 0, body
        items = body.get("items", [])
        assert len(items) == 2, items
        # All items belong to our agent.
        for it in items:
            assert it["agent_id"] == aid, it

        # Page 2 via offset.
        r = await client.get(
            f"/v1/chats?agent_id={aid}&limit=2&offset=2",
        )
        assert r.status_code == 200, r.text
        body2 = r.json()
        assert body2.get("offset") == 2, body2
        items2 = body2.get("items", [])
        assert len(items2) == 2, items2
        # No overlap with page 1.
        assert {it["id"] for it in items}.isdisjoint(
            {it["id"] for it in items2}
        ), f"pages overlap: {items} vs {items2}"
    finally:
        await _cleanup(client, cleanup_urls)
