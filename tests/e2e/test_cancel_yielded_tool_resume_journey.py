"""E2E: cancel-yielded-tool → resume cycle for ask_user.

Sibling to T0861 (operator approve), T0862 (ask_user respond), and
T0863 (timeout-as-rejection). T0864 closes the fourth corner of the
§7 resume contract: operator-initiated cancel-yielded-tool fires
the resume cycle through the YieldCancelled payload path.

Existing coverage of cancel-yielded-tool:
  * T0759/T0760/T0761 pin the park-time observable shapes (404,
    409, etc.).
  * test_yielding_tools_park_respond_then_park_cancel_journey
    pinned the bus → mark_resumable side but was quarantined
    in commit 10abef7 because it raced the new §7 resume cycle.
  * The END-TO-END cancel cycle through the bus + worker resume
    has NOT been pinned with a journey test — until now.

The cycle:
  1. Seed full agent ladder + Session (auto_start=False).
  2. asyncpg-inject ask_user park + session_leases at
     runnable=FALSE (mirrors scheduler.park_turn).
  3. POST /v1/sessions/{sid}/yields/{tcid}/cancel
     {reason: "user changed mind"} → 202.
     The cancel router publishes make_cancelled_payload onto
     the bus (primer.api.routers.yields:314).
  4. In-app YieldEventListener picks up the NOTIFY, calls
     scheduler.mark_resumable.
  5. Worker pool claim loop wakes, claims the row.
  6. _handle_resume → classify_resume_payload detects the
     __yield_cancelled__ marker → constructs a YieldCancelled
     instance → get_resume_hook("ask_user") → ask_user_resume
     synthesises a ToolCallResult carrying cancelled=True +
     cancel_reason (primer.toolset.misc:ask_user_resume).
  7. Worker wraps as ToolResultPart → inject_resume_messages →
     clear_park + complete_turn(RUNNING, re_enqueue=True).
  8. Test polls /v1/sessions/{sid} until parked_status=None.
  9. Asserts parked cleared + turn_no advanced.

Multi-subsystem in one test:
  * cancel-yielded-tool router (publishes YieldCancelled marker)
  * Postgres NOTIFY bus channel
  * YieldEventListener → scheduler.mark_resumable + claim
  * worker pool _handle_resume → registry path (ask_user_resume)
  * yield_runtime.classify_resume_payload → YieldCancelled
    instance
  * storage clear_park + complete_turn

Covers backlog item T0864. Together with T0861 / T0862 / T0863
the four flagship tests close the entire §7 resume contract:
  - T0861: operator approve (inline _approval branch)
  - T0862: ask_user respond (generic registry branch)
  - T0863: timeout-as-rejection (sweeper-style payload)
  - T0864: cancel-yielded-tool (YieldCancelled payload)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest


async def _pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost",
        port=5432,
        user="primer",
        password="primer",
        database="matrix_e2e",
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_ladder(
    client: httpx.AsyncClient, suffix: str, tmp_path,
) -> tuple[str, str, list[str]]:
    """Returns (session_id, workspace_id, cleanup_urls)."""
    pid = f"llm-t864-{suffix}"
    aid = f"ag-t864-{suffix}"
    wp_id = f"wp-t864-{suffix}"
    tpl_id = f"tpl-t864-{suffix}"

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

    r = await client.post(
        "/v1/agents",
        json={
            "id": aid,
            "description": "T0864 cancel-yielded-tool probe",
            "model": {"provider_id": pid, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent: {r.text}"

    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "tpl",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": aid},
            "auto_start": False,
        },
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    return sid, wid, cleanup_urls


async def _inject_ask_user_park_with_lease(
    *, session_id: str, tool_call_id: str, prompt: str,
) -> None:
    """Inject ask_user park + lease(runnable=FALSE) atomically."""
    event_key = f"ask_user:{session_id}:{tool_call_id}"
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state: dict[str, Any] = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "ask_user",
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "prompt": prompt,
            },
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    park_sql = """
        UPDATE sessions
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
    lease_sql = """
        INSERT INTO session_leases (session_id, runnable, next_attempt_at)
        VALUES ($1, FALSE, now())
        ON CONFLICT (session_id) DO UPDATE
            SET runnable = FALSE,
                worker_id = NULL,
                expires_at = NULL,
                next_attempt_at = now()
    """
    conn = await _pg()
    try:
        async with conn.transaction():
            await conn.execute(lease_sql, session_id)
            await conn.execute(
                park_sql,
                session_id,
                event_key,
                parked_until.isoformat(),
                now.isoformat(),
                json.dumps(parked_state),
            )
    finally:
        await conn.close()


