"""E2E: full ask_user park → respond → resume cycle.

Sibling to T0861 (which covered the same end-to-end cycle for the
`_approval` tool — the special-cased inline path in
`WorkerPool._handle_resume`). This test exercises the OTHER branch:
the generic registry-driven `get_resume_hook(tool_name)` path that
serves `sleep`, `ask_user`, `watch_files`, and `mcp_task`.

The two branches share the same scheduler / event-bus / worker-pool
plumbing but route through different code in `_handle_resume`:

  * `_approval` → inline `_resume_tool_approval(tool_manager, ...)`
    re-dispatches the original call with `bypass_approval=True`.
  * everything else → `get_resume_hook(tool_name)(metadata, payload)`
    synthesises the tool_result directly from metadata + the event
    payload (no re-dispatch).

T0862 picks `ask_user` as the canonical representative because it's
HTTP-driveable (`POST /v1/sessions/{sid}/ask_user/respond` publishes
the resume event onto the bus) and LM-Studio-free.

The cycle:
  1. Seed LLMProvider + Agent + workspace ladder + Session
     (auto_start=False — no LLM call ever fires).
  2. asyncpg-inject an ask_user park onto the session row PLUS a
     `session_leases` row at runnable=FALSE (mirrors what
     scheduler.park_turn writes in production — without the lease
     row, mark_resumable's lease UPDATE is a no-op and the worker
     pool never claims the row).
  3. GET /v1/sessions/{sid}/ask_user/pending → 200 sanity check.
  4. POST /v1/sessions/{sid}/ask_user/respond {tool_call_id,
     response} → 202. The router publishes onto the bus.
  5. Poll GET /v1/sessions/{sid} until parked_status=None.
  6. Assert parked columns cleared AND turn_no advanced.

Multi-subsystem in one test:
  * ask_user respond router (publishes)
  * event_bus (Postgres LISTEN/NOTIFY)
  * scheduler (mark_resumable + claim)
  * worker pool (_handle_resume → generic registry path, NOT the
    _approval inline branch)
  * yield_resume_registry.get_resume_hook("ask_user")
  * primer.toolset.misc.ask_user_resume (the synthesiser)
  * storage (clear_park + complete_turn)

Covers backlog item T0862. Together with T0861 the two flagship
tests close the full §7 worker-pool resume wiring contract:
T0861 pins the inline _approval branch; T0862 pins the generic
registry-hook branch.
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
        database="primer_e2e",
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_ladder(
    client: httpx.AsyncClient, suffix: str, tmp_path,
) -> tuple[str, str, list[str]]:
    """Returns (session_id, workspace_id, cleanup_urls)."""
    pid = f"llm-t862-{suffix}"
    aid = f"ag-t862-{suffix}"
    wp_id = f"wp-t862-{suffix}"
    tpl_id = f"tpl-t862-{suffix}"

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
            "description": "T0862 ask_user resume probe",
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
    *,
    session_id: str,
    tool_call_id: str,
    prompt: str,
) -> None:
    """Inject an ask_user park + lease row (runnable=FALSE) in one txn.

    Same shape as scheduler.park_turn would leave behind, but
    asyncpg-direct so the test doesn't need a real LLM to drive
    the park.
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
    # Same shape T0861 uses: insert a session_leases row at
    # runnable=FALSE so mark_resumable's lease UPDATE has a target.
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
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 20.0,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    """Poll GET /v1/sessions/{id} until parked_status is None.

    Returns the final session JSON on success; raises AssertionError
    if the worker doesn't consume the resumable row in time.
    """
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
# T0862 — full ask_user park → respond → resume cycle (generic-hook branch)
# ===========================================================================


@pytest.mark.asyncio
async def test_t0862_ask_user_park_respond_resume_clears_park_and_advances_turn(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0862 — End-to-end ask_user resume cycle: park, respond,
    worker pool resumes via the GENERIC registry hook
    (primer.toolset.misc.ask_user_resume), parked columns clear,
    turn_no advances.

    Pinned invariants:
      * `/ask_user/respond` publishes onto the event bus.
      * Postgres LISTEN/NOTIFY drives mark_resumable + re-arms
        the lease.
      * Worker pool's claim loop picks up the resumable row.
      * `_run_one_turn` routes to `_handle_resume`.
      * `_handle_resume` looks up `ask_user_resume` via
        `yield_resume_registry.get_resume_hook("ask_user")` (NOT
        the `_approval` inline branch T0861 covers).
      * Hook returns a ToolCallResult; the worker wraps it as
        ToolResultPart and persists [assistant_with_tool_use,
        tool_result_msg] via inject_resume_messages.
      * clear_park + complete_turn(RUNNING, re_enqueue=True).
      * Observable: parked_status=None, parked_state cleared,
        turn_no > parked_turn.

    Like T0861, the next normal claim after resume will try to
    drive an LLM turn against the bogus URL and fail-fast — we
    don't assert on the terminal state, only on the resume cycle
    completing.
    """
    sid, _wid, cleanup_urls = await _seed_ladder(
        client, unique_suffix, tmp_path,
    )
    tool_call_id = f"tc-t862-{unique_suffix}"
    prompt = "What is the airspeed velocity of an unladen swallow?"

    try:
        # Initial turn_no.
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        initial_turn_no = r.json()["turn_no"]

        # ----- 1. Inject ask_user park + invisible lease ---------
        await _inject_ask_user_park_with_lease(
            session_id=sid,
            tool_call_id=tool_call_id,
            prompt=prompt,
        )

        # ----- 2. Sanity: /ask_user/pending sees the park --------
        r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
        assert r.status_code == 200, r.text
        pending = r.json()
        assert pending["tool_call_id"] == tool_call_id, pending
        assert prompt in pending["prompt"], pending

        # ----- 3. POST /respond — operator answers --------------
        operator_answer = "African or European?"
        r = await client.post(
            f"/v1/sessions/{sid}/ask_user/respond",
            json={
                "tool_call_id": tool_call_id,
                "response": operator_answer,
            },
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted"}, r.text

        # ----- 4. Poll until resume cycle clears parked_status --
        body = await _wait_for_resume(client, sid)

        # parked_state fully cleared (clear_park drops every column).
        assert body["parked_status"] is None, body
        assert body.get("parked_state") in (None, {}), body
        assert body.get("parked_event_key") in (None, ""), body

        # turn_no advanced via complete_turn(RUNNING, re_enqueue=True).
        final_turn_no = body["turn_no"]
        assert final_turn_no > initial_turn_no, (
            f"turn_no didn't advance through resume: "
            f"initial={initial_turn_no}, final={final_turn_no}"
        )

        # No /errors/internal envelope leak.
        assert "/errors/internal" not in json.dumps(body), body
    finally:
        await _cleanup(client, cleanup_urls)
