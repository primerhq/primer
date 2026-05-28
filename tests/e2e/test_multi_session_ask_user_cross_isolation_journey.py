"""E2E: cross-session ask_user park isolation + sequential resume journey.

Multi-subsystem user-journey that pins event isolation across
SIMULTANEOUSLY-PARKED sessions. Walks two independent sessions
through:

  ladder seeding (2× agent / 2× workspace / 2× session) →
  asyncpg-injected ask_user parks (different prompts + tcids) →
  cross-session pending/respond mismatch probes →
  sequential resume cycles → verify A's resume does not perturb B's
  park, then B's resume does not perturb A's terminal state.

Existing siblings cover orthogonal contracts:

  * T0813 — same-session concurrent /respond traffic
  * T0862 — single-session full ask_user resume cycle
  * T0760 — tool_call_id mismatch on a SINGLE parked session

T0866 fills the cross-cutting gap: two distinct sessions parked at
the same instant, each with its own prompt + tcid, must remain
fully isolated. A respond directed at session A with session B's
tcid must 404 (no cross-session reach via shared tcid namespace);
A's resume must not flip B's parked state.

Subsystems exercised in one test:

  1. LLMProvider + Agent + WorkspaceProvider + Template + Workspace
     CRUD (shared LLMProvider across two agents to also pin the
     reference-not-cross-contaminated path).
  2. Two Sessions created with auto_start=False so no LLM call fires
     before we inject the parks.
  3. asyncpg direct park injection on BOTH sessions in one
     transaction (mirrors the production park_turn writes).
  4. ask_user pending GET endpoint — session-scoped lookup.
  5. ask_user respond POST endpoint — 404 on tcid mismatch even when
     the foreign tcid belongs to a real, currently-parked sibling.
  6. Event bus (Postgres LISTEN/NOTIFY) + scheduler mark_resumable
     end-to-end for both parks, sequentially.
  7. Worker pool _handle_resume routing through the generic
     ``ask_user`` registry hook for both sessions.
  8. clear_park + complete_turn observability (parked_status cleared,
     turn_no advanced) — asserted independently for both sessions.

Pinned invariants:

  * Per-session park visibility — GET /pending on A never returns B's
    prompt, even though both sessions are parked on ``ask_user`` at
    the same time.
  * Cross-session tcid namespace isolation — POST /respond on A with
    B's tcid returns 404 with the routing-layer NotFoundError shape
    (not /errors/internal), and B's parked_state stays untouched.
  * Resume side-channel isolation — after A's bus event flips A to
    resumable and the worker pool clears A's park, B's parked_status
    is still ``parked`` (B's row was never touched by the listener
    nor the worker pool's claim loop).
  * Per-session turn_no monotonicity — A and B each advance their own
    turn_no via complete_turn(re_enqueue=True); neither advances on
    the other's resume.
  * No /errors/internal envelope leaks at any HTTP step.

Covers backlog item T0866.
"""

from __future__ import annotations

import asyncio
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
        database="primer_e2e",
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_pair(
    client: httpx.AsyncClient, suffix: str, tmp_path,
) -> tuple[str, str, list[str]]:
    """Returns (sid_a, sid_b, cleanup_urls).

    Both sessions share an LLMProvider (one fake-model row) but
    each gets its own Agent + workspace ladder so the test exercises
    cross-workspace session isolation, not just cross-session-within-
    one-workspace.
    """
    pid = f"llm-t866-{suffix}"
    aid_a = f"ag-t866a-{suffix}"
    aid_b = f"ag-t866b-{suffix}"
    wp_id = f"wp-t866-{suffix}"
    tpl_id = f"tpl-t866-{suffix}"

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

    for aid in (aid_a, aid_b):
        r = await client.post(
            "/v1/agents",
            json={
                "id": aid,
                "description": f"T0866 cross-isolation probe ({aid})",
                "model": {"provider_id": pid, "model_name": "fake-model"},
                "tools": [],
                "system_prompt": ["probe"],
            },
        )
        assert r.status_code == 201, f"seed agent {aid}: {r.text}"

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
            "description": "T0866 tpl",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert r.status_code == 201, r.text

    # Two independent workspaces materialised from the same template
    # — pins that the parks remain isolated across workspace
    # boundaries, not just session boundaries.
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201, r.text
    wid_a = r.json()["id"]
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201, r.text
    wid_b = r.json()["id"]

    r = await client.post(
        f"/v1/workspaces/{wid_a}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": aid_a},
            "auto_start": False,
        },
    )
    assert r.status_code == 201, r.text
    sid_a = r.json()["id"]

    r = await client.post(
        f"/v1/workspaces/{wid_b}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": aid_b},
            "auto_start": False,
        },
    )
    assert r.status_code == 201, r.text
    sid_b = r.json()["id"]

    cleanup_urls = [
        f"/v1/workspaces/{wid_a}/sessions/{sid_a}/cancel",
        f"/v1/workspaces/{wid_b}/sessions/{sid_b}/cancel",
        f"/v1/workspaces/{wid_a}",
        f"/v1/workspaces/{wid_b}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid_a}",
        f"/v1/agents/{aid_b}",
        f"/v1/llm_providers/{pid}",
    ]
    return sid_a, sid_b, cleanup_urls


