"""E2E tests: M6 chat surface wire-contract polish.

Four envelope/contract pins for the M6 chats REST surface:

* T0771 — POST /v1/chats with missing agent_id returns 422
  /errors/validation-error (RFC 7807 envelope, not /errors/internal).
* T0772 — GET /v1/chats?agent_id=X returns only rows matching that
  agent_id; rows for other agents are filtered out.
* T0773 — GET /v1/chats/{id}/messages on a non-existent chat returns
  404 /errors/not-found cleanly.
* T0775 — POST /v1/chats then immediately DELETE returns 201 then
  200 (ended); no /errors/internal under the create-then-destroy
  lifecycle race.

All four pin observable envelope contracts the M6 surface
must hold to per spec §3 (RFC 7807) without needing an LLM.
"""

from __future__ import annotations

import httpx
import pytest


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
            "description": "probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent failed: {r.text}"


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0771 — POST /v1/chats missing agent_id → 422
# ===========================================================================


@pytest.mark.asyncio
async def test_t0771_chats_post_missing_agent_id_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """T0771 — agent_id is a required field on ChatCreateBody. An
    empty body must return a 422 /errors/validation-error envelope
    that names ``agent_id`` in the extensions.errors[].loc path,
    never /errors/internal.
    """
    r = await client.post("/v1/chats", json={})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["status"] == 422
    assert body["type"].endswith("/validation-error"), body
    # The validator error path should name agent_id.
    errors = body.get("extensions", {}).get("errors", [])
    locs = ["/".join(str(p) for p in e.get("loc", [])) for e in errors]
    assert any("agent_id" in loc for loc in locs), (
        f"expected agent_id in validation errors, got {locs!r}"
    )


# ===========================================================================
# T0772 — GET /v1/chats?agent_id=X filter
# ===========================================================================


@pytest.mark.asyncio
async def test_t0772_chats_list_filter_by_agent_id_returns_only_matching(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0772 — Create two agents + one chat per agent. GET /v1/chats
    ?agent_id=ag-A must return only chats bound to ag-A; ag-B's
    chat must NOT appear in the filtered result.

    Defends the `agent_id` filter wired in matrix/api/routers/chats.py.
    """
    pid = f"llm-t772-{unique_suffix}"
    ag_a = f"ag-a-{unique_suffix}"
    ag_b = f"ag-b-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, ag_a, pid)
    await _seed_agent(client, ag_b, pid)
    cleanup_urls = [
        f"/v1/agents/{ag_a}",
        f"/v1/agents/{ag_b}",
        f"/v1/llm_providers/{pid}",
    ]
    chat_a: str | None = None
    chat_b: str | None = None
    try:
        r = await client.post("/v1/chats", json={"agent_id": ag_a})
        assert r.status_code == 201, r.text
        chat_a = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{chat_a}")
        r = await client.post("/v1/chats", json={"agent_id": ag_b})
        assert r.status_code == 201, r.text
        chat_b = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{chat_b}")

        # GET ?agent_id=ag_a → only chat_a present.
        r = await client.get(f"/v1/chats?agent_id={ag_a}")
        assert r.status_code == 200, r.text
        items = r.json().get("items", [])
        ids = {it["id"] for it in items}
        assert chat_a in ids, f"chat_a {chat_a!r} missing from filtered list"
        assert chat_b not in ids, (
            f"chat_b {chat_b!r} leaked into ag_a-filtered list: {ids}"
        )

        # GET ?agent_id=ag_b → only chat_b present.
        r = await client.get(f"/v1/chats?agent_id={ag_b}")
        assert r.status_code == 200, r.text
        items = r.json().get("items", [])
        ids = {it["id"] for it in items}
        assert chat_b in ids
        assert chat_a not in ids
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0773 — GET /v1/chats/{id}/messages on missing chat → 404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0773_chat_messages_get_returns_404_for_unknown_chat(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0773 — list_chat_messages explicitly 404s on a missing chat
    BEFORE reading the messages table (per matrix/api/routers/chats.py:
    'so we don't leak \"this id has no messages\" as a probe surface').

    Pins that probe-resistance contract against accidental degradation
    to 200/[] or /errors/internal.
    """
    fake_cid = f"chat-does-not-exist-{unique_suffix}"
    r = await client.get(f"/v1/chats/{fake_cid}/messages")
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found"), body


# ===========================================================================
# T0775 — POST then immediate DELETE on chat returns clean envelopes
# ===========================================================================


@pytest.mark.asyncio
async def test_t0775_chat_create_then_delete_lifecycle_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0775 — Create a chat (201) then immediately DELETE it (200
    with status='ended'). Verify no /errors/internal anywhere along
    the path. Pins the create-then-destroy lifecycle envelope.

    Sister to T0764 (round-trip preserves fields) and T0765
    (delete-twice 409). Specifically asserts the fast lifecycle
    works cleanly without intervening reads or messages.
    """
    pid = f"llm-t775-{unique_suffix}"
    aid = f"ag-t775-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    try:
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        # Immediately delete — no intervening reads.
        r2 = await client.delete(f"/v1/chats/{cid}")
        assert r2.status_code == 200, r2.text
        assert r2.json()["status"] == "ended"
        # GET still returns the row (DELETE marks ended, not destroys).
        r3 = await client.get(f"/v1/chats/{cid}")
        assert r3.status_code == 200, r3.text
        assert r3.json()["status"] == "ended"
    finally:
        await _cleanup(client, cleanup_urls)
