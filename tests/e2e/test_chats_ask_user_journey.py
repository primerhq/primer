"""E2E: chat-surface conversational-yield (ask_user + approval) journey.

Sibling to the SESSION-side ask_user resume cycle
(``test_ask_user_resume_cycle_journey.py``). Where the session surface
PARKS (drops the lease, writes park columns, resumes via the engine
claim loop), the CHAT surface SOFT-YIELDS: it surfaces the tool's
prompt as a visible assistant message, records
``chat.pending_tool_call``, and ends the turn idle. The human's next
message resolves it (the resume path consumes the reply as the pending
call's tool_result and continues the agent loop). No parking; the Chat
model carries no ``parked_*`` columns any more.

Driven through the REAL path end-to-end against the live ``primer api``
server: a scripted mock-LLM agent emits an ``ask_user`` tool call on
its first turn and a terminating text reply on the continuation turn.
The user message is driven over the chat WebSocket (the only ingress
for chat user_messages); the chat-claim worker picks the chat up,
drives the turn, the tool soft-yields, and the dispatch loop records
``pending_tool_call`` + ends the turn. The operator's reply (a second
WS user_message) drives the continuation turn.

Two journeys in this module:

  1. ask_user  - first turn emits ``misc__ask_user`` (prompt
     "Which env?"); the question is surfaced, ``pending_tool_call``
     is set with mode ``ask_user``, ``turn_status`` returns to
     ``idle``, and NO ``parked_*`` columns exist. The reply
     "staging" is consumed as the tool_result; the continuation turn
     emits a final assistant message and clears ``pending_tool_call``.

  2. approval  - first turn emits a gated tool (``misc__uuid_v4``
     under a ``required`` policy); the chat soft-yields on the
     ``_approval`` pseudo-tool, ``pending_tool_call`` mode is
     ``approval``. Reply "yes" → the gated tool executes and the
     pending call clears; a sibling "no" path is asserted to reject
     without executing.

Subsystems exercised:
  * chat WebSocket ingress (user_message frame → append_user_message
    → turn_status=claimable → claim worker)
  * chat-claim worker + ``primer.chat.dispatch`` loop
  * ``primer.chat.executor.ChatTurnRunner`` soft_yield / resume_pending
  * ``misc.ask_user`` + the ``_approval`` gate falling back to
    ``ctx.chat_id`` so they yield on the chat surface
  * tool_approval policy resolution (approval journey)
  * Chat model ``pending_tool_call`` (in-conversation yield state;
    no park columns)
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_scripted_agent


# ---------------------------------------------------------------------------
# Helpers
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
    ``primer_session`` cookie is present (per
    ``primer.api.routers.chats.chat_ws``); the ``client`` fixture holds
    it in its cookie jar after login."""
    pairs = [f"{c.name}={c.value}" for c in client.cookies.jar]
    if not pairs:
        return []
    return [("Cookie", "; ".join(pairs))]


async def _send_user_message(
    client: httpx.AsyncClient, chat_id: str, content: str, *, drain: int = 6,
) -> None:
    """Open a fresh WS, send a ``user_message`` frame, drain a few frames.

    The WS replays prior rows (cursor replay) on every reconnect and only
    emits the leading ``usage`` envelope after replay, so the first frame
    kind is not stable across connects. We therefore do NOT assert any
    frame ordering here: this helper only needs to deliver the message to
    the claim worker and let the rows commit. State is verified out-of-band
    via ``GET /v1/chats/{id}`` (pending_tool_call / turn_status).
    """
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


async def _wait_for_pending(
    client: httpx.AsyncClient,
    chat_id: str,
    *,
    expect_mode: str,
    timeout_s: float = 30.0,
    interval_s: float = 0.3,
) -> dict:
    """Poll GET /v1/chats/{id} until pending_tool_call is set + turn idle.

    Success requires BOTH the pending call recorded AND the turn back
    to idle (the soft-yield ended the turn). Returns the chat body.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/chats/{chat_id}")
        if r.status_code == 200:
            last = r.json()
            ptc = last.get("pending_tool_call")
            if (
                ptc is not None
                and ptc.get("mode") == expect_mode
                and last.get("turn_status") == "idle"
            ):
                return last
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"chat {chat_id} never recorded a {expect_mode!r} pending_tool_call "
        f"at idle within {timeout_s}s; last_body={last!r}"
    )


async def _wait_for_cleared(
    client: httpx.AsyncClient,
    chat_id: str,
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.3,
) -> dict:
    """Poll until pending_tool_call clears AND the turn is back to idle."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/chats/{chat_id}")
        if r.status_code == 200:
            last = r.json()
            if (
                last.get("pending_tool_call") is None
                and last.get("turn_status") == "idle"
            ):
                return last
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"chat {chat_id} pending_tool_call never cleared within "
        f"{timeout_s}s; last_body={last!r}"
    )


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


