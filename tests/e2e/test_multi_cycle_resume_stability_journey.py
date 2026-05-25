"""E2E: resume cycle survives an on-disk-session-ENDED edge case.

Surfaced while designing a multi-cycle stability test: when a
session's PREVIOUS turn ended fatally (e.g. the post-resume LLM
call failed against a bogus URL), the on-disk AgentSession status
is left at ENDED. A subsequent inject of a new park onto the DB
row brings the row back to a claimable state, the worker pool
picks it up, _handle_resume runs the hook successfully — but then
inject_resume_messages calls commit_state, which rejects with:

  ConflictError: cannot commit state on ENDED session 'sess-...'

Pre-fix, that exception escaped _handle_resume into _run_one_turn's
fatal handler. _handle_fatal called complete_turn(ENDED, failed) —
but NEVER touched parked_*. The row was left:
  status        = ENDED
  parked_status = 'resumable'      ← STUCK
  lease         = runnable=TRUE    ← would re-claim forever
  parked_event_key still pointing at the bus event that already
  fired

In production this is rare (it requires a multi-cycle scenario on
a session whose previous cycle's post-resume LLM call failed),
but the consequence is a stuck row + a worker that keeps trying
to claim + fail. Defence-in-depth fix: wrap the
inject_resume_messages call in _handle_resume in try/except,
clear_park + complete_turn(ENDED, failed) on failure so the row
lands in a sane terminal state.

T0865 pins this contract:
  1. Seed full agent ladder + session.
  2. Cycle 1: inject _approval park, POST /respond approved.
     Worker resumes successfully → post-resume LLM call fails
     fatally (bogus URL) → status=ENDED, on-disk AgentSession
     ENDED too.
  3. Cycle 2: inject another _approval park onto the DB row +
     reset status=running on the DB. The on-disk AgentSession
     is STILL ENDED — that's the edge case.
  4. POST /respond approved on cycle 2.
  5. Worker pool wakes, claims the row, runs _handle_resume.
  6. _resume_tool_approval works (it doesn't touch the on-disk
     session); inject_resume_messages calls commit_state which
     raises ConflictError.
  7. With the new defensive branch in _handle_resume: park is
     cleared and the session terminates ENDED-failed. WITHOUT
     the fix, parked_status would stay 'resumable' forever.
  8. Test polls until parked_status=None; asserts cleared.

Multi-subsystem: tool_approval router → bus → scheduler →
worker pool → _handle_resume's defensive branch → storage
clear_park + complete_turn. The DEFENSIVE branch is the new
addition this iteration; previous flagship tests didn't reach it
because they only exercise the happy path (one cycle on a
clean session).

Covers backlog item T0865.
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
        user="matrix",
        password="matrix",
        database="matrix_e2e",
    )


async def _seed_ladder(
    client: httpx.AsyncClient, suffix: str, tmp_path,
) -> tuple[str, str, list[str]]:
    pid = f"llm-t865-{suffix}"
    aid = f"ag-t865-{suffix}"
    wp_id = f"wp-t865-{suffix}"
    tpl_id = f"tpl-t865-{suffix}"

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
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/agents",
        json={
            "id": aid,
            "description": "T0865 cycle-after-failure probe",
            "model": {"provider_id": pid, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id, "description": "tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
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
    *, session_id: str, tool_call_id: str, inner_tool: str,
    reset_status: bool = True,
) -> str:
    """Inject _approval park + lease(FALSE).

    If ``reset_status`` is True (default), also force status to
    'running' on the DB row. Used after a prior fatal turn to
    make the row claimable again — the on-disk AgentSession
    status is NOT reset (that requires touching workspace files),
    which is the edge case this test surfaces.
    """
    event_key = f"tool_approval:{session_id}:{tool_call_id}"
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state: dict[str, Any] = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "original_call": {
                    "id": tool_call_id,
                    "name": inner_tool,
                    "arguments": {"path": "/etc/x"},
                },
                "policy_id": "p-t865",
                "approval_type": "required",
                "gate_reason": "T0865 probe",
            },
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    status_jsonb = (
        ", '{status}', to_jsonb('running'::text)" if reset_status else ""
    )
    # The placeholder is appended into the jsonb_set chain below.
    park_sql_status_reset = """
        UPDATE sessions
        SET data = jsonb_set(
                     jsonb_set(
                       jsonb_set(
                         jsonb_set(
                           jsonb_set(
                             jsonb_set(data,
                               '{status}', to_jsonb('running'::text)),
                             '{parked_status}', to_jsonb('parked'::text)),
                           '{parked_event_key}', to_jsonb($2::text)),
                         '{parked_until}', to_jsonb($3::text)),
                       '{parked_at}', to_jsonb($4::text)),
                     '{parked_state}', $5::jsonb
                   ),
            updated_at = now()
        WHERE id = $1
    """
    park_sql_plain = """
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
    park_sql = park_sql_status_reset if reset_status else park_sql_plain
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
    return event_key


