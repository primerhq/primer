"""E2E: timeout-as-rejection for an _approval park (§2 contract).

Closes the §2 feature directive's "Pin timeout-as-rejection" item.
T0858 covers the multi-strategy CRUD; T0861 covers the operator-
driven approve cycle end-to-end; T0859 covers chats respond; this
test covers the **timeout** path through the same resume wiring:

  * The primer.bus.scheduler_tasks.TimeoutSweeper periodically
    scans for parked rows whose `parked_until <= now()` and
    publishes the `__yield_timeout__` marker onto the bus.
  * The bus listener picks it up, calls scheduler.mark_resumable
    with the marker payload.
  * The worker pool claims the resumable row, _handle_resume
    runs _resume_tool_approval which classifies the payload as a
    YieldTimeout instance and synthesises a
    ToolResultPart(error=True, reason="timed-out") — the same
    branch unit-tested in test_approval_resume.py
    :test_resume_timeout_synthesises_rejection.

To keep the test fast (the sweeper polls every 30s by default —
that's a real e2e wait), this test simulates the sweeper's
publish DIRECTLY via asyncpg `pg_notify`. The sweeper itself is
unit-tested in tests/bus/test_listener_and_tasks.py
:TestTimeoutSweeper; here we focus on the worker-side timeout
handling end-to-end, which is the new contract §7 unlocked.

The cycle:
  1. Seed full agent ladder + Session (auto_start=False).
  2. asyncpg-inject _approval park + session_leases at
     runnable=FALSE (mirrors what scheduler.park_turn writes).
  3. asyncpg `pg_notify('matrix_yield_events', '<json>')` with
     the timeout-marker payload — simulates what
     TimeoutSweeper does.
  4. Bus listener flips parked → resumable + arms the lease.
  5. Worker pool claims, _handle_resume runs the _approval
     branch with the YieldTimeout-classified payload.
  6. _resume_tool_approval synthesises a rejected
     ToolResultPart with reason="timed-out", persists, and
     clear_park + complete_turn(RUNNING, re_enqueue=True).
  7. Test polls /v1/sessions/{sid} until parked_status=None.
  8. Asserts parked cleared + turn_no advanced.

Multi-subsystem in one test:
  * matrix_yield_events bus channel (Postgres NOTIFY)
  * primer.bus.listener.YieldEventListener (in-app subscriber)
  * scheduler.mark_resumable + claim
  * worker pool _handle_resume → _resume_tool_approval timeout
    branch (yield_runtime.py:348 → decision="rejected",
    reason="timed-out")
  * storage (clear_park + complete_turn)

Covers backlog item T0863. First end-to-end timeout-as-rejection
test against the live wired §7 resume path.
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
    pid = f"llm-t863-{suffix}"
    aid = f"ag-t863-{suffix}"
    wp_id = f"wp-t863-{suffix}"
    tpl_id = f"tpl-t863-{suffix}"

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
            "description": "T0863 approval-timeout probe",
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


async def _inject_approval_park_with_lease(
    *,
    session_id: str,
    tool_call_id: str,
    inner_tool_name: str,
    arguments: dict,
    parked_until_offset_s: float = -1.0,
) -> str:
    """Inject _approval park + invisible lease. Returns the event_key.

    `parked_until_offset_s` defaults to -1.0 (parked_until 1s in the
    past) so the TimeoutSweeper's `parked_until <= now()` predicate
    would match this row immediately — useful if a future test
    swaps the manual NOTIFY for the real sweeper wait.
    """
    now = datetime.now(timezone.utc)
    parked_at = now - timedelta(seconds=5)
    parked_until = now + timedelta(seconds=parked_until_offset_s)
    event_key = f"tool_approval:{session_id}:{tool_call_id}"
    parked_state: dict[str, Any] = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": event_key,
            "timeout": 5.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "original_call": {
                    "id": tool_call_id,
                    "name": inner_tool_name,
                    "arguments": arguments,
                },
                "policy_id": "p-t863",
                "approval_type": "required",
                "gate_reason": "operator gate (T0863)",
            },
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": parked_at.isoformat(),
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
                parked_at.isoformat(),
                json.dumps(parked_state),
            )
    finally:
        await conn.close()
    return event_key


async def _publish_timeout_marker(event_key: str) -> None:
    """Fire pg_notify('matrix_yield_events', ...) with the
    timeout-marker payload directly from the test process.

    Simulates exactly what primer.bus.scheduler_tasks.TimeoutSweeper
    does (`make_timeout_payload()` returns
    `{"__yield_timeout__": True}`, the bus publish wraps it as
    `{"event_key": ..., "payload": ...}` and runs pg_notify).
    Skipping the sweeper's 30s poll window keeps this test fast
    (~1s vs ~35s). The sweeper's own publish behaviour is
    unit-tested in tests/bus/test_listener_and_tasks.py.
    """
    body = json.dumps({
        "event_key": event_key,
        "payload": {"__yield_timeout__": True},
    })
    conn = await _pg()
    try:
        await conn.execute(
            "SELECT pg_notify('matrix_yield_events', $1)",
            body,
        )
    finally:
        await conn.close()


async def _wait_for_resume(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 20.0,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    """Poll GET /v1/sessions/{id} until parked_status is None."""
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
# T0863 — _approval park times out → bus listener → worker → rejection
# ===========================================================================


@pytest.mark.asyncio
async def test_t0863_approval_timeout_publish_resume_synthesises_rejection(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0863 — End-to-end timeout-as-rejection cycle for an
    _approval park. Simulates the TimeoutSweeper by firing the
    `__yield_timeout__` marker payload directly via pg_notify;
    asserts the worker pool's resume branch synthesises a
    rejection and clears the park.

    Pinned invariants:
      * The bus listener routes a `__yield_timeout__` payload to
        scheduler.mark_resumable using the parked event_key.
      * mark_resumable + lease re-arm + worker claim works the
        same as for the operator-responded path (T0861).
      * _handle_resume detects tool_name="_approval", calls
        _resume_tool_approval with the classified payload — which
        classify_resume_payload turns into a YieldTimeout
        instance — and synthesises ToolResultPart(error=True)
        with reason="timed-out" (yield_runtime.py:348).
      * The worker calls clear_park + complete_turn(RUNNING,
        re_enqueue=True) on the synthetic-rejection path the
        same way it does on the approve/operator-reject paths.
      * Observable: parked_status=None, turn_no advanced.

    The synthesised tool_result's reason="timed-out" content is
    unit-tested at the function level
    (tests/worker/test_approval_resume.py
    ::test_resume_timeout_synthesises_rejection). This test
    pins the END-TO-END cycle: bus → scheduler → worker → resume
    → clear_park.
    """
    sid, _wid, cleanup_urls = await _seed_ladder(
        client, unique_suffix, tmp_path,
    )
    tool_call_id = f"tc-t863-{unique_suffix}"

    try:
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        initial_turn_no = r.json()["turn_no"]

        # ----- 1. Inject _approval park (parked_until in the past) -
        event_key = await _inject_approval_park_with_lease(
            session_id=sid,
            tool_call_id=tool_call_id,
            inner_tool_name="_workspaces__exec",
            arguments={"command": ["true"]},
            parked_until_offset_s=-1.0,
        )

        # Sanity: pending endpoint sees the park.
        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        assert r.json()["tool_call_id"] == tool_call_id, r.text

        # ----- 2. Simulate the TimeoutSweeper's publish ------------
        # pg_notify('matrix_yield_events', '{...}') is what the
        # sweeper does once parked_until <= now(). The bus listener
        # in-app picks it up.
        await _publish_timeout_marker(event_key)

        # ----- 3. Poll until resume cycle completes ----------------
        body = await _wait_for_resume(client, sid)

        # parked_state fully cleared (clear_park dropped every column).
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