# ===========================================================================
# Journey 1 - chat ask_user soft-yield + resume
# ===========================================================================


@pytest.mark.asyncio
async def test_chat_ask_user_softyield_and_resume(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    """A chat agent calls ask_user; the question surfaces, the chat
    records ``pending_tool_call`` (mode ask_user) at idle with NO park
    columns; the reply is consumed as the tool_result and the
    continuation turn clears the pending call.
    """
    registry, base_url = mock_llm
    suffix = f"chat-ask-{unique_suffix}"
    scenario = f"scripted:{suffix}"
    question = "Which env?"
    reply = "staging"

    agent = await make_scripted_agent(
        client, registry, base_url,
        suffix=suffix, scenario=scenario, tools=["misc__ask_user"],
        rules=[
            # First turn (no tool_result yet): emit the ask_user call.
            Rule(when_tool_result=False, emit_tool="misc__ask_user",
                 emit_args={"prompt": question}),
            # Continuation (tool_result present): final text answer that
            # references the reply so we can assert the loop continued.
            Rule(when_tool_result=True,
                 emit_text=f"Deploying to {reply}."),
        ],
    )
    cleanup_urls = [
        f"/v1/agents/{agent['agent_id']}",
        f"/v1/llm_providers/{agent['provider_id']}",
    ]
    cid: str | None = None
    try:
        r = await client.post("/v1/chats", json={"agent_id": agent["agent_id"]})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{cid}")

        # ----- 1. Drive the first turn: send a user_message ----------
        await _send_user_message(client, cid, "Deploy the app please")

        # ----- 2. The chat soft-yielded on ask_user ------------------
        body = await _wait_for_pending(client, cid, expect_mode="ask_user")
        ptc = body["pending_tool_call"]
        assert ptc["mode"] == "ask_user", ptc
        assert ptc.get("tool_call_id"), ptc
        assert body["turn_status"] == "idle", body
        # No park columns on the chat row (parked_* removed by the feature).
        for parked_key in (
            "parked_status", "parked_state", "parked_event_key",
            "parked_until", "parked_at",
        ):
            assert parked_key not in body, (
                f"chat row leaked park column {parked_key!r}: {body}"
            )
        assert "/errors/internal" not in json.dumps(body), body

        # The question is visible as an assistant message.
        items = await _messages(client, cid)
        deltas = _delta_texts(items)
        assert any(question in d for d in deltas), (
            f"question {question!r} not surfaced; deltas={deltas}"
        )

        # ----- 3. Reply resolves the pending call --------------------
        await _send_user_message(client, cid, reply)

        # ----- 4. The continuation turn cleared pending_tool_call ----
        cleared = await _wait_for_cleared(client, cid)
        assert cleared["pending_tool_call"] is None, cleared

        # The agent continued: a final assistant message referencing the
        # reply was produced after the question.
        items = await _messages(client, cid)
        deltas = _delta_texts(items)
        assert any(reply in d and question not in d for d in deltas), (
            f"continuation answer referencing {reply!r} not found; "
            f"deltas={deltas}"
        )
        # And the reply was consumed as a tool_result for the pending call.
        tool_results = [
            it for it in items if it.get("kind") == "tool_result"
        ]
        assert tool_results, f"no tool_result row after resume; items={items}"
        assert any(
            reply in json.dumps(tr.get("payload") or {}) for tr in tool_results
        ), f"reply {reply!r} not in any tool_result payload; {tool_results}"
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# Journey 2 - chat approval soft-yield: yes executes, no rejects
# ===========================================================================


async def _seed_required_policy(
    client: httpx.AsyncClient, *, pol_id: str, tool_name: str,
) -> None:
    """Gate ``misc__{tool_name}`` behind a required-approval policy.

    Clears any pre-existing policy for the same (toolset, tool) first so
    concurrent iterations don't collide, then invalidates the resolver
    cache so the new policy is seen by the next turn.
    """
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if (
                it.get("toolset_id") == "misc"
                and it.get("tool_name") == tool_name
            ):
                await client.delete(
                    f"/v1/tool_approval_policies/{it['id']}",
                )
    r = await client.post(
        "/v1/tool_approval_policies",
        json={
            "id": pol_id,
            "toolset_id": "misc",
            "tool_name": tool_name,
            "enabled": True,
            "approval": {"type": "required"},
            "timeout_seconds": 600,
        },
    )
    assert r.status_code in (200, 201), r.text
    inv = await client.post("/v1/tool_approval_policies/invalidate")
    assert inv.status_code == 202, inv.text


async def _drive_to_approval_pending(
    client: httpx.AsyncClient,
    *,
    agent_id: str,
):
    """Create a chat, drive the first turn to an approval soft-yield,
    then return ``(chat_id, pending_body)``.
    """
    r = await client.post("/v1/chats", json={"agent_id": agent_id})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    await _send_user_message(client, cid, "Make me an id")
    pending = await _wait_for_pending(client, cid, expect_mode="approval")
    return cid, pending


@pytest.mark.asyncio
async def test_chat_approval_softyield_yes_executes_no_rejects(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    """A chat agent calls a gated tool; the chat soft-yields on
    ``_approval`` (mode approval). Replying "yes" executes the gated
    tool and clears the pending call; replying "no" rejects WITHOUT
    executing and still clears the pending call.
    """
    registry, base_url = mock_llm
    gated = "uuid_v4"

    async def _build_agent(local_suffix: str) -> dict:
        scenario = f"scripted:{local_suffix}"
        return await make_scripted_agent(
            client, registry, base_url,
            suffix=local_suffix, scenario=scenario,
            tools=[f"misc__{gated}"],
            rules=[
                Rule(when_tool_result=False, emit_tool=f"misc__{gated}",
                     emit_args={}),
                Rule(when_tool_result=True, emit_text="all done"),
            ],
        )

    # --- YES path ---------------------------------------------------
    suffix_yes = f"chat-appr-yes-{unique_suffix}"
    pol_yes = f"pol-{suffix_yes}"
    agent_yes = await _build_agent(suffix_yes)
    cleanup_urls = [
        f"/v1/tool_approval_policies/{pol_yes}",
        f"/v1/agents/{agent_yes['agent_id']}",
        f"/v1/llm_providers/{agent_yes['provider_id']}",
    ]
    cid_yes: str | None = None
    try:
        await _seed_required_policy(client, pol_id=pol_yes, tool_name=gated)
        cid_yes, pending = await _drive_to_approval_pending(
            client, agent_id=agent_yes["agent_id"],
        )
        cleanup_urls.insert(0, f"/v1/chats/{cid_yes}")
        assert pending["pending_tool_call"]["mode"] == "approval", pending
        # No park columns.
        assert "parked_status" not in pending, pending

        # Reply "yes" → the gated tool executes.
        await _send_user_message(client, cid_yes, "yes")

        cleared = await _wait_for_cleared(client, cid_yes)
        assert cleared["pending_tool_call"] is None, cleared
        items = await _messages(client, cid_yes)
        tool_results = [
            it for it in items if it.get("kind") == "tool_result"
        ]
        assert tool_results, f"no tool_result after approval yes; {items}"
        # The executed gated tool's result must NOT be an error.
        last_tr = tool_results[-1]
        assert not (last_tr.get("payload") or {}).get("error"), last_tr
    finally:
        await _cleanup(client, cleanup_urls)

    # --- NO path ----------------------------------------------------
    suffix_no = f"chat-appr-no-{unique_suffix}"
    pol_no = f"pol-{suffix_no}"
    agent_no = await _build_agent(suffix_no)
    cleanup_urls = [
        f"/v1/tool_approval_policies/{pol_no}",
        f"/v1/agents/{agent_no['agent_id']}",
        f"/v1/llm_providers/{agent_no['provider_id']}",
    ]
    cid_no: str | None = None
    try:
        await _seed_required_policy(client, pol_id=pol_no, tool_name=gated)
        cid_no, pending = await _drive_to_approval_pending(
            client, agent_id=agent_no["agent_id"],
        )
        cleanup_urls.insert(0, f"/v1/chats/{cid_no}")
        assert pending["pending_tool_call"]["mode"] == "approval", pending

        # Reply "no" → rejected WITHOUT executing the gated tool.
        await _send_user_message(client, cid_no, "no")

        cleared = await _wait_for_cleared(client, cid_no)
        assert cleared["pending_tool_call"] is None, cleared
        items = await _messages(client, cid_no)
        tool_results = [
            it for it in items if it.get("kind") == "tool_result"
        ]
        assert tool_results, f"no tool_result after approval no; {items}"
        # The rejection result is an error and the gated tool did NOT run.
        last_tr = tool_results[-1]
        assert (last_tr.get("payload") or {}).get("error"), last_tr
        assert "declin" in json.dumps(last_tr.get("payload") or {}).lower(), (
            last_tr
        )
    finally:
        await _cleanup(client, cleanup_urls)