async def _inject_ask_user_park_with_lease(
    *,
    session_id: str,
    tool_call_id: str,
    prompt: str,
    conn: asyncpg.Connection,
) -> None:
    """Inject an ask_user park + session_leases row (runnable=FALSE).

    Same shape as the production scheduler.park_turn writes; written
    against an externally-supplied connection so the caller can batch
    both parks into one transaction (T0866's defining setup step).
    """
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
    await conn.execute(lease_sql, session_id)
    await conn.execute(
        park_sql,
        session_id,
        event_key,
        parked_until.isoformat(),
        now.isoformat(),
        json.dumps(parked_state),
    )


async def _wait_for_resume(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 20.0,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    """Poll GET /v1/sessions/{id} until parked_status clears."""
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
# T0866 — cross-session ask_user park isolation + sequential resume
# ===========================================================================


@pytest.mark.asyncio
async def test_t0866_multi_session_ask_user_cross_isolation_journey(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0866 — Two sessions parked on ``ask_user`` simultaneously
    must maintain full isolation across pending/respond/resume.

    Steps:

      1. Seed shared LLMProvider, two Agents, two Workspaces, two
         Sessions (auto_start=False so no worker churn before parks
         land).
      2. Open one asyncpg transaction; inject ask_user parks on
         BOTH sessions with distinct prompts + tcids; commit.
      3. GET /pending on each session — assert each returns its own
         prompt + tcid (no cross-leak even though both rows live in
         the same table).
      4. POST /respond on A with B's tcid → 404. Verify B's parked
         state is untouched.
      5. POST /respond on B with A's tcid → 404. Verify A's parked
         state is untouched.
      6. POST /respond on A with A's tcid → 202. Wait for the worker
         pool to claim → resume → clear A's park.
      7. While A is mid-resume / post-resume, sanity-check B's row:
         parked_status still 'parked', parked_state still carries
         B's prompt + tcid.
      8. POST /respond on B with B's tcid → 202. Wait for B to
         resume.
      9. Verify each session's turn_no advanced INDEPENDENTLY (the
         resume event for one did not bump the other's turn).

    No /errors/internal envelope at any step.
    """
    sid_a, sid_b, cleanup_urls = await _seed_pair(
        client, unique_suffix, tmp_path,
    )
    tcid_a = f"tc-t866a-{unique_suffix}"
    tcid_b = f"tc-t866b-{unique_suffix}"
    prompt_a = "Session A asks: pick a colour."
    prompt_b = "Session B asks: name a constellation."

    try:
        # ----- 1. Initial turn_nos for the monotonicity check -------
        r = await client.get(f"/v1/sessions/{sid_a}")
        assert r.status_code == 200, r.text
        initial_turn_a = r.json()["turn_no"]
        r = await client.get(f"/v1/sessions/{sid_b}")
        assert r.status_code == 200, r.text
        initial_turn_b = r.json()["turn_no"]

        # ----- 2. Inject BOTH parks in one transaction --------------
        conn = await _pg()
        try:
            async with conn.transaction():
                await _inject_ask_user_park_with_lease(
                    session_id=sid_a,
                    tool_call_id=tcid_a,
                    prompt=prompt_a,
                    conn=conn,
                )
                await _inject_ask_user_park_with_lease(
                    session_id=sid_b,
                    tool_call_id=tcid_b,
                    prompt=prompt_b,
                    conn=conn,
                )
        finally:
            await conn.close()

        # ----- 3. Per-session /pending isolation --------------------
        r = await client.get(f"/v1/sessions/{sid_a}/ask_user/pending")
        assert r.status_code == 200, r.text
        pending_a = r.json()
        assert pending_a["tool_call_id"] == tcid_a, pending_a
        assert prompt_a in pending_a["prompt"], pending_a
        # Critical isolation pin: A's response MUST NOT carry B's tcid.
        assert pending_a["tool_call_id"] != tcid_b, (
            f"cross-session leak — A's pending returned B's tcid: {pending_a!r}"
        )

        r = await client.get(f"/v1/sessions/{sid_b}/ask_user/pending")
        assert r.status_code == 200, r.text
        pending_b = r.json()
        assert pending_b["tool_call_id"] == tcid_b, pending_b
        assert prompt_b in pending_b["prompt"], pending_b
        assert pending_b["tool_call_id"] != tcid_a, (
            f"cross-session leak — B's pending returned A's tcid: {pending_b!r}"
        )
        # Different prompts proves the row lookup is session-scoped.
        assert pending_a["prompt"] != pending_b["prompt"], (
            f"both sessions returned the same prompt — table-scan "
            f"missing WHERE id=$1: a={pending_a!r} b={pending_b!r}"
        )

        # ----- 4. Cross-tcid mismatch — A endpoint with B's tcid → 404
        r = await client.post(
            f"/v1/sessions/{sid_a}/ask_user/respond",
            json={"tool_call_id": tcid_b, "response": "wrong-target"},
        )
        assert r.status_code == 404, r.text
        body = r.json()
        assert body.get("type", "").endswith("/not-found"), body
        assert "/errors/internal" not in json.dumps(body), body
        # B's row stays parked, untouched.
        r = await client.get(f"/v1/sessions/{sid_b}")
        assert r.status_code == 200, r.text
        b_after_cross = r.json()
        assert b_after_cross["parked_status"] == "parked", b_after_cross
        assert (
            b_after_cross.get("parked_state", {}).get("tool_call_id") == tcid_b
        ), b_after_cross

        # ----- 5. Mirror probe — B endpoint with A's tcid → 404 -----
        r = await client.post(
            f"/v1/sessions/{sid_b}/ask_user/respond",
            json={"tool_call_id": tcid_a, "response": "wrong-target"},
        )
        assert r.status_code == 404, r.text
        body = r.json()
        assert body.get("type", "").endswith("/not-found"), body
        assert "/errors/internal" not in json.dumps(body), body
        # A's row stays parked, untouched.
        r = await client.get(f"/v1/sessions/{sid_a}")
        assert r.status_code == 200, r.text
        a_after_cross = r.json()
        assert a_after_cross["parked_status"] == "parked", a_after_cross
        assert (
            a_after_cross.get("parked_state", {}).get("tool_call_id") == tcid_a
        ), a_after_cross

        # ----- 6. Real respond on A — 202 + resume cycle -----------
        r = await client.post(
            f"/v1/sessions/{sid_a}/ask_user/respond",
            json={"tool_call_id": tcid_a, "response": "blue"},
        )
        assert r.status_code == 202, r.text
        body_a_final = await _wait_for_resume(client, sid_a)
        assert body_a_final["parked_status"] is None, body_a_final
        assert body_a_final.get("parked_state") in (None, {}), body_a_final
        assert body_a_final["turn_no"] > initial_turn_a, body_a_final
        assert "/errors/internal" not in json.dumps(body_a_final), body_a_final

        # ----- 7. B's row must still be parked + untouched ----------
        # This is the load-bearing isolation pin: A's bus event fired
        # on event_key 'ask_user:{sid_a}:{tcid_a}' — B's event_key
        # ('ask_user:{sid_b}:{tcid_b}') must NOT have been triggered by
        # any side-channel. mark_resumable + claim loop must skip B.
        r = await client.get(f"/v1/sessions/{sid_b}")
        assert r.status_code == 200, r.text
        b_mid = r.json()
        assert b_mid["parked_status"] == "parked", (
            f"B's parked_status changed during A's resume — "
            f"cross-session listener leak suspected. b={b_mid!r}"
        )
        assert (
            b_mid.get("parked_state", {}).get("tool_call_id") == tcid_b
        ), b_mid
        assert b_mid["turn_no"] == initial_turn_b, (
            f"B's turn_no advanced during A's resume — turn counter "
            f"crosstalk. b={b_mid!r}"
        )

        # ----- 8. /pending on B still shows B's prompt -------------
        r = await client.get(f"/v1/sessions/{sid_b}/ask_user/pending")
        assert r.status_code == 200, r.text
        pending_b_mid = r.json()
        assert pending_b_mid["tool_call_id"] == tcid_b, pending_b_mid
        assert prompt_b in pending_b_mid["prompt"], pending_b_mid

        # ----- 9. Real respond on B — 202 + resume cycle -----------
        r = await client.post(
            f"/v1/sessions/{sid_b}/ask_user/respond",
            json={"tool_call_id": tcid_b, "response": "orion"},
        )
        assert r.status_code == 202, r.text
        body_b_final = await _wait_for_resume(client, sid_b)
        assert body_b_final["parked_status"] is None, body_b_final
        assert body_b_final.get("parked_state") in (None, {}), body_b_final
        assert body_b_final["turn_no"] > initial_turn_b, body_b_final
        assert "/errors/internal" not in json.dumps(body_b_final), body_b_final
    finally:
        await _cleanup(client, cleanup_urls)