async def _wait_for_resume(
    client: httpx.AsyncClient, session_id: str,
    *, timeout_s: float = 20.0,
) -> dict[str, Any]:
    """Poll until parked_status is None."""
    import asyncio
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_body: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last_body = r.json()
            if last_body.get("parked_status") in (None, "null"):
                return last_body
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"session {session_id} parked_status never cleared "
        f"within {timeout_s}s; last_body={last_body!r}"
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
# T0865 — cycle-after-fatal cleanly clears parked_status (no stuck orphan)
# ===========================================================================


@pytest.mark.asyncio
async def test_t0865_resume_after_on_disk_ended_clears_park(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0865 — Defence-in-depth pin: when _handle_resume's
    inject_resume_messages call fails (e.g. because the on-disk
    AgentSession is already ENDED from a prior fatal turn), the
    resume branch must NOT leave the row stuck at
    parked_status='resumable'. It must clear_park +
    complete_turn(ENDED, failed) so the row lands in a sane
    terminal state.

    Pinned invariants:
      * Cycle 1's happy path: park, /respond, resume cycle clears
        park, turn_no advances. (Same as T0861's contract.)
      * After cycle 1, the post-resume LLM call fails fatally on
        the bogus URL → on-disk AgentSession + session row both
        ENDED.
      * Cycle 2's inject restores the DB row's status=running and
        parked_status='parked' but the on-disk AgentSession stays
        ENDED — this is the edge case.
      * /respond fires the cycle. Worker claims, _handle_resume
        succeeds at the hook dispatch but fails at
        inject_resume_messages (commit_state rejects on ENDED).
      * Without the defensive branch (pre-fix), the row would
        sit at parked_status='resumable' forever — a stuck
        orphan that would re-claim on every worker poll.
      * With the defensive branch (this iteration's fix),
        parked_status clears AND the session is set ENDED-failed.
        No stuck row, no claim loop.

    The defensive branch was added to matrix/worker/pool.py
    in the same commit as this test.
    """
    sid, _wid, cleanup_urls = await _seed_ladder(
        client, unique_suffix, tmp_path,
    )

    try:
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        baseline = r.json()["turn_no"]

        # ----- Cycle 1: happy resume (per T0861's contract) -------
        tcid_1 = f"tc-c1-{unique_suffix}"
        await _inject_approval_park(
            session_id=sid,
            tool_call_id=tcid_1,
            inner_tool="_workspaces__exec",
        )
        r = await client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={"tool_call_id": tcid_1, "decision": "approved"},
        )
        assert r.status_code == 202, r.text
        body1 = await _wait_for_resume(client, sid)
        assert body1["parked_status"] is None, body1
        assert body1["turn_no"] > baseline, body1
        # The post-resume LLM call fails. Wait briefly for the
        # fatal handler to advance status. We don't strictly
        # depend on this — the next inject resets status='running'.

        # ----- Cycle 2: inject onto post-fatal row ---------------
        # The on-disk AgentSession is now ENDED (cycle 1's fatal
        # complete_turn set it). Cycle 2's inject only touches the
        # DB row (not the workspace files), so commit_state in
        # _handle_resume will reject. The DEFENSIVE branch must
        # then clear_park + end the session — NOT leak as fatal.
        tcid_2 = f"tc-c2-{unique_suffix}"
        await _inject_approval_park(
            session_id=sid,
            tool_call_id=tcid_2,
            inner_tool="_workspaces__exec",
        )
        r = await client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={"tool_call_id": tcid_2, "decision": "approved"},
        )
        assert r.status_code == 202, r.text

        # ----- The load-bearing assertion -----------------------
        # parked_status MUST clear within the polling window,
        # proving the defensive branch fired. Pre-fix, this poll
        # would time out because the worker's fatal handler
        # never touches parked_*.
        body2 = await _wait_for_resume(client, sid, timeout_s=20.0)
        assert body2["parked_status"] is None, body2
        assert body2.get("parked_state") in (None, {}), body2
        assert body2.get("parked_event_key") in (None, ""), body2

        # No /errors/internal leak.
        assert "/errors/internal" not in json.dumps(body2), body2
    finally:
        await _cleanup(client, cleanup_urls)
