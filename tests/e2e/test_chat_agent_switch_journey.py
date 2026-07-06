"""E2E: mid-chat agent switching journey.

A chat is bound to an agent at creation, but the agent + its system prompt
are resolved fresh at the start of every turn from ``chat.agent_id``. The
``POST /v1/chats/{id}/agent`` endpoint re-points that field, so the NEXT
turn runs under the new agent with the full prior history as shared context.
History is never rewritten; only the system prompt + tools change.

This journey drives the REAL path end-to-end against the live ``primer api``
server using the scripted mock-LLM harness:

  1. Two scripted agents whose model name IS their scenario id, so the mock
     keys distinct replies off the request model: agent-A always emits
     "REPLY-FROM-A", agent-B always emits "REPLY-FROM-B".
  2. A chat is created bound to agent-A. A user_message over the chat WS
     drives the first turn; the assistant reply is "REPLY-FROM-A".
  3. ``POST /v1/chats/{id}/agent {agent_id: B}`` -> 200 + agent_id == B.
  4. A second user_message drives the next turn; the assistant reply is now
     "REPLY-FROM-B" -- the switch took effect on the next turn.

Plus two negative assertions on the switch endpoint: 404 for an unknown
target agent and 409 for a chat that has ended.

Subsystems exercised:
  * chat WebSocket ingress (user_message frame -> claim worker -> turn)
  * fresh per-turn agent resolution from ``chat.agent_id``
  * ``POST /v1/chats/{id}/agent`` switch endpoint (200 / 404 / 409)
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_scripted_agent


# ---------------------------------------------------------------------------
# Helpers (mirrors test_chats_ask_user_journey.py exactly)
# ---------------------------------------------------------------------------


def _ws_url(client: httpx.AsyncClient, chat_id: str) -> str:
    http_url = str(client.base_url).rstrip("/")
    ws_origin = http_url.replace("http://", "ws://").replace(
        "https://", "wss://",
    )
    return f"{ws_origin}/v1/chats/{chat_id}/ws"


def _ws_headers(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Forward the authenticated client's session cookie onto the WS
    handshake. The chat WS closes with 4401 unless the signed
    ``primer_session`` cookie is present; the ``client`` fixture holds it in
    its cookie jar after login."""
    pairs = [f"{c.name}={c.value}" for c in client.cookies.jar]
    if not pairs:
        return []
    return [("Cookie", "; ".join(pairs))]


async def _send_user_message(
    client: httpx.AsyncClient, chat_id: str, content: str, *, drain: int = 6,
) -> None:
    """Open a fresh WS, send a ``user_message`` frame, drain a few frames.

    We do NOT assert frame ordering here: this helper only needs to deliver
    the message to the claim worker and let the rows commit. State is
    verified out-of-band via ``GET /v1/chats/{id}`` and the message log.
    """
    import websockets

    async with websockets.connect(
        _ws_url(client, chat_id), additional_headers=_ws_headers(client),
    ) as ws:
        await ws.send(json.dumps({"kind": "user_message", "content": content}))
        for _ in range(drain):
            try:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                break


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


async def _messages(client: httpx.AsyncClient, chat_id: str) -> list[dict]:
    r = await client.get(f"/v1/chats/{chat_id}/messages?after_seq=0")
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _delta_texts(items: list[dict]) -> list[str]:
    """Concatenated assistant_token deltas, one entry per row."""
    out: list[str] = []
    for it in items:
        if it.get("kind") == "assistant_token":
            payload = it.get("payload") or {}
            delta = payload.get("delta") or payload.get("content") or ""
            if delta:
                out.append(delta)
    return out


