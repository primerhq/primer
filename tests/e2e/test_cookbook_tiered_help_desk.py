"""Cookbook recipe #13 regression: Tiered Help Desk with Supervisor Sign-off.

Guards the CHAT-HITL loop a tiered customer-support desk relies on, driven
end-to-end over the chat WebSocket ingress (the headless chat surface). Where
the Release Conductor recipe (#10) pins the SESSION-side HITL loop -- which
PARKS and resumes over the REST yield endpoints -- this recipe pins the
CHAT-side equivalent, which SOFT-YIELDS: every gate degrades to a
conversational turn and the customer's next WS message is consumed as the
pending tool call's result. Chats never park; the Chat model carries no
``parked_*`` columns.

Four chat mechanics, chained in one conversation:

1. Chat WS ingress -- a ``user_message`` frame on ``WS /v1/chats/{id}/ws``
   starts/continues the chat; the claim worker drives the turn.
2. KB-grounded answer -- the front-line agent calls
   ``system__search_collection`` over a REAL embedder-backed collection and
   answers grounded on the refund-policy doc, citing its source path.
3. Soft-yield ``system__ask_user`` -- the front-line agent asks the customer
   for the charge amount INLINE. This does NOT park: the question surfaces as
   an ordinary assistant turn, ``chat.pending_tool_call`` (mode ``ask_user``)
   is recorded at ``turn_status=idle``, and the customer's next WS message is
   consumed as the tool_result (resume).
4. ``system__switch_to_agent`` handoff -- the front-line agent hands the chat
   off to a billing specialist; ``chat.agent_id`` repoints and the dispatch
   loop injects the handoff prompt as the specialist's first turn. The shared
   message history is preserved across the switch.
5. Chat tool-approval -- the specialist's refund action is gated behind a
   REQUIRED ``ToolApprovalPolicy``. The chat soft-yields on ``_approval``
   (mode ``approval``); the supervisor's next WS message resolves it
   conversationally -- an affirmative reply ("yes") RUNS the gated tool and a
   durable ``ToolApprovalRecord{decision:"approved"}`` is written, while a
   refusal ("no") rejects WITHOUT running it and records
   ``decision:"rejected"``.

Recipe: primerhq.github.io/docs_source/cookbook/tiered-help-desk.md

The gated "issue a refund" action is the built-in ``misc__uuid_v4`` (it runs
for real on approve and is re-dispatchable through the approval gate). In the
published recipe this stands in for a ``billing__issue_refund`` tool -- the
test pins the GATE MECHANISM, which is tool-agnostic.

Agent behaviour is scripted (deterministic mock LLM); the embedder, the
indexer, and the vector search are REAL. The approve/reject DECISION is
operator-driven (the supervisor's WS reply), never scripted into the LLM.
Assertions read from the chat message log (``GET /v1/chats/{id}/messages``,
the chat transcript) and the chat row (``GET /v1/chats/{id}``); a chat's
transcript lives in ``chat_messages`` rows, not an on-disk ``messages.jsonl``.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_tiered_help_desk.py -n0 -q
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_scripted_agent
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = [pytest.mark.asyncio, requires("embedder", "pgvector")]


# ---------------------------------------------------------------------------
# KB seeding (mirrors test_cookbook_support_desk.py)
# ---------------------------------------------------------------------------


def _embedder_cfg() -> dict:
    return load_config()["embedder"]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

_REFUND_PATH = "refund-policy.md"
_PASSWORD_PATH = "password.md"
_DOCS = {
    _REFUND_PATH: (
        "Refund policy for paid plans. To request money back, open the "
        "Billing page, choose the charge in question, and click Request "
        "Refund within 30 days of the invoice. Approved refunds return to "
        "the original payment method in 5 to 7 business days. Refunds above "
        "500 dollars require a supervisor sign-off."
    ),
    _PASSWORD_PATH: (
        "Resetting account credentials. Open id.company.com, click Forgot "
        "Password, enter your employee email, and follow the reset link. "
        "The reset link expires after 15 minutes."
    ),
}

_REFUND_QUERY = "how do I get my money back for a charge"

# The customer's verbatim answer to the inline ask_user question. The chat
# consumes it as the pending ask_user call's tool_result OUTPUT, and that
# output is the only thing the OpenAI ``tool`` message carries to the model
# (the ask_user mode/name is dropped at serialisation). The scripted
# front-line agent therefore discriminates its post-ask_user resume turn on
# this marker substring, not on ``"ask_user"``.
_ASK_USER_REPLY = "It was 900 dollars."
_ASK_USER_REPLY_MARKER = "900 dollars"


async def _make_embedder(client: httpx.AsyncClient, suffix: str) -> str:
    cfg = _embedder_cfg()
    eid = f"emb-thd-{suffix}"
    r = await client.post(
        "/v1/embedding_providers",
        json={
            "id": eid,
            "provider": "openai",
            "models": [{"name": cfg["model"]}],
            "config": {
                "url": cfg["base_url"],
                "api_key": cfg["api_key"],
                "flavor": "lmstudio",
            },
            "limits": {"max_concurrency": 2},
        },
    )
    assert r.status_code in (200, 201, 409), r.text
    return eid


async def _make_ssp(client: httpx.AsyncClient, suffix: str) -> str:
    sid = f"ssp-thd-{suffix}"
    r = await client.post(
        "/v1/ssp",
        json={"id": sid, "provider": "pgvector", "config": _PGVECTOR_DSN},
    )
    assert r.status_code in (200, 201, 409), r.text
    return sid


async def _seed_kb(client: httpx.AsyncClient, sfx: str) -> str:
    """Create the support KB collection + ingest the docs; return the cid."""
    eid = await _make_embedder(client, sfx)
    ssp_id = await _make_ssp(client, sfx)
    cid = f"kb-thd-{sfx}"
    cfg = _embedder_cfg()
    r = await client.post("/v1/collections", json={
        "id": cid,
        "description": "Tiered help-desk knowledge base.",
        "embedder": {"provider_id": eid, "model": cfg["model"]},
        "search_provider_id": ssp_id,
    })
    assert r.status_code in (200, 201), r.text
    for path, content in _DOCS.items():
        r = await client.put(
            f"/v1/collections/{cid}/documents",
            params={"path": path},
            json={"content": content},
        )
        assert r.status_code in (200, 201), r.text
    return cid


async def _search_with_retry(
    client: httpx.AsyncClient, cid: str, query: str, *,
    top_k: int = 3, attempts: int = 12, delay_s: float = 1.0,
) -> list[dict]:
    last: list[dict] = []
    for _ in range(attempts):
        r = await client.post(
            f"/v1/collections/{cid}/search",
            json={"query": query, "top_k": top_k},
        )
        assert r.status_code == 200, r.text
        last = r.json().get("hits", [])
        if last:
            return last
        await asyncio.sleep(delay_s)
    return last


# ---------------------------------------------------------------------------
# Chat WS helpers (mirror test_chats_ask_user_journey.py)
# ---------------------------------------------------------------------------


def _ws_url(
    client: httpx.AsyncClient, chat_id: str, *, cursor: int | None = None,
) -> str:
    http_url = str(client.base_url).rstrip("/")
    ws_origin = http_url.replace("http://", "ws://").replace(
        "https://", "wss://",
    )
    url = f"{ws_origin}/v1/chats/{chat_id}/ws"
    # Connecting at the live tail (cursor = current last_seq) suppresses the
    # replay flush of all prior rows. Without it, every reconnect replays the
    # whole transcript first; on a busy chat the client closes (after draining
    # a few frames) before the server has read the freshly-sent frame off the
    # wire, the replay write hits the closed socket and raises
    # WebSocketDisconnect, and the recv loop returns BEFORE persisting the
    # send -- silently dropping the message. Tailing removes the replay race.
    if cursor is not None:
        url += f"?cursor={cursor}"
    return url


def _ws_headers(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Forward the authenticated client's session cookie onto the WS
    handshake. The chat WS closes with 4401 unless the signed
    ``primer_session`` cookie is present."""
    pairs = [f"{c.name}={c.value}" for c in client.cookies.jar]
    if not pairs:
        return []
    return [("Cookie", "; ".join(pairs))]


