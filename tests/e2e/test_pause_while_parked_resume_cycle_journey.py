"""E2E: pause-while-parked + cancel-yielded-tool + resume cycle journey.

Pins the most subtle corner of the yielding-tools state machine —
the interaction between a soft-pause signal and an in-flight park.

Spec §9.2 covers cancel-session vs cancel-yielded-tool precedence
(T0761). This journey pins the OTHER cross-signal precedence: pause
vs resume. With the worker pool's resume wiring landed (roadmap §7),
pause_requested takes precedence over a resumable park —
``_run_one_turn`` checks pause_requested BEFORE the parked_status
branch, so a session that gets paused while parked transitions to
PAUSED on the next worker claim (NOT through the resume hook).

The journey end-to-end:

  1. CREATE session (auto_start=False so no LLM call fires).
  2. asyncpg-inject ask_user park + status=running + a lease row at
     runnable=FALSE — mirrors the production worker handoff state
     (status doesn't change at park_turn — only parked_status does;
     spec §7.2 ``park_turn does NOT touch status``).
  3. POST /pause — session.status was RUNNING so pause_requested
     flips to TRUE (sessions.py:269-277 RUNNING branch). status
     stays RUNNING.
  4. POST cancel-yielded-tool — publishes __yield_cancelled__
     marker onto the bus. Listener flips parked_status to
     'resumable' AND marks the lease runnable.
  5. Worker claims the now-runnable session. ``_run_one_turn``
     checks pause_requested FIRST — it's True, so the session
     transitions to PAUSED via complete_turn(re_enqueue=False).
     The resume hook does NOT fire.
  6. Observable post-pause: status=PAUSED, parked_status STILL
     'resumable' (complete_turn does not clear parked_*), parked_state
     intact (the __yield_cancelled__ marker is preserved for the
     eventual /resume).
  7. POST /resume — flips status to RUNNING, clears pause_requested,
     scheduler.enqueue() arms the lease for runnable=True.
  8. Worker claims the still-resumable session. _run_one_turn:
     cancel_requested=False, pause_requested=False (just cleared),
     parked_status='resumable' → _handle_resume fires NOW.
     ``ask_user_resume`` synthesises a YieldCancelled tool_result;
     inject_resume_messages persists; clear_park drops every
     parked_* column; complete_turn(RUNNING, re_enqueue=True)
     advances turn_no.
  9. Final assertion: parked_status=None, parked_state cleared,
     turn_no advanced past the parked-turn. (The next normal
     worker claim then fails fast on the bogus LLM URL, the
     session ends — but that's outside the scope of this test.)

Subsystems exercised in one test:

  * Session router (pause + resume endpoints + status state machine)
  * Yielding-tools router (cancel-yielded-tool + bus publish)
  * Event bus (Postgres LISTEN/NOTIFY)
  * Scheduler (mark_resumable, claim filter, enqueue)
  * Worker pool (_run_one_turn pause-precedence branch + _handle_resume)
  * yield_resume_registry.get_resume_hook("ask_user") with the
    YieldCancelled payload
  * Storage (clear_park; complete_turn's parked-state retention semantics)

Pinned invariants:

  * park_turn doesn't touch status — a parked session stays at its
    pre-park status (RUNNING in this case).
  * /pause on a RUNNING+parked session sets pause_requested=True
    without changing status.
  * cancel-yielded-tool still publishes + the bus listener still
    marks resumable even with pause_requested=True on the row.
  * Worker pool's pause precedence: on a resumable session with
    pause_requested=True, status flips to PAUSED and the resume
    hook is SKIPPED for this claim.
  * complete_turn does NOT clear parked_* columns — the resume
    event survives the pause-to-PAUSED transition (so /resume can
    still dispatch the hook later).
  * /resume from PAUSED + resumable: pause_requested clears,
    scheduler arms the lease, worker re-claims, NOW _handle_resume
    runs, parked columns clear, turn_no advances.

Covers backlog item T0867. New invariant — no existing test pins
the pause-precedence-over-resume branch end-to-end.
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


async def _seed_ladder(
    client: httpx.AsyncClient, suffix: str, tmp_path,
) -> tuple[str, str, list[str]]:
    """Returns (session_id, workspace_id, cleanup_urls)."""
    pid = f"llm-t867-{suffix}"
    aid = f"ag-t867-{suffix}"
    wp_id = f"wp-t867-{suffix}"
    tpl_id = f"tpl-t867-{suffix}"

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
            "description": "T0867 pause-while-parked probe",
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


async def _inject_running_ask_user_park(
    *,
    session_id: str,
    tool_call_id: str,
    prompt: str,
) -> None:
    """Inject status=running + ask_user park + lease(runnable=FALSE).

    The status=running is the load-bearing difference vs T0862's
    helper: the /pause endpoint distinguishes RUNNING (sets
    pause_requested=True) from CREATED (direct PAUSED flip). T0867
    pins the RUNNING branch.
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


