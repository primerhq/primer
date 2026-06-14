"""E2E tests: yielding-tools endpoints with directly-injected park state.

The primer process exposes ask_user / cancel-yielded-tool / pending
endpoints that require a session to be in the parked state. Without
LM Studio wired in, no agent loop can drive a real park, so we use
direct postgres JSONB injection as fixture setup — identical to the
shape the worker pool would write via park_turn(), just inserted
out-of-band.

This is the same pattern many E2E suites use for state that's
expensive to drive through the production code path. Once LM Studio
is online (or a debug park-injection endpoint lands), these tests
become stronger by exercising the actual park path too.

Covers backlog items:
* T0759 — GET /v1/sessions/{id}/ask_user/pending returns 404 when
  the session is parked on a different tool (sleep), so the
  endpoint doesn't leak ask_user-specific shape across tools.
* T0760 — POST /v1/sessions/{id}/ask_user/respond with a
  tool_call_id that doesn't match the parked yield returns 404;
  the row stays parked (parked_status unchanged); no /errors/internal.
* T0761 — POST /v1/sessions/{id}/yields/{tcid}/cancel rejects with
  409 when cancel_requested is already true on the row
  (cancel-session wins over cancel-yielded-tool per §9.2).
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
# Seed helpers — provider + agent + workspace + session via the API,
# then inject parked_* fields directly via JSONB UPDATE.
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
    assert r.status_code == 201, f"seed LLM failed: {r.text}"


async def _seed_agent(
    client: httpx.AsyncClient, agent_id: str, provider_id: str,
) -> None:
    r = await client.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "park-injection probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent failed: {r.text}"


async def _seed_workspace(
    client: httpx.AsyncClient, wp_id: str, tpl_id: str, tmp_path,
) -> str:
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        },
    )
    assert r.status_code == 201, f"seed wp provider failed: {r.text}"
    r = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "tpl",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert r.status_code == 201, f"seed tpl failed: {r.text}"
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201, f"seed ws failed: {r.text}"
    return r.json()["id"]


async def _seed_session(
    client: httpx.AsyncClient, workspace_id: str, agent_id: str,
) -> str:
    r = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": agent_id},
            "auto_start": False,
        },
    )
    assert r.status_code == 201, f"seed session failed: {r.text}"
    return r.json()["id"]


async def _wait_for_worker_idle(conn: asyncpg.Connection, session_id: str) -> None:
    """Poll until the worker has finished processing the session's initial turn.

    A freshly created session (auto_start=False) is still enqueued in the
    claim engine and the live worker claims + runs it immediately (completes
    in one turn because there are no instructions). If we inject the park
    before the worker's on_release UPDATE lands, the UPDATE overwrites our
    injection (it replaces the entire data JSONB). We poll until turn_no >= 1
    (success path) which confirms on_release has already written its final row.
    Timeout after 5 s -- sufficient for any in-process worker round-trip.
    """
    import asyncio as _asyncio
    deadline = _asyncio.get_event_loop().time() + 5.0
    while _asyncio.get_event_loop().time() < deadline:
        row = await conn.fetchrow(
            "SELECT COALESCE((data->>'turn_no')::int, 0) AS tn "
            "FROM sessions WHERE id = $1",
            session_id,
        )
        if row is None or row["tn"] >= 1:
            return
        await _asyncio.sleep(0.05)


async def _inject_park(
    session_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    event_key: str,
    prompt: str | None = None,
    response_schema: dict | None = None,
) -> None:
    """Inject parked_* fields onto a session row, mirroring the shape
    the worker pool writes via the park branch of on_release.

    Waits for the worker's initial-turn on_release to complete before
    writing, to avoid a race where the worker's full-row UPDATE overwrites
    our injected JSONB fields. All six park columns are written:
      parked_status='parked', parked_event_key, parked_event_keys (null),
      parked_until, parked_at, parked_state (the JSONB blob).

    The parked_state blob matches ParkedState.to_jsonable() exactly:
      schema_version, tool_call_id (top-level), yielded (with tool_name,
      event_key, timeout, resume_metadata, event_keys), llm_messages,
      turn_no, started_at, resume_event_payload, graph_checkpoint.
    """
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    resume_metadata: dict[str, Any] = {"tool_call_id": tool_call_id}
    if prompt is not None:
        resume_metadata["prompt"] = prompt
    if response_schema is not None:
        resume_metadata["response_schema"] = response_schema
    if tool_name == "sleep":
        resume_metadata["requested_seconds"] = 30.0

    parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": tool_name,
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": resume_metadata,
            "event_keys": None,
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
        "graph_checkpoint": None,
    }

    sql = """
        UPDATE sessions
        SET data = jsonb_set(
                     jsonb_set(
                       jsonb_set(
                         jsonb_set(
                           jsonb_set(
                             jsonb_set(data,
                               '{parked_status}', to_jsonb('parked'::text)),
                             '{parked_event_key}', to_jsonb($2::text)),
                           '{parked_event_keys}', 'null'::jsonb),
                         '{parked_until}', to_jsonb($3::text)),
                       '{parked_at}', to_jsonb($4::text)),
                     '{parked_state}', $5::jsonb
                   ),
            updated_at = now()
        WHERE id = $1
    """
    conn = await _pg()
    try:
        await _wait_for_worker_idle(conn, session_id)
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


async def _set_cancel_requested(session_id: str) -> None:
    """Flip cancel_requested=true on a session row."""
    conn = await _pg()
    try:
        await conn.execute(
            "UPDATE sessions SET data = jsonb_set(data, '{cancel_requested}', "
            "to_jsonb(true)) WHERE id = $1",
            session_id,
        )
    finally:
        await conn.close()


async def _read_parked_status(session_id: str) -> str | None:
    """Read parked_status off the session row (post-mutation check)."""
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            "SELECT data->>'parked_status' AS parked_status "
            "FROM sessions WHERE id = $1",
            session_id,
        )
        return row["parked_status"] if row else None
    finally:
        await conn.close()


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


async def _seed_ladder(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> tuple[str, list[str]]:
    pid = f"llm-inj-{unique_suffix}"
    aid = f"ag-inj-{unique_suffix}"
    wp_id = f"wp-inj-{unique_suffix}"
    tpl_id = f"tpl-inj-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    sid = await _seed_session(client, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    return sid, cleanup_urls


# ===========================================================================
# T0759 — ask_user/pending returns 404 when parked on a different tool
# ===========================================================================


@pytest.mark.asyncio
async def test_t0759_ask_user_pending_404_when_parked_on_sleep(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0759 — A session parked on the sleep tool (not ask_user) must
    cause GET /ask_user/pending to return 404, NOT 200 with sleep's
    resume_metadata leaked. Pins cross-tool isolation in the
    pending endpoint at primer/api/routers/yields.py.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-sleep-{unique_suffix}"
    try:
        await _inject_park(
            sid,
            tool_name="sleep",
            tool_call_id=tcid,
            event_key=f"timer:{tcid}",
        )
        # Sanity: row IS parked.
        assert await _read_parked_status(sid) == "parked"

        # GET /ask_user/pending → 404 (parked on a different tool).
        r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["status"] == 404
        assert body["type"].endswith("/not-found"), body
        # Critical: no sleep-tool leak in the envelope.
        body_str = json.dumps(body)
        assert "requested_seconds" not in body_str, (
            f"sleep's resume_metadata leaked through pending endpoint: {body}"
        )
        assert "resume_metadata" not in body_str, body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0760 — ask_user/respond with tool_call_id mismatch returns 404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0760_ask_user_respond_404_when_tool_call_id_mismatch(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0760 — POSTing to /ask_user/respond with a tool_call_id that
    doesn't match the parked yield's tool_call_id must 404 and leave
    the row parked. Defends primer/api/routers/yields.py's
    _tool_call_id_for() lookup against silently flipping the wrong
    yield to resumable.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    real_tcid = f"tc-real-{unique_suffix}"
    wrong_tcid = f"tc-wrong-{unique_suffix}"
    try:
        await _inject_park(
            sid,
            tool_name="ask_user",
            tool_call_id=real_tcid,
            event_key=f"ask_user:{sid}:{real_tcid}",
            prompt="What is your name?",
        )

        # POST with the WRONG tcid → 404.
        r = await client.post(
            f"/v1/sessions/{sid}/ask_user/respond",
            json={"tool_call_id": wrong_tcid, "response": "Alice"},
        )
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["status"] == 404
        assert body["type"].endswith("/not-found"), body

        # Row stays parked (no accidental flip).
        assert await _read_parked_status(sid) == "parked", (
            "row flipped despite tool_call_id mismatch"
        )

        # And the GET endpoint still returns the original prompt.
        r2 = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
        assert r2.status_code == 200, r2.text
        assert r2.json()["tool_call_id"] == real_tcid
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0761 — yields/cancel rejects with 409 when cancel_requested=true
# ===========================================================================


@pytest.mark.asyncio
async def test_t0761_yields_cancel_returns_409_when_cancel_requested_true(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0761 — Per spec §9.2, cancel-session always wins over
    cancel-yielded-tool. If a session has cancel_requested=true
    already, POSTing to /yields/{tcid}/cancel must 409, not 202.
    Defends the conflict-resolution rule in
    primer/api/routers/yields.py:post_cancel_yielded_tool.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-409-{unique_suffix}"
    try:
        await _inject_park(
            sid,
            tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            prompt="?",
        )
        # Flip cancel_requested=true on the row.
        await _set_cancel_requested(sid)

        # POST cancel-yielded-tool → 409 conflict.
        r = await client.post(
            f"/v1/sessions/{sid}/yields/{tcid}/cancel",
            json={"reason": "should be rejected"},
        )
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["status"] == 409
        assert body["type"].endswith("/conflict"), body
        # Detail mentions cancel-session precedence.
        detail = (body.get("detail") or "").lower()
        assert "terminating" in detail or "cancel_requested" in detail, (
            f"expected detail to reference cancel-session: {body!r}"
        )

        # Row stays parked + cancel_requested stays true.
        assert await _read_parked_status(sid) == "parked"
    finally:
        await _cleanup(client, cleanup_urls)
