"""E2E: an aggregated LLM provider fails over from a 429 member to a live one.

Runs only under PRIMER_RUN_E2E=1 (see tests/e2e/conftest.py). Locally verify
collection with --collect-only; CI runs it.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule


def _ws_headers(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Forward the authenticated client's session cookie onto the WS
    handshake (mirrors tests/e2e/test_bus_tasks_and_chat_ws.py)."""
    pairs = [f"{c.name}={c.value}" for c in client.cookies.jar]
    if not pairs:
        return []
    return [("Cookie", "; ".join(pairs))]


@pytest.mark.asyncio
async def test_aggregated_fails_over_from_429_member(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
):
    import websockets

    registry, base_url = mock_llm
    # Two scenario models on the shared mock: one always 429, one serves.
    registry.register("agg:429", [Rule(emit_status=429, emit_error_message="429")])
    registry.register("agg:ok", [Rule(emit_text="served-by-member-2")])

    # One openchat provider (points at the mock) exposing both scenario models.
    member_pid = f"agg-member-{unique_suffix}"
    r = await client.post("/v1/llm_providers", json={
        "id": member_pid,
        "provider": "openchat",
        "models": [
            {"name": "agg:429", "context_length": 8192},
            {"name": "agg:ok", "context_length": 8192},
        ],
        "config": {"url": base_url, "flavor": "lmstudio"},
        "limits": {"max_concurrency": 4},
    })
    assert r.status_code in (200, 201), r.text

    # Aggregated provider: member[0] = 429 model, member[1] = ok model.
    agg_pid = f"agg-{unique_suffix}"
    r = await client.post("/v1/llm_providers", json={
        "id": agg_pid,
        "provider": "aggregated",
        "config": {
            "members": [
                {"provider_id": member_pid, "model_name": "agg:429"},
                {"provider_id": member_pid, "model_name": "agg:ok"},
            ],
            "strategy": "sequential",
            "failover_point": "before_first_token",
            "failover_on": "transient_and_config",
        },
        "models": [{"name": "agg-virtual", "context_length": 8192}],
        "limits": {"max_concurrency": 4},
    })
    assert r.status_code in (200, 201), r.text

    # Agent bound to the aggregated virtual model.
    agent_id = f"agg-agent-{unique_suffix}"
    r = await client.post("/v1/agents", json={
        "id": agent_id,
        "description": "aggregated failover probe",
        "model": {"provider_id": agg_pid, "model_name": "agg-virtual"},
        "tools": [],
        "system_prompt": ["aggregated failover probe"],
    })
    assert r.status_code in (200, 201), r.text

    cleanup_urls = [
        f"/v1/agents/{agent_id}",
        f"/v1/llm_providers/{agg_pid}",
        f"/v1/llm_providers/{member_pid}",
    ]
    try:
        # Drive one chat turn over WS (mirror
        # tests/e2e/test_bus_tasks_and_chat_ws.py's create-chat +
        # websocket_connect + receive loop). Assert the assistant text is
        # served by member[1] after member[0] fails over on connect (429).
        r = await client.post("/v1/chats", json={"agent_id": agent_id})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{cid}")

        http_url = str(client.base_url).rstrip("/")
        ws_origin = http_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        ws_url = f"{ws_origin}/v1/chats/{cid}/ws"

        assistant_text = ""
        saw_done = False
        async with websockets.connect(
            ws_url, max_size=None, additional_headers=_ws_headers(client),
        ) as ws:
            # Spec §6.4: initial ``usage`` frame after accept().
            initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            assert initial["kind"] == "usage", initial
            await ws.send(json.dumps(
                {"kind": "user_message", "content": "hello"}
            ))
            deadline = asyncio.get_event_loop().time() + 30.0
            while not saw_done:
                remaining = deadline - asyncio.get_event_loop().time()
                assert remaining > 0, "chat turn did not reach a 'done' frame"
                frame = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=remaining)
                )
                kind = frame.get("kind")
                if kind == "assistant_token":
                    assistant_text += frame.get("delta") or ""
                elif kind == "done":
                    saw_done = True

        assert assistant_text == "served-by-member-2", assistant_text

        # CRUD RFC7807: an empty members list is 422.
        bad = await client.post("/v1/llm_providers", json={
            "id": f"agg-bad-{unique_suffix}",
            "provider": "aggregated",
            "config": {"members": []},
            "models": [{"name": "v", "context_length": 8192}],
            "limits": {"max_concurrency": 4},
        })
        assert bad.status_code == 422, bad.text
    finally:
        for url in cleanup_urls:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
