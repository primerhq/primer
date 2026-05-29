"""E2E: full park → respond → resume cycle for the §2 approval gate.

Flagship test for the roadmap §7 worker-pool resume wiring landed
in commit 731a05b. Earlier API-loop tests (T0833/T0835/T0836/T0850)
pin the park-time observable contract; T0858/T0859 pin the
multi-strategy CRUD + chat-side respond. None of them assert the
END-TO-END cycle that's just become possible — operator answers
the gate, the bus event fires, the worker pool resumes, the
session's parked columns clear, and turn_no advances.

This test walks that cycle:

  1. Seed LLMProvider + Agent + workspace ladder + Session
     (auto_start=False — no LLM call ever fires).
  2. Inject an _approval park onto the session row via asyncpg
     (same shape T0833 uses; the park's `original_call` points at
     a workspace tool the bypass_approval dispatch can actually
     execute).
  3. GET /v1/sessions/{id}/tool_approval/pending — sanity check
     the park is observable.
  4. POST /v1/sessions/{id}/tool_approval/respond
     {tool_call_id, decision: approved} → 202. This publishes
     onto the event bus, which fires mark_resumable, which
     re-arms the lease, which wakes the worker pool.
  5. Poll GET /v1/sessions/{id} until parked_status is None,
     bounded 20s timeout. Resume happens in-process (the bringup
     runs `primer api --run-worker`), so the cycle completes
     synchronously plus poll latency.
  6. Assert parked_state has been cleared AND turn_no has
     advanced from the parked turn — both observable signals
     that the resume branch consumed the park.

Multi-subsystem in one test:
  * tool_approval respond router (POST publishes to event_bus)
  * event_bus (Postgres LISTEN/NOTIFY in bringup config)
  * scheduler (mark_resumable flips parked→resumable, re-arms lease)
  * worker pool claim loop (picks up the resumable row)
  * worker pool _run_one_turn → _handle_resume (the just-wired
    branch — invokes _resume_tool_approval, persists tool_result,
    calls clear_park + complete_turn(RUNNING, re_enqueue=True))
  * storage (asyncpg JSONB updates round-trip)

Caveat: the next normal claim after resume will try to drive an
LLM turn against the seeded provider's bogus URL
(http://127.0.0.1:9999). That fails fast and the row terminates
in ENDED-failed. We don't assert on that — only on the resume
half completing (parked clears, turn advances). The LLM-side is
LM-Studio-gated and tracked separately by T0850.

Covers backlog item T0861. First test on the new end-to-end
contract; previous resume-side assertions were all in-process
unit tests against a mocked scheduler.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest


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
# Seed helpers — minimum infra to create a session bound to an agent.
# ---------------------------------------------------------------------------


async def _seed_ladder(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> tuple[str, str, list[str]]:
    """Returns (session_id, workspace_id, cleanup_urls)."""
    pid = f"llm-t861-{unique_suffix}"
    aid = f"ag-t861-{unique_suffix}"
    wp_id = f"wp-t861-{unique_suffix}"
    tpl_id = f"tpl-t861-{unique_suffix}"

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
            "description": "T0861 approval-resume probe",
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
            "config": {"kind": "local", "root_path": str(tmp_path)},
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


async def _inject_approval_park(
    *,
    session_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict,
) -> None:
    """Stamp parked_status=parked + _approval parked_state onto the
    session row. Same shape the production approval gate writes.
    """
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": f"tool_approval:{session_id}:{tool_call_id}",
            "timeout": 600.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "original_call": {
                    "id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                },
                "policy_id": "p-t861",
                "approval_type": "required",
                "gate_reason": "operator gate (T0861)",
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
    # Sessions created with auto_start=False don't have a session_leases
    # row yet (the lease is created on the first scheduler.enqueue,
    # which auto_start triggers). Without a lease row, mark_resumable's
    # UPDATE on session_leases is a no-op and the worker pool never
    # picks up the resumable session — even though sessions.data is
    # correctly marked.
    #
    # Insert a lease row with runnable=FALSE so the row is invisible
    # to claimers UNTIL the respond router's mark_resumable flips it
    # to TRUE. This mirrors the state the scheduler.park_turn would
    # leave the lease in if the park had happened via the real worker
    # path.
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
                parked_state["yielded"]["event_key"],
                parked_until.isoformat(),
                now.isoformat(),
                json.dumps(parked_state),
            )
    finally:
        await conn.close()


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            if url.endswith("/cancel"):
                await client.post(url)
            else:
                await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


async def _wait_for_resume(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 20.0,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    """Poll GET /v1/sessions/{id} until parked_status is None.

    Returns the final session JSON. Raises AssertionError if the
    park doesn't clear within the timeout (worker pool wasn't able
    to consume the resumable row).
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout_s
    last_body: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            body = r.json()
            last_body = body
            if body.get("parked_status") in (None, "null"):
                return body
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"session {session_id} did not finish resuming within "
        f"{timeout_s}s; last_body={last_body!r}"
    )