async def _wait_for_status(
    client: httpx.AsyncClient,
    session_id: str,
    expected_status: str,
    *,
    timeout_s: float = 20.0,
    interval_s: float = 0.4,
) -> dict[str, Any]:
    """Poll GET /v1/sessions/{id} until status matches.

    Returns the final session row on success; raises AssertionError
    if the status never matches.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_body: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last_body = r.json()
            if last_body.get("status") == expected_status:
                return last_body
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"session {session_id} did not reach status {expected_status!r} "
        f"within {timeout_s}s; last_body={last_body!r}"
    )


async def _wait_for_park_cleared(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 20.0,
    interval_s: float = 0.4,
) -> dict[str, Any]:
    """Poll until parked_status is None — the resume hook clears it."""
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
        f"session {session_id} parked_status did not clear within "
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
# T0867 — pause-while-parked + cancel-yielded-tool + resume cycle
# ===========================================================================


@pytest.mark.asyncio
async def test_t0867_pause_while_parked_then_cancel_then_resume_cycle(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0867 — Walk a session through pause-while-parked,
    cancel-yielded-tool (worker pickup pauses-not-resumes),
    /resume (worker pickup resumes-via-hook). Pin the full
    interaction primer in one test.
    """
    sid, wid, cleanup_urls = await _seed_ladder(
        client, unique_suffix, tmp_path,
    )
    tool_call_id = f"tc-t867-{unique_suffix}"
    prompt = "Will pause win over resume?"

    try:
        # ----- 1. Initial turn_no for the monotonicity check ------
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        initial_turn_no = r.json()["turn_no"]

        # ----- 2. Inject running + park + lease(runnable=FALSE) ---
        await _inject_running_ask_user_park(
            session_id=sid,
            tool_call_id=tool_call_id,
            prompt=prompt,
        )

        # Sanity: row reads back as parked + running.
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        before_pause = r.json()
        assert before_pause["status"] == "running", before_pause
        assert before_pause["parked_status"] == "parked", before_pause
        assert before_pause.get("pause_requested") in (None, False), before_pause

        # ----- 3. POST /pause — RUNNING branch sets pause_requested
        r = await client.post(
            f"/v1/workspaces/{wid}/sessions/{sid}/pause",
        )
        assert r.status_code == 204, (
            f"expected 204 from /pause on RUNNING session; "
            f"got {r.status_code} {r.text!r}"
        )

        # Status STAYS running — pause_requested is the only field
        # the RUNNING branch updates. parked_status stays parked too.
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        after_pause = r.json()
        assert after_pause["status"] == "running", (
            f"/pause on RUNNING session must not change status — "
            f"spec contract on sessions.py:269-277. after_pause={after_pause!r}"
        )
        assert after_pause["pause_requested"] is True, after_pause
        assert after_pause["parked_status"] == "parked", after_pause

        # ----- 4. POST cancel-yielded-tool — publishes marker -----
        r = await client.post(
            f"/v1/sessions/{sid}/yields/{tool_call_id}/cancel",
            json={"reason": "operator skipped"},
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted"}, r.text

        # ----- 5. Worker claims resumable session → PAUSED --------
        # The bus listener flips parked_status to 'resumable' AND
        # marks the lease runnable. Worker pool's claim loop picks
        # it up; _run_one_turn sees pause_requested=True FIRST and
        # transitions to PAUSED (skipping _handle_resume).
        paused_body = await _wait_for_status(client, sid, "paused")

        # ----- 6. Observable post-pause-claim ---------------------
        # parked_status retained — complete_turn does NOT touch
        # parked_* columns. The resume marker is preserved on the
        # row so a later /resume can replay it through the hook.
        assert paused_body["status"] == "paused", paused_body
        assert paused_body["parked_status"] == "resumable", (
            f"parked_status must remain 'resumable' after "
            f"complete_turn(PAUSED) — clear_park is only called by "
            f"_handle_resume. paused_body={paused_body!r}"
        )
        # turn_no advanced once (the pause completion is a turn).
        assert paused_body["turn_no"] == initial_turn_no + 1, paused_body
        # parked_state survives — the __yield_cancelled__ event has
        # been merged into resume_event_payload by mark_resumable.
        ps = paused_body.get("parked_state") or {}
        assert ps.get("tool_call_id") == tool_call_id, ps
        rep = ps.get("resume_event_payload") or {}
        assert rep.get("__yield_cancelled__") is True, (
            f"resume_event_payload should carry the cancellation "
            f"marker so the eventual resume hook synthesises a "
            f"YieldCancelled tool_result. rep={rep!r}"
        )
        assert rep.get("reason") == "operator skipped", rep
        # No /errors/internal envelope leak.
        assert "/errors/internal" not in json.dumps(paused_body), paused_body

        # ----- 7. POST /resume — re-arms claim, clears pause flag -
        r = await client.post(
            f"/v1/workspaces/{wid}/sessions/{sid}/resume",
        )
        assert r.status_code == 200, r.text
        resumed_body = r.json()
        assert resumed_body["status"] == "running", resumed_body
        assert resumed_body.get("pause_requested") in (None, False), resumed_body

        # ----- 8. Worker claims still-resumable session → hook ----
        # On this claim, pause_requested is False AND parked_status
        # is 'resumable' — _run_one_turn routes to _handle_resume,
        # the cancelled-tool result rehydrates, parked_* clear,
        # turn_no advances again via complete_turn(RUNNING).
        final_body = await _wait_for_park_cleared(client, sid)

        # ----- 9. Final observable assertions --------------------
        assert final_body["parked_status"] is None, final_body
        assert final_body.get("parked_state") in (None, {}), final_body
        assert final_body.get("parked_event_key") in (None, ""), final_body
        # turn_no advanced past the PAUSED-completion turn (>=
        # initial+2: one bump per complete_turn invocation).
        assert final_body["turn_no"] >= initial_turn_no + 2, (
            f"turn_no should have advanced twice (PAUSED + resume); "
            f"initial={initial_turn_no}, final={final_body['turn_no']}"
        )
        assert "/errors/internal" not in json.dumps(final_body), final_body
    finally:
        await _cleanup(client, cleanup_urls)