async def _wait_for_resume(
    client: httpx.AsyncClient, session_id: str,
    *, timeout_s: float = 20.0, interval_s: float = 0.5,
) -> dict[str, Any]:
    import asyncio
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_body: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last_body = r.json()
            if last_body.get("parked_status") in (None, "null"):
                return last_body
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"session {session_id} did not finish resuming within "
        f"{timeout_s}s; last_body={last_body!r}"
    )


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            if url.endswith("/cancel"):
                await client.post(url)
            else:
                await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0864 — cancel-yielded-tool → resume cycle (YieldCancelled branch)
# ===========================================================================


@pytest.mark.asyncio
async def test_t0864_cancel_yielded_tool_publishes_and_resumes_session(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0864 — End-to-end cancel-yielded-tool cycle: park, POST
    /yields/{tcid}/cancel, the bus publishes YieldCancelled
    marker, worker picks up, resume cycle clears the park.

    Pinned invariants:
      * cancel-yielded-tool router POST /v1/sessions/{sid}/
        yields/{tcid}/cancel returns 202 {"status":"accepted"}.
      * The router publishes make_cancelled_payload({reason})
        onto the bus.
      * Bus listener routes to scheduler.mark_resumable.
      * Worker pool claim loop wakes, claims the row.
      * _handle_resume → classify_resume_payload detects the
        __yield_cancelled__ marker → constructs YieldCancelled
        with the reason.
      * get_resume_hook("ask_user") → ask_user_resume
        synthesises a ToolCallResult carrying cancelled=True +
        cancel_reason (primer.toolset.misc:ask_user_resume).
      * Worker wraps as ToolResultPart → inject_resume_messages
        → clear_park + complete_turn(RUNNING, re_enqueue=True).
      * Observable: parked_status=None, turn_no advanced.

    The synthesised tool_result's content (cancel reason
    plumbed through to the agent) is unit-tested at the
    function level in tests/toolset/test_ask_user.py
    ::test_resume_with_cancelled. This test pins the END-TO-END
    cycle: HTTP → bus → scheduler → worker → resume → clear_park.
    """
    sid, _wid, cleanup_urls = await _seed_ladder(
        client, unique_suffix, tmp_path,
    )
    tool_call_id = f"tc-t864-{unique_suffix}"
    cancel_reason = "user changed mind about the question"

    try:
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        initial_turn_no = r.json()["turn_no"]

        # ----- 1. Inject ask_user park + invisible lease ---------
        await _inject_ask_user_park_with_lease(
            session_id=sid,
            tool_call_id=tool_call_id,
            prompt="What is your favourite colour?",
        )

        # ----- 2. Sanity: /ask_user/pending sees the park --------
        r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
        assert r.status_code == 200, r.text
        assert r.json()["tool_call_id"] == tool_call_id, r.text

        # ----- 3. POST /yields/{tcid}/cancel → 202 -------------
        r = await client.post(
            f"/v1/sessions/{sid}/yields/{tool_call_id}/cancel",
            json={"reason": cancel_reason},
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted"}, r.text

        # ----- 4. Poll until resume cycle completes -------------
        body = await _wait_for_resume(client, sid)

        # parked_state cleared.
        assert body["parked_status"] is None, body
        assert body.get("parked_state") in (None, {}), body
        assert body.get("parked_event_key") in (None, ""), body

        # turn_no advanced (complete_turn(RUNNING, re_enqueue=True)).
        final_turn_no = body["turn_no"]
        assert final_turn_no > initial_turn_no, (
            f"turn_no didn't advance through resume: "
            f"initial={initial_turn_no}, final={final_turn_no}"
        )

        # No /errors/internal envelope leak.
        assert "/errors/internal" not in json.dumps(body), body
    finally:
        await _cleanup(client, cleanup_urls)
