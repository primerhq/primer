"""E2E: a chat agent hands the chat off via ``system__switch_to_agent``.

``system__switch_to_agent(agent_id, prompt)`` hands the CURRENT chat off to
another agent: it ends the caller's turn, switches ``chat.agent_id`` to the
target, and the dispatch loop injects the handoff prompt as the next turn,
which the new agent answers. This is the dispatch handoff path -- the
high-value journey of this tranche.

This drives the REAL path end-to-end against the live ``primer api`` server
using the scripted mock-LLM harness:

  1. Target agent B emits a distinctive reply ("HANDOFF-REPLY-<unique>").
  2. Caller A is bound to ``system__switch_to_agent`` and only ever switches
     away to B (a single fallback rule emitting the switch tool call).
  3. A chat is bound to A. ONE user_message drives A's turn; A emits the
     switch tool call, which yields. The chat runner ends A's turn, switches
     ``chat.agent_id`` to B, and the dispatch loop injects the handoff prompt
     as the next turn -- B answers it.

Assertions:
  * B's handoff reply ("HANDOFF-REPLY-<unique>") reaches the transcript at
    idle. Because the handoff is a second auto-injected turn, a longer
    timeout is used.
  * ``GET /v1/chats/{cid}`` shows ``agent_id == B`` (the switch took effect).

Subsystems exercised:
  * chat WebSocket ingress -> claim worker -> turn
  * ``system__switch_to_agent`` tool: yield -> end turn -> repoint agent_id
  * dispatch loop auto-injecting the handoff prompt as the next turn
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_scripted_agent


# ---------------------------------------------------------------------------
# Helpers (copied verbatim from test_chat_agent_switch_journey.py)
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
    """Open a fresh WS, send a ``user_message`` frame, drain a few frames."""
    import websockets

    async with websockets.connect(
        _ws_url(client, chat_id), additional_headers=_ws_headers(client),
    ) as ws:
        await ws.send(json.dumps({"kind": "user_message", "content": content}))
        for _ in range(drain):
            try:
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
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
# Journey - switch_to_agent hands the chat off; B answers the handoff turn
# ===========================================================================


@pytest.mark.asyncio
async def test_chat_switch_to_agent_hands_off_and_target_replies(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    """Caller A calls ``system__switch_to_agent`` to hand off to B. The
    switch repoints ``chat.agent_id`` to B and the dispatch loop injects the
    handoff prompt, which B answers with "HANDOFF-REPLY-<unique>"."""
    registry, base_url = mock_llm

    suffix_a = f"chat-sw-a-{unique_suffix}"
    suffix_b = f"chat-sw-b-{unique_suffix}"
    handoff_marker = f"HANDOFF-REPLY-{unique_suffix}"

    # Target B: a single text reply answering the injected handoff prompt.
    agent_b = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix_b, scenario=f"scripted:{suffix_b}",
        rules=[Rule(emit_text=handoff_marker)],
    )
    # Caller A: only ever switches away to B. A single fallback rule emitting
    # the switch tool call is sufficient -- A's turn ends on the yield.
    agent_a = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix_a, scenario=f"scripted:{suffix_a}",
        tools=["system__switch_to_agent"],
        rules=[
            Rule(
                emit_tool="switch_to_agent",
                emit_args={
                    "agent_id": agent_b["agent_id"],
                    "prompt": "you take over now",
                },
            ),
        ],
    )

    cleanup_urls = [
        f"/v1/agents/{agent_a['agent_id']}",
        f"/v1/llm_providers/{agent_a['provider_id']}",
        f"/v1/agents/{agent_b['agent_id']}",
        f"/v1/llm_providers/{agent_b['provider_id']}",
    ]
    cid: str | None = None
    try:
        # ----- Create the chat bound to caller A ---------------------
        r = await client.post(
            "/v1/chats", json={"agent_id": agent_a["agent_id"]},
        )
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{cid}")

        # ----- One user_message triggers the handoff ----------------
        await _send_user_message(client, cid, "please hand off")

        # The handoff is a SECOND auto-injected turn (B answering the handoff
        # prompt), so allow more time than a single-turn reply.
        await _wait_for_reply(
            client, cid, expect_text=handoff_marker, timeout_s=40.0,
        )

        # ----- The switch took effect: chat.agent_id == B -----------
        got = await client.get(f"/v1/chats/{cid}")
        assert got.status_code == 200, got.text
        assert got.json()["agent_id"] == agent_b["agent_id"], (
            f"chat agent_id did not switch to B after switch_to_agent; "
            f"body={got.json()!r}"
        )
    finally:
        await _cleanup(client, cleanup_urls)
