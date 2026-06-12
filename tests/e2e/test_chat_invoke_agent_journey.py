"""E2E: a chat agent invokes a subagent via ``system__invoke_agent``.

``system__invoke_agent(agent_id, prompt)`` runs another agent once and returns
``{output: <text>}`` to the caller without yielding. This journey drives the
REAL path end-to-end against the live ``primer api`` server using the scripted
mock-LLM harness:

  1. A subagent B whose only behaviour is to emit a distinctive marker
     ("SUBAGENT-OUTPUT-<unique>"). B needs no special tools.
  2. A caller A bound to ``system__invoke_agent``. On its first turn A emits the
     invoke_agent tool call targeting B; once the tool result is present in
     history A emits its final reply ("A-DONE-<unique>"). The
     ``when_tool_result=True`` rule is FIRST so A does not loop on the tool.
  3. A chat is bound to A. One user_message drives the turn; A calls
     invoke_agent, B runs, A finishes.

Assertions:
  * A's final reply ("A-DONE-<unique>") reaches the transcript at idle.
  * A ``tool_result`` row carries B's marker in its payload (the invoke_agent
    call really ran the subagent and surfaced its output to A).

The chat ``tool_result`` row payload shape (see primer/chat/executor.py) is:
    {"id": <call_id>, "name": <tool name>, "result": <output text>,
     "error": <bool>}
The ``name`` may be bare ("invoke_agent") or scoped ("system__invoke_agent");
the marker is asserted by scanning every tool_result row's payload, so the
match does not depend on the exact tool-name spelling.

Subsystems exercised:
  * chat WebSocket ingress -> claim worker -> turn
  * tool binding + execution of ``system__invoke_agent`` from a chat
  * nested agent run (caller -> subagent) and result plumb-back
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
# Journey - a chat agent invokes a subagent; the output reaches the transcript
# ===========================================================================


@pytest.mark.asyncio
async def test_chat_invoke_agent_surfaces_subagent_output(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    """Caller A calls ``system__invoke_agent`` on subagent B; B's marker
    appears in a ``tool_result`` row and A emits its final reply."""
    registry, base_url = mock_llm

    suffix_a = f"chat-inv-a-{unique_suffix}"
    suffix_b = f"chat-inv-b-{unique_suffix}"
    sub_marker = f"SUBAGENT-OUTPUT-{unique_suffix}"
    done_marker = f"A-DONE-{unique_suffix}"

    # Subagent B: a single text reply carrying a distinctive marker.
    agent_b = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix_b, scenario=f"scripted:{suffix_b}",
        rules=[Rule(emit_text=sub_marker)],
    )
    # Caller A: invokes B on the first turn, then finishes once the tool
    # result is present. ORDER MATTERS: the when_tool_result rule is FIRST so
    # A emits its final text instead of re-calling the tool (avoids a loop).
    agent_a = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix_a, scenario=f"scripted:{suffix_a}",
        tools=["system__invoke_agent"],
        rules=[
            Rule(when_tool_result=True, emit_text=done_marker),
            Rule(
                emit_tool="system__invoke_agent",
                emit_args={
                    "agent_id": agent_b["agent_id"],
                    "prompt": "summarise",
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

        # ----- Drive one turn ----------------------------------------
        await _send_user_message(client, cid, "Please consult the subagent.")
        await _wait_for_reply(
            client, cid, expect_text=done_marker, timeout_s=40.0,
        )

        # ----- The subagent ran: its marker is in a tool_result row --
        items = await _messages(client, cid)
        tool_results = [
            it for it in items if it.get("kind") == "tool_result"
        ]
        assert tool_results, f"no tool_result row in transcript; items={items}"
        assert any(
            sub_marker in json.dumps(tr.get("payload") or {})
            for tr in tool_results
        ), (
            f"subagent marker {sub_marker!r} not found in any tool_result "
            f"payload; tool_results={tool_results}"
        )
    finally:
        await _cleanup(client, cleanup_urls)
