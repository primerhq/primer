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


# T0772 (chats list filter by agent_id) pruned 2026-05-25 — narrow
# happy-path filter pin subsumed by general per-router filter-pinning
# patterns. The agent_id-filter wiring in chats.py is a 3-line
# Predicate; regressions would surface in the broader chat journey
# tests (T0764 round-trip; T0859 chats approval journey) which create
# chats under specific agents and read them back.


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


# T0775 (chat POST→DELETE lifecycle envelope) pruned 2026-05-25 —
# happy-path create-then-delete pin subsumed by T0859 (chats approval
# park-respond-delete journey) which walks the create AND delete
# transitions on a real chat, plus T0764/T0765 round-trip + double-
# delete contract pins.
