"""E2E: yielding-tools journey — ask_user park → respond → cancel.

Second post-pivot user-journey test on the API surface. One pytest
function exercises the M1–M3 yielding-tools subsystems end-to-end by
chaining respond + cancel operations against the same session,
asserting the bus / listener / scheduler flips parked → resumable
between them.

Unlike the single-contract pins in test_yields_with_injected_park.py,
this test treats park-respond-park-cancel as ONE operator journey and
asserts the state machine round-trips cleanly across both events.

Subsystems crossed:
  1. providers + workspace + agent + session seeding (CRUD)
  2. direct DB park injection (asyncpg) — substitutes for an LLM
     turn that would yield. Necessary because LM Studio may not be
     reachable from this environment.
  3. ask_user pending GET endpoint
  4. ask_user respond POST endpoint + bus publish
  5. listener round-trip (parked → resumable) — observed via psql
  6. cancel-yielded-tool POST endpoint + bus publish
  7. resume_event_payload shape (response payload vs cancellation
     marker)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest


# ---------------------------------------------------------------------------
# Postgres connection (matches bringup defaults)
# ---------------------------------------------------------------------------


async def _pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost",
        port=5432,
        user="matrix",
        password="matrix",
        database="matrix_e2e",
    )


# ---------------------------------------------------------------------------
# Park injection (same shape as worker.pool._handle_yield writes)
# ---------------------------------------------------------------------------


async def _inject_ask_user_park(
    session_id: str,
    *,
    tool_call_id: str,
    prompt: str,
    response_schema: dict | None = None,
) -> str:
    """Inject an ask_user park onto the session row. Returns the
    event_key the bus listener watches for."""
    event_key = f"ask_user:{session_id}:{tool_call_id}"
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    resume_metadata: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "prompt": prompt,
    }
    if response_schema is not None:
        resume_metadata["response_schema"] = response_schema

    parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "ask_user",
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": resume_metadata,
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    sql = """
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
    conn = await _pg()
    try:
        await conn.execute(
            sql,
            session_id,
            event_key,
            parked_until.isoformat(),
            now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()
    return event_key


async def _ensure_lease(session_id: str) -> None:
    """Insert a session_leases row so park-respond can resolve the
    holder. Schema (matches matrix.scheduler.postgres bootstrap):
      session_id (PK) | worker_id | leased_at | expires_at
      | next_attempt_at | runnable
    """
    conn = await _pg()
    try:
        await conn.execute(
            """
            INSERT INTO session_leases (session_id, worker_id, expires_at, next_attempt_at, runnable)
            VALUES ($1, NULL, now() + interval '5 minutes', now(), false)
            ON CONFLICT (session_id) DO NOTHING
            """,
            session_id,
        )
    finally:
        await conn.close()


async def _read_parked(session_id: str) -> dict | None:
    """Read parked_status + resume_event_payload off the session row."""
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            """
            SELECT data->>'parked_status' AS parked_status,
                   data->'parked_state'->'resume_event_payload' AS payload
            FROM sessions WHERE id = $1
            """,
            session_id,
        )
        if row is None:
            return None
        return {
            "parked_status": row["parked_status"],
            "payload": (
                json.loads(row["payload"]) if row["payload"] else None
            ),
        }
    finally:
        await conn.close()