async def _wait_for_reply(
    client: httpx.AsyncClient,
    chat_id: str,
    *,
    expect_text: str,
    timeout_s: float = 30.0,
    interval_s: float = 0.3,
) -> dict:
    """Poll until an assistant delta contains ``expect_text`` AND the turn
    is back to idle (the turn completed). Returns the chat body."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/chats/{chat_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("turn_status") == "idle":
                items = await _messages(client, chat_id)
                deltas = _delta_texts(items)
                if any(expect_text in d for d in deltas):
                    return last
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"chat {chat_id} never produced an assistant reply containing "
        f"{expect_text!r} at idle within {timeout_s}s; last_body={last!r}"
    )


# ===========================================================================
# Journey - mid-chat agent switch changes which agent answers
# ===========================================================================


@pytest.mark.asyncio
async def test_chat_agent_switch_changes_responder(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    """A chat bound to agent-A replies "REPLY-FROM-A"; after switching to
    agent-B the next turn replies "REPLY-FROM-B". History is preserved and
    the switch takes effect on the next turn."""
    registry, base_url = mock_llm

    suffix_a = f"chat-switch-a-{unique_suffix}"
    suffix_b = f"chat-switch-b-{unique_suffix}"

    # Each agent's model name IS its scenario id, so the mock keys distinct
    # replies off the request model. A single text rule per agent yields a
    # constant, distinguishable reply for every turn.
    agent_a = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix_a, scenario=f"scripted:{suffix_a}",
        rules=[Rule(emit_text="REPLY-FROM-A")],
    )
    agent_b = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix_b, scenario=f"scripted:{suffix_b}",
        rules=[Rule(emit_text="REPLY-FROM-B")],
    )

    cleanup_urls = [
        f"/v1/agents/{agent_a['agent_id']}",
        f"/v1/llm_providers/{agent_a['provider_id']}",
        f"/v1/agents/{agent_b['agent_id']}",
        f"/v1/llm_providers/{agent_b['provider_id']}",
    ]
    cid: str | None = None
    try:
        # ----- 1. Create the chat bound to agent-A -------------------
        r = await client.post(
            "/v1/chats", json={"agent_id": agent_a["agent_id"]},
        )
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{cid}")

        # ----- 2. First turn: agent-A handles it ---------------------
        await _send_user_message(client, cid, "Who are you?")
        await _wait_for_reply(client, cid, expect_text="REPLY-FROM-A")

        # ----- 3. Switch the chat's agent to agent-B -----------------
        sw = await client.post(
            f"/v1/chats/{cid}/agent",
            json={"agent_id": agent_b["agent_id"]},
        )
        assert sw.status_code == 200, sw.text
        assert sw.json()["agent_id"] == agent_b["agent_id"], sw.text

        # ----- 3b. The switch appended a "switch" agent_marker row ----
        # (Task A5) so the timeline records the attribution boundary.
        items = await _messages(client, cid)
        markers = [it for it in items if it.get("kind") == "agent_marker"]
        assert len(markers) == 1, f"expected exactly one agent_marker; got {items}"
        # ChatMessage rows serialize the kind-specific fields under the
        # nested `payload` blob (same shape as assistant_token deltas,
        # read via _delta_texts). append_agent_marker writes
        # payload={"marker", "agent_id", "from_agent_id"}.
        marker_payload = markers[0]["payload"]
        assert marker_payload["marker"] == "switch", markers[0]
        assert marker_payload["agent_id"] == agent_b["agent_id"], markers[0]
        assert marker_payload["from_agent_id"] == agent_a["agent_id"], markers[0]

        # ----- 4. Next turn: agent-B now answers ---------------------
        await _send_user_message(client, cid, "And now who are you?")
        await _wait_for_reply(client, cid, expect_text="REPLY-FROM-B")

        # History is preserved: agent-A's earlier reply is still in the log.
        items = await _messages(client, cid)
        deltas = _delta_texts(items)
        assert any("REPLY-FROM-A" in d for d in deltas), (
            f"agent-A reply missing from history after switch; deltas={deltas}"
        )
        assert any("REPLY-FROM-B" in d for d in deltas), (
            f"agent-B reply missing after switch; deltas={deltas}"
        )

        # ----- 5. 404 for an unknown target agent --------------------
        miss = await client.post(
            f"/v1/chats/{cid}/agent",
            json={"agent_id": f"no-such-agent-{unique_suffix}"},
        )
        assert miss.status_code == 404, miss.text

        # ----- 6. 409 once the chat has ended ------------------------
        ended = await client.delete(f"/v1/chats/{cid}")
        assert ended.status_code == 200, ended.text
        conflict = await client.post(
            f"/v1/chats/{cid}/agent",
            json={"agent_id": agent_a["agent_id"]},
        )
        assert conflict.status_code == 409, conflict.text
    finally:
        await _cleanup(client, cleanup_urls)