async def _chat_last_seq(client: httpx.AsyncClient, chat_id: str) -> int:
    r = await client.get(f"/v1/chats/{chat_id}")
    if r.status_code != 200:
        return -1
    return int(r.json().get("last_seq", -1))


async def _send_user_message(
    client: httpx.AsyncClient, chat_id: str, content: str, *, drain: int = 6,
    confirm_attempts: int = 4,
) -> None:
    """Open a fresh WS, send a ``user_message`` frame, and CONFIRM it landed.

    We do NOT assert frame ordering here (cursor replay makes the first frame
    kind unstable across reconnects); delivery is verified out-of-band by
    polling ``GET /v1/chats/{id}`` until ``last_seq`` advances past the value
    observed before the send.

    Confirming + retrying the send is load-bearing: the WS recv loop and the
    client's close race on a fast loopback socket, so a fire-and-forget send
    can be dropped if the client closes the connection before the server's
    recv loop has read the frame off the wire. Each attempt reopens a fresh
    WS (cursor replay makes this safe -- the frame is only persisted when the
    server actually reads it) and we re-check ``last_seq`` until it moves.
    """
    import websockets

    before = await _chat_last_seq(client, chat_id)
    for _ in range(confirm_attempts):
        try:
            async with websockets.connect(
                # Tail from the current last_seq so there is no replay flush to
                # race the client's close against (see _ws_url).
                _ws_url(client, chat_id, cursor=max(before, 0)),
                additional_headers=_ws_headers(client),
            ) as ws:
                await ws.send(
                    json.dumps({"kind": "user_message", "content": content}),
                )
                for _ in range(drain):
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except (TimeoutError, Exception):  # noqa: BLE001
                        break
        except Exception:  # noqa: BLE001 - retry the connect on transient errors
            pass
        # Poll briefly for the persist to land (the server appends + bumps
        # last_seq on the recv path; turn rows bump it further).
        for _ in range(20):
            if await _chat_last_seq(client, chat_id) > before:
                return
            await asyncio.sleep(0.25)
    raise AssertionError(
        f"user_message {content!r} never persisted on chat {chat_id} "
        f"(last_seq stuck at {before})"
    )


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