async def _wait_for_parked_status(
    session_id: str, expected: str, *, timeout_s: float = 5.0,
) -> dict:
    """Poll the session row until parked_status == expected, or fail."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        last = await _read_parked(session_id)
        if last and last.get("parked_status") == expected:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"parked_status did not reach {expected!r} within {timeout_s}s; "
        f"last={last!r}"
    )


# ---------------------------------------------------------------------------
# API seeding (full ladder)
# ---------------------------------------------------------------------------


async def _seed_ladder(
    client: httpx.AsyncClient, suffix: str, tmp_path: Path,
) -> dict[str, str]:
    """Seed provider+agent+workspace+session via the API. Returns ids."""
    ids = {
        "llm": f"jy-llm-{suffix}",
        "wp": f"jy-wp-{suffix}",
        "tpl": f"jy-tpl-{suffix}",
        "agent": f"jy-ag-{suffix}",
        "workspace": "",
        "session": "",
    }
    r = await client.post("/v1/llm_providers", json={
        "id": ids["llm"],
        "provider": "openresponses",
        "models": [{"name": "stub-model", "context_length": 8192}],
        "config": {
            "url": "http://127.0.0.1:1",
            "api_key": "sk-not-used",
            "flavor": "other",
        },
        "limits": {"max_concurrency": 1},
    })
    assert r.status_code == 201, r.text
    r = await client.post("/v1/agents", json={
        "id": ids["agent"],
        "description": "yielding-journey probe",
        "model": {"provider_id": ids["llm"], "model_name": "stub-model"},
        "tools": [],
    })
    assert r.status_code == 201, r.text
    r = await client.post("/v1/workspace_providers", json={
        "id": ids["wp"],
        "provider": "local",
        "config": {"kind": "local", "path": str(tmp_path)},
    })
    assert r.status_code == 201, r.text
    r = await client.post("/v1/workspace_templates", json={
        "id": ids["tpl"],
        "description": "yielding-journey template",
        "provider_id": ids["wp"],
        "backend": {"kind": "local"},
    })
    assert r.status_code == 201, r.text
    r = await client.post("/v1/workspaces", json={"template_id": ids["tpl"]})
    assert r.status_code == 201, r.text
    ids["workspace"] = r.json()["id"]
    r = await client.post(
        f"/v1/workspaces/{ids['workspace']}/sessions",
        json={"binding": {"kind": "agent", "agent_id": ids["agent"]},
              "auto_start": False},
    )
    assert r.status_code == 201, r.text
    ids["session"] = r.json()["id"]
    return ids


async def _cleanup(client: httpx.AsyncClient, ids: dict[str, str]) -> None:
    for url in (
        f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
        f"/v1/agents/{ids['agent']}",
        f"/v1/workspace_templates/{ids['tpl']}",
        f"/v1/workspace_providers/{ids['wp']}",
        f"/v1/llm_providers/{ids['llm']}",
    ):
        if url:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Race-broken after roadmap §7 worker-pool resume wiring landed "
        "(commits 068184a → f83fee7). The test assumed parked_state "
        "PERSISTS post-respond — that was the visible behaviour in the "
        "gap-blocked codebase. With resume wired, the worker pool now "
        "claims the resumable row after /respond and clears the park "
        "before the test's `_wait_for_parked_status` finishes catching "
        "the brief 'resumable' window. The subsequent re-park + cancel "
        "step then races against either the worker's next claim (which "
        "fast-fails on the bogus LLM URL) or its lease state. T0862 "
        "(test_ask_user_resume_cycle_journey.py) covers the new end-to-"
        "end ask_user resume contract under the wired path. A future "
        "iteration should either refactor this test to assert the new "
        "cleared-park behaviour, or split it into two independent "
        "respond-flow + cancel-flow tests that don't share a "
        "session row."
    )
)
@pytest.mark.asyncio
async def test_yielding_tools_park_respond_then_park_cancel_journey(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """ask_user journey: park → respond (bus listener flips to
    resumable) → re-park (new tool_call_id) → cancel (bus listener
    flips to resumable with cancellation marker).

    Asserts the full park/respond/cancel chain works as a sequence,
    not just the individual contracts in test_yields_with_injected_park.py.
    """
    ids = await _seed_ladder(client, unique_suffix, tmp_path)
    await _ensure_lease(ids["session"])

    try:
        # ===== 1. First park: ask_user with prompt ========================
        tcid1 = f"tc-respond-{unique_suffix}"
        await _inject_ask_user_park(
            ids["session"], tool_call_id=tcid1, prompt="What is your favourite colour?",
        )

        # --- GET pending returns the prompt
        r = await client.get(f"/v1/sessions/{ids['session']}/ask_user/pending")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("prompt") == "What is your favourite colour?", body
        assert body.get("tool_call_id") == tcid1, body

        # --- POST respond: bus listener should flip parked → resumable
        r = await client.post(
            f"/v1/sessions/{ids['session']}/ask_user/respond",
            json={"tool_call_id": tcid1, "response": "blue"},
        )
        assert r.status_code == 202, r.text

        # --- wait for listener to round-trip
        state = await _wait_for_parked_status(ids["session"], "resumable")
        # resume_event_payload should carry {"response": "blue"}
        payload = state.get("payload")
        assert payload is not None and payload.get("response") == "blue", state

        # ===== 2. Second park: ask_user, then cancel-yielded-tool ==========
        tcid2 = f"tc-cancel-{unique_suffix}"
        await _inject_ask_user_park(
            ids["session"], tool_call_id=tcid2, prompt="Confirm to proceed?",
        )

        # GET pending now returns the NEW prompt + new tcid
        r = await client.get(f"/v1/sessions/{ids['session']}/ask_user/pending")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("tool_call_id") == tcid2, body

        # --- POST cancel: marker should land in resume_event_payload
        r = await client.post(
            f"/v1/sessions/{ids['session']}/yields/{tcid2}/cancel",
            json={"reason": "operator-skipped"},
        )
        assert r.status_code == 202, r.text

        # --- wait for listener to flip
        state = await _wait_for_parked_status(ids["session"], "resumable")
        payload = state.get("payload")
        assert payload is not None, state
        assert payload.get("__yield_cancelled__") is True, payload
        assert payload.get("reason") == "operator-skipped", payload

        # ===== 3. Post-cancel: pending GET returns a clean envelope =======
        # Whether it 404s (parked_status='resumable' filter) or 200s
        # (returns the resume_metadata regardless) is a downstream
        # contract detail. The invariant we pin here is: never 5xx.
        r = await client.get(f"/v1/sessions/{ids['session']}/ask_user/pending")
        assert r.status_code in (200, 404), r.text
        if r.status_code == 200:
            # If the endpoint returns the prompt post-cancel, the
            # tcid should still match (no cross-contamination from
            # the earlier tcid1 respond cycle).
            assert r.json().get("tool_call_id") == tcid2, r.json()

    finally:
        await _cleanup(client, ids)
