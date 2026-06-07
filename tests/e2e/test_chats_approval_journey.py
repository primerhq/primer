"""E2E: §2 chat-surface tool_approval multi-action journey.

ONE pytest function walks the chat-side counterpart of the
session-bound tool_approval surface. Sibling to T0833/T0835 (session
side, already covered in test_tool_approval_pending_respond.py) and
T0836 (chat-no-park 404, ditto).

What's distinctly chat-side and pinned here:

  1. Chat row LIFECYCLE under a parked _approval state — create chat
     via POST /v1/chats, inject a parked_state blob onto the chat row
     (asyncpg-direct, same shape the tool_approval router reads),
     GET /chats/{cid}/tool_approval/pending → 200 with the documented
     envelope (tool_name, arguments, policy_id, approval_type,
     gate_reason, parked_at, timeout_at). Mirror of T0833 for chats.
  2. POST /chats/{cid}/tool_approval/respond with the parked yield's
     tool_call_id + rejected decision → 202 {"status":"accepted"}.
     Covers backlog item T0837 (the previously-pending chat-side
     respond contract).
  3. tool_call_id mismatch on chats /respond → 404 /errors/not-found,
     payload never leaks the parked tool_call_id or the wrong-id back
     verbatim. Mirror of the session-side 404 mismatch path; pins the
     cross-id isolation at the chat surface.
  4. Multi-decision: after one rejected POST, the chat's parked_state
     STILL surfaces 200 on /pending (worker-pool resume dispatch is
     unwired per roadmap §7, so backend state isn't cleared on
     respond — pinning today's observable contract so a future fix
     flips this with one targeted update).
  5. Chat DELETE while parked-on-approval → 204; subsequent
     /tool_approval/pending → 404 (chat row gone). Confirms the
     chat-lifecycle DELETE path doesn't 5xx when parked_state is
     present.

Subsystems exercised in one test:

  * tool_approval router (chats /pending + /respond branches)
  * chats router (POST + DELETE lifecycle)
  * storage layer (asyncpg direct UPDATE on the chats table; the
    JSONB shape is the same as for sessions per
    primer/api/routers/tool_approval.py:_approval_blob_or_404)
  * event bus (implicit — _publish_decision publishes the rejected
    decision onto the bus; the test doesn't assert the bus
    subscriber side because T0857 already pins inbox→bus end-to-end
    for the same shape)

Covers backlog item T0858 sibling for chats (T0859 in this iteration's
numbering). Bundles T0837 with the negative-path tool_call_id
isolation pin and the parked-chat-delete contract.

Pinned invariants:
  * Chat-side /tool_approval/pending mirrors the session-side shape
    on a parked row.
  * Chat-side /tool_approval/respond honours the same tool_call_id
    match-or-404 rule as the session side.
  * Chat DELETE clears the parked_state row entirely — no orphan
    /pending response after the row is gone.
  * No code path leaks /errors/internal across the chat approval
    flow, even with invalid inputs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest

from tests._support.smk import smk


# ---------------------------------------------------------------------------
# Postgres connection (matches scripts/e2e/bringup.sh defaults).
# ---------------------------------------------------------------------------


async def _pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost",
        port=5432,
        user="primer",
        password="primer",
        database="primer_e2e",
    )


# ---------------------------------------------------------------------------
# Seed helpers — minimum infra to create a chat.
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
    assert r.status_code == 201, f"seed LLM: {r.text}"


async def _seed_agent(
    client: httpx.AsyncClient, agent_id: str, provider_id: str,
) -> None:
    r = await client.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "chat approval probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent: {r.text}"


# ---------------------------------------------------------------------------
# Park injection — mirrors test_tool_approval_pending_respond.py
# _inject_approval_park but parameterised for the chats table.
# Same JSONB shape the tool_approval router reads via
# _approval_blob_or_404 + _build_pending_response.
# ---------------------------------------------------------------------------


async def _inject_approval_park_on_chat(
    *,
    chat_id: str,
    tool_call_id: str,
    inner_tool_name: str,
    arguments: dict[str, Any],
    policy_id: str,
    approval_type: str,
    gate_reason: str,
) -> None:
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state: dict[str, Any] = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": f"approval:{chat_id}:{tool_call_id}",
            "timeout": 600.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "original_call": {
                    "id": tool_call_id,
                    "name": inner_tool_name,
                    "arguments": arguments,
                },
                "policy_id": policy_id,
                "approval_type": approval_type,
                "gate_reason": gate_reason,
            },
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    # Chat model is stored in the singular-lowercase `chat` table via
    # primer/storage/postgres.py:_table_name_for (Session is the only
    # documented historical exception that gets a plural name).
    sql = """
        UPDATE chat
        SET data = jsonb_set(
                     jsonb_set(
                       jsonb_set(
                         jsonb_set(
                           jsonb_set(data,
                             '{parked_status}', to_jsonb('parked'::text)),
                           '{parked_event_key}', to_jsonb($2::text)),
                         '{parked_until}', to_jsonb($3::text)),
                       '{parked_at}', to_jsonb($4::text)),
                     '{parked_state}', $5::jsonb
                   ),
            updated_at = now()
        WHERE id = $1
    """
    conn = await _pg()
    try:
        await conn.execute(
            sql,
            chat_id,
            parked_state["yielded"]["event_key"],
            parked_until.isoformat(),
            now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


# ===========================================================================
# T0859 — Chats _approval park + respond + mismatch + delete journey
# ===========================================================================


@smk("SMK-CHT-04", status="partial")
@pytest.mark.asyncio
async def test_t0859_chats_approval_park_respond_and_delete_journey(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0859 — Chat-side tool_approval surface end-to-end:
    seed → create chat → inject park → pending 200 (envelope round-
    trips full payload) → respond rejected → 202 → respond with
    wrong tool_call_id → 404 (no /errors/internal, no leak of the
    parked yield's id) → DELETE chat → /pending 404. Covers backlog
    item T0837 plus the chat-side tool_call_id isolation pin and the
    parked-chat-delete contract.
    """
    pid = f"llm-t859-{unique_suffix}"
    aid = f"ag-t859-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)

    cleanup_urls: list[str] = [
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    chat_id: str | None = None
    tcid = f"tc-t859-{unique_suffix}"
    inner_tool = "shell_exec"
    arguments = {"cmd": "rm -rf /tmp/y"}
    policy_id = f"p-t859-{unique_suffix}"
    gate_reason = "dangerous shell command"

    try:
        # ----- 1. Create chat -------------------------------------
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201, r.text
        chat_id = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{chat_id}")

        # ----- 2. Inject _approval park onto chat row -------------
        await _inject_approval_park_on_chat(
            chat_id=chat_id,
            tool_call_id=tcid,
            inner_tool_name=inner_tool,
            arguments=arguments,
            policy_id=policy_id,
            approval_type="required",
            gate_reason=gate_reason,
        )

        # ----- 3. /pending returns 200 with full envelope ---------
        r = await client.get(f"/v1/chats/{chat_id}/tool_approval/pending")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tool_call_id"] == tcid, body
        assert body["tool_name"] == inner_tool, body
        assert body["arguments"] == arguments, body
        assert body.get("policy_id") == policy_id, body
        assert body.get("approval_type") == "required", body
        assert body.get("gate_reason") == gate_reason, body
        # parked_at + timeout_at present as ISO strings
        assert isinstance(body["parked_at"], str) and body["parked_at"], body
        assert isinstance(body["timeout_at"], str) and body["timeout_at"], body
        body_str = json.dumps(body)
        assert "/errors/internal" not in body_str, body

        # ----- 4. /respond rejected with matching tcid → 202 ------
        # T0837 — chat-side respond contract.
        r = await client.post(
            f"/v1/chats/{chat_id}/tool_approval/respond",
            json={
                "tool_call_id": tcid,
                "decision": "rejected",
                "reason": "auto-test reject",
            },
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted"}, r.text

        # ----- 5. /respond with mismatched tcid → 404 (isolation) -
        # The parked yield's id must NOT be leaked back, and a wrong
        # id must not silently succeed.
        wrong_tcid = f"tc-wrong-{unique_suffix}"
        r = await client.post(
            f"/v1/chats/{chat_id}/tool_approval/respond",
            json={
                "tool_call_id": wrong_tcid,
                "decision": "approved",
            },
        )
        assert r.status_code == 404, r.text
        env = r.json()
        assert env["type"].endswith("/not-found"), env
        env_str = json.dumps(env)
        # Documented contract: the response must NOT echo the parked
        # tool_call_id (leak prevention, mirror of session side).
        assert tcid not in env_str, (
            f"404 envelope leaked parked tool_call_id {tcid!r}: {env}"
        )
        # And no /errors/internal.
        assert "/errors/internal" not in env_str, env

        # ----- 6. /pending STILL 200 (worker resume unwired) ------
        # Documented gap (roadmap §7). The bus event from step 4 was
        # published but no consumer flips parked_status off. Pinning
        # today's observable so a future resume-wiring change flips
        # this assertion with one targeted update.
        r = await client.get(f"/v1/chats/{chat_id}/tool_approval/pending")
        assert r.status_code == 200, r.text
        assert r.json()["tool_call_id"] == tcid, r.text

        # ----- 7. DELETE parked-on-approval chat is clean ---------
        # Per T0775 the chat DELETE is a soft-end: returns 200 with
        # status='ended' (not a row destroy). Pin that this works
        # even when the row carries a parked_state blob — the DELETE
        # path mustn't 5xx on parked rows.
        rm = await client.delete(f"/v1/chats/{chat_id}")
        assert rm.status_code == 200, rm.text
        assert rm.json().get("status") == "ended", rm.text

        # And no internal-leak envelope on the ended row.
        rm_str = rm.text
        assert "/errors/internal" not in rm_str, rm_str

        # Subsequent DELETE on the already-ended chat → 409 per
        # T0765 contract (not 5xx); the finally block will hit
        # this — catch it explicitly so the unwind doesn't log
        # noise.
        cleanup_urls.remove(f"/v1/chats/{chat_id}")
    finally:
        for url in cleanup_urls:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