def _tool_results(items: list[dict]) -> list[dict]:
    return [it for it in items if it.get("kind") == "tool_result"]


async def _wait_for_pending(
    client: httpx.AsyncClient, chat_id: str, *, expect_mode: str,
    timeout_s: float = 40.0, interval_s: float = 0.3,
) -> dict:
    """Poll GET /v1/chats/{id} until pending_tool_call is set + turn idle.

    Both conditions prove the soft-yield landed: the pending call recorded
    AND the turn returned to idle (the gate ended the turn, no park)."""
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
    client: httpx.AsyncClient, chat_id: str, *,
    timeout_s: float = 40.0, interval_s: float = 0.3,
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


async def _wait_for_agent(
    client: httpx.AsyncClient, chat_id: str, *, agent_id: str,
    timeout_s: float = 40.0, interval_s: float = 0.3,
) -> dict:
    """Poll until chat.agent_id == agent_id (the switch took effect)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/chats/{chat_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("agent_id") == agent_id:
                return last
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"chat {chat_id} agent_id never switched to {agent_id!r} within "
        f"{timeout_s}s; last_body={last!r}"
    )


# ---------------------------------------------------------------------------
# Approval policy seeding (mirrors test_chats_ask_user_journey.py)
# ---------------------------------------------------------------------------


_GATED_TOOL = "uuid_v4"  # stands in for billing__issue_refund


async def _seed_required_policy(
    client: httpx.AsyncClient, *, pol_id: str, tool_name: str,
) -> None:
    """Gate ``misc__{tool_name}`` behind a required-approval policy.

    Clears any pre-existing policy for the same (toolset, tool) first so
    concurrent iterations don't 409, then invalidates the resolver cache.
    """
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if (
                it.get("toolset_id") == "misc"
                and it.get("tool_name") == tool_name
            ):
                await client.delete(f"/v1/tool_approval_policies/{it['id']}")
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


# ---------------------------------------------------------------------------
# Scripted agents
# ---------------------------------------------------------------------------


async def _make_frontline(
    client, registry, base_url, *, suffix, cid, billing_agent_id, answer,
) -> dict:
    """Front-line agent: search KB -> answer + ask_user -> switch to billing.

    Turn-by-turn, discriminated by the message history the mock sees:
      1. no tool result yet               -> search_collection
      2. last tool result is search hits  -> ask_user (the inline question)
      3. last tool result is the ask_user reply (the consumed amount)
                                          -> switch_to_agent (handoff)
    The two ``when_tool_result=True`` turns are discriminated purely on the
    tool-result OUTPUT the model sees: the search hits carry the refund doc
    path; the consumed ask_user reply carries the customer's verbatim answer
    (``_ASK_USER_REPLY_MARKER``). The ask_user mode/name is NOT visible to the
    model -- the OpenAI ``tool`` message carries only the result output -- so
    we must key the handoff turn on the reply text, not on ``"ask_user"``.
    """
    return await make_scripted_agent(
        client, registry, base_url, suffix=suffix,
        scenario=f"scripted:{suffix}",
        tools=[
            "system__search_collection",
            "system__ask_user",
            "system__switch_to_agent",
        ],
        system_prompt=[
            "You are a front-line support agent. Search the KB, answer "
            "grounded and cite the doc path, ask the customer for the charge "
            "amount, then hand off to billing.",
        ],
        rules=[
            # 1. First turn: search the KB.
            Rule(
                when_tool_result=False,
                emit_tool="system__search_collection",
                emit_args={"collection_id": cid, "query": _REFUND_QUERY,
                           "top_k": 3},
                emit_tool_call_id="call_search",
            ),
            # 3. The ask_user reply was consumed as the pending call's
            #    tool_result; its content is the customer's verbatim reply
            #    (``"It was 900 dollars."``). Discriminate on that reply text
            #    -- the OpenAI ``tool`` message the model sees carries only the
            #    tool_result OUTPUT, never the ask_user mode/name, so we cannot
            #    key on ``"ask_user"`` here. Checked BEFORE the search rule
            #    (rule order = first match) so the handoff fires only after the
            #    customer answered the inline question.
            Rule(
                when_last_tool_result_contains=_ASK_USER_REPLY_MARKER,
                emit_tool="system__switch_to_agent",
                emit_args={"agent_id": billing_agent_id,
                           "prompt": "Customer wants a 900 dollar refund. "
                                     "Please process it."},
                emit_tool_call_id="call_switch",
            ),
            # 2. Search hits returned (the refund doc path is present): answer
            #    grounded + ask for the charge amount inline.
            Rule(
                when_last_tool_result_contains=_REFUND_PATH,
                emit_tool="system__ask_user",
                emit_args={"prompt": f"{answer} What is the charge amount?"},
                emit_tool_call_id="call_ask",
            ),
            # Fallback: a plain answer (should not normally be reached).
            Rule(when_tool_result=True, emit_text=answer),
        ],
    )


async def _make_billing(
    client, registry, base_url, *, suffix,
) -> dict:
    """Billing specialist: on the handoff turn, call the gated refund tool;
    after it resolves, report. The gated tool soft-yields on the chat for
    supervisor approval."""
    return await make_scripted_agent(
        client, registry, base_url, suffix=suffix,
        scenario=f"scripted:{suffix}",
        tools=[f"misc__{_GATED_TOOL}"],
        system_prompt=[
            "You are a billing specialist. Issue the refund the customer "
            "requested; large refunds require a supervisor sign-off.",
        ],
        rules=[
            # Handoff turn (no tool result yet): issue the refund (gated). A
            # DISTINCT tool_call id (not the shared "call_0") is load-bearing:
            # the chat already carries the front-line agent's earlier tool
            # calls, and the approval resume locates the supervisor's reply by
            # matching the pending call's id against the chat's tool_call rows.
            # A duplicated id would match the FIRST (front-line) call and
            # consume the wrong message as the decision.
            Rule(when_tool_result=False, emit_tool=f"misc__{_GATED_TOOL}",
                 emit_args={}, emit_tool_call_id="call_refund"),
            # After the gate resolves: report.
            Rule(when_tool_result=True,
                 emit_text="Your refund request has been processed."),
        ],
    )


# ===========================================================================
# Test
# ===========================================================================


@smk("SMK-COOKBOOK-13")
async def test_tiered_help_desk_chat_hitl(
    client: httpx.AsyncClient, mock_llm, unique_suffix, tmp_path,
):
    """Full chat-HITL desk over the WS: KB-grounded answer -> inline ask_user
    soft-yield -> switch_to_agent handoff -> supervisor-gated refund resolved
    BOTH ways (approve runs it + records approved; reject denies + records
    rejected)."""
    registry, base_url = mock_llm
    sfx = unique_suffix

    # ----- Seed the KB once; reused by both the approve and reject runs. ----
    cid = await _seed_kb(client, sfx)

    # Prove the KB grounding the front-line agent relies on is real: the refund
    # query ranks the refund-policy doc on top.
    hits = await _search_with_retry(client, cid, _REFUND_QUERY)
    assert hits, "real semantic search returned no hits for the KB"
    top_src = str(hits[0].get("meta", {}).get("document_name", ""))
    assert top_src == _REFUND_PATH, (
        f"refund query did not rank {_REFUND_PATH} first: "
        f"{[h.get('meta', {}).get('document_name') for h in hits]}"
    )

    answer = (
        "Open the Billing page, choose the charge, and click Request Refund "
        f"within 30 days. (Source: {_REFUND_PATH})"
    )

    async def _run_desk(*, decision: str) -> None:
        """Drive one full chat conversation; ``decision`` is the supervisor's
        WS reply ("yes" -> approve, "no" -> reject)."""
        local = f"{decision}-{sfx}"
        pol_id = f"thd-pol-{local}"
        billing = await _make_billing(
            client, registry, base_url, suffix=f"thd-bill-{local}",
        )
        frontline = await _make_frontline(
            client, registry, base_url, suffix=f"thd-fl-{local}",
            cid=cid, billing_agent_id=billing["agent_id"], answer=answer,
        )
        cleanup = [
            f"/v1/tool_approval_policies/{pol_id}",
            f"/v1/agents/{frontline['agent_id']}",
            f"/v1/llm_providers/{frontline['provider_id']}",
            f"/v1/agents/{billing['agent_id']}",
            f"/v1/llm_providers/{billing['provider_id']}",
        ]
        cid_chat: str | None = None
        try:
            await _seed_required_policy(
                client, pol_id=pol_id, tool_name=_GATED_TOOL,
            )

            # --- Open the chat bound to the front-line agent ---
            r = await client.post(
                "/v1/chats", json={"agent_id": frontline["agent_id"]},
            )
            assert r.status_code == 201, r.text
            cid_chat = r.json()["id"]
            cleanup.insert(0, f"/v1/chats/{cid_chat}")

            # === 1. Customer asks about a refund over the WS ===
            await _send_user_message(
                client, cid_chat, "I want a refund for a 900 dollar charge.",
            )

            # === 2. Front-line searched the KB, answered grounded, and
            #        soft-yielded on ask_user (NO park) ===
            body = await _wait_for_pending(
                client, cid_chat, expect_mode="ask_user",
            )
            assert body["turn_status"] == "idle", body
            # No park columns on the chat row -- the soft-yield proof.
            for parked_key in (
                "parked_status", "parked_state", "parked_event_key",
                "parked_until", "parked_at",
            ):
                assert parked_key not in body, (
                    f"chat row leaked park column {parked_key!r}: {body}"
                )

            items = await _messages(client, cid_chat)
            # The front-line searched and the refund doc surfaced.
            assert any(
                "system__search_collection" in json.dumps(it) for it in items
            ), f"front-line never searched the KB: {items!r}"
            assert any(
                _REFUND_PATH in json.dumps(tr.get("payload") or {})
                for tr in _tool_results(items)
            ), f"search did not surface {_REFUND_PATH}: {items!r}"
            # The grounded answer cited the source path AND the inline
            # ask_user question surfaced as an ordinary assistant turn.
            deltas = _delta_texts(items)
            assert any(_REFUND_PATH in d for d in deltas), (
                f"grounded answer did not cite {_REFUND_PATH}: {deltas}"
            )
            assert any("charge amount" in d for d in deltas), (
                f"inline ask_user question not surfaced: {deltas}"
            )

            # === 3. Customer answers the inline question -> consumed as the
            #        ask_user tool_result; front-line then hands off ===
            await _send_user_message(client, cid_chat, _ASK_USER_REPLY)

            # The switch took effect: chat.agent_id repointed to billing,
            # and the shared history was preserved (the prior rows are still
            # in the message log the specialist inherits).
            switched = await _wait_for_agent(
                client, cid_chat, agent_id=billing["agent_id"],
            )
            assert switched["agent_id"] == billing["agent_id"], switched
            # History preserved: the earlier KB answer + the customer's
            # original refund request are still in the transcript.
            after_switch = await _messages(client, cid_chat)
            joined = json.dumps(after_switch)
            assert _REFUND_PATH in joined, (
                "switch dropped the prior KB-grounded history"
            )
            assert any(
                "900 dollar charge" in str(it.get("payload") or {})
                for it in after_switch
            ), "switch dropped the customer's original request from history"

            # === 4. The specialist's refund tool tripped the approval gate;
            #        the chat soft-yielded on _approval (mode approval) ===
            gate = await _wait_for_pending(
                client, cid_chat, expect_mode="approval",
            )
            assert gate["pending_tool_call"]["mode"] == "approval", gate
            assert "parked_status" not in gate, gate

            # === 5. The SUPERVISOR resolves the gate over the WS ===
            await _send_user_message(client, cid_chat, decision)

            cleared = await _wait_for_cleared(client, cid_chat)
            assert cleared["pending_tool_call"] is None, cleared

            items = await _messages(client, cid_chat)
            trs = _tool_results(items)
            assert trs, f"no tool_result after the gate resolved: {items!r}"
            last_tr = trs[-1]
            payload = last_tr.get("payload") or {}
            if decision == "yes":
                # APPROVE: the gated refund tool actually RAN (no error).
                assert not payload.get("error"), (
                    f"approved refund did not run cleanly: {last_tr!r}"
                )
            else:
                # REJECT: the gated tool did NOT run; the result is a refusal.
                assert payload.get("error"), (
                    f"rejected refund still ran: {last_tr!r}"
                )
                assert "declin" in json.dumps(payload).lower(), last_tr

            # The durable approval audit trail: a ToolApprovalRecord for this
            # chat with the matching decision.
            rec_status = "approved" if decision == "yes" else "rejected"
            r = await client.get(
                "/v1/tool_approval/records",
                params={"status": rec_status, "length": 100},
            )
            assert r.status_code == 200, r.text
            ours = [
                rec for rec in r.json().get("items", [])
                if rec.get("chat_id") == cid_chat
            ]
            assert ours, (
                f"no {rec_status!r} ToolApprovalRecord for chat {cid_chat}: "
                f"{r.json().get('items')!r}"
            )
            assert ours[0]["decision"] == rec_status, ours[0]
        finally:
            await _cleanup(client, cleanup)

    try:
        # ---- APPROVE path: the gated refund runs. ----
        await _run_desk(decision="yes")
        # ---- REJECT path: the gated refund is denied. ----
        await _run_desk(decision="no")
    finally:
        await _cleanup(client, [
            f"/v1/collections/{cid}",
            f"/v1/ssp/ssp-thd-{sfx}",
            f"/v1/embedding_providers/emb-thd-{sfx}",
        ])