# ===========================================================================
# T0861 — full park → respond approved → resume cycle clears park
# ===========================================================================


@pytest.mark.asyncio
async def test_t0861_approval_park_respond_resume_clears_park_and_advances_turn(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0861 — End-to-end approval resume cycle: park, respond
    approved, worker pool resumes, parked columns clear, turn_no
    advances.

    Pinned invariants:
      * The respond router publishes onto the event bus.
      * The bus listener (Postgres LISTEN/NOTIFY in bringup config)
        triggers scheduler.mark_resumable.
      * mark_resumable re-arms the lease and the worker pool's claim
        loop picks the row up.
      * _run_one_turn detects parked_status='resumable' and routes
        to _handle_resume.
      * _handle_resume runs _resume_tool_approval (special-case),
        persists the synthetic tool_result, calls clear_park, and
        completes the turn with re_enqueue=True.
      * Observable state: parked_status=None, turn_no > parked_turn.

    The post-resume LLM turn fails on the seeded provider's bogus
    URL (http://127.0.0.1:9999), so the row terminates in
    ENDED-failed shortly after. We don't assert on the terminal
    state — only on the resume cycle completing. The LLM-side path
    is LM-Studio-gated (T0850).
    """
    sid, wid, cleanup_urls = await _seed_ladder(
        client, unique_suffix, tmp_path,
    )
    tool_call_id = f"tc-t861-{unique_suffix}"

    try:
        # Initial turn_no for the seeded session.
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        initial_turn_no = r.json()["turn_no"]

        # ----- 1. Inject _approval park onto the session row -----
        # Pick a tool the bypass_approval re-dispatch can actually
        # invoke without exploding. _workspaces__exec wires through
        # to the local workspace's exec handler; with empty cmd it
        # fails fast as a normal ToolResult(error=True) — which is
        # fine, the resume cycle still completes.
        await _inject_approval_park(
            session_id=sid,
            tool_call_id=tool_call_id,
            tool_name="_workspaces__exec",
            arguments={"command": ["true"]},
        )

        # ----- 2. Sanity: park is observable via /pending -----
        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        assert r.json()["tool_call_id"] == tool_call_id

        # ----- 3. POST respond {approved} -----
        r = await client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={
                "tool_call_id": tool_call_id,
                "decision": "approved",
            },
        )
        assert r.status_code == 202, r.text

        # ----- 4. Poll until parked_status clears (resume done) ---
        body = await _wait_for_resume(client, sid)

        # parked_state should be cleared too (clear_park drops every
        # parked_* column).
        assert body["parked_status"] is None, body
        assert body.get("parked_state") in (None, {}), body
        assert body.get("parked_event_key") in (None, ""), body

        # turn_no advanced — complete_turn(RUNNING, re_enqueue=True)
        # bumps it by 1.
        final_turn_no = body["turn_no"]
        assert final_turn_no > initial_turn_no, (
            f"turn_no didn't advance through resume: "
            f"initial={initial_turn_no}, final={final_turn_no}"
        )

        # No /errors/internal envelope leak.
        assert "/errors/internal" not in json.dumps(body), body
    finally:
        await _cleanup(client, cleanup_urls)
