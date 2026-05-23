"""E2E tests for ask_user/respond + cancel-yielded-tool mutation effects.

Builds on the park-injection pattern from test_yields_with_injected_park.py
(commit e29e42f) to exercise the M3 mutation surfaces:

* T0780 — POST /v1/sessions/{id}/ask_user/respond on a session parked
  on a non-ask_user tool (sleep) returns 404 (cross-tool envelope
  mirror of T0759 for the GET).
* T0782 — POST /v1/sessions/{id}/ask_user/respond with a response
  body failing the prompt's JSON Schema returns 422 with an
  internal-free envelope.
* T0783 — POST /v1/sessions/{id}/ask_user/respond on a valid parked
  session actually flips parked_status='parked' → 'resumable' and
  stamps resume_event_payload onto parked_state (verified via psql).
* T0784 — POST /v1/sessions/{id}/yields/{tcid}/cancel on a valid
  parked session actually publishes the YieldCancelled marker into
  parked_state.resume_event_payload (verified via psql).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest


# ---------------------------------------------------------------------------
# Postgres + seed helpers (mirrors test_yields_with_injected_park.py)
# ---------------------------------------------------------------------------


async def _pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", port=5432,
        user="matrix", password="matrix", database="matrix_e2e",
    )


async def _seed_llm_provider(client: httpx.AsyncClient, pid: str) -> None:
    r = await client.post(
        "/v1/llm_providers",
        json={
            "id": pid, "provider": "ollama",
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
            "id": agent_id, "description": "respond/cancel probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent failed: {r.text}"


async def _seed_workspace(
    client: httpx.AsyncClient, wp_id: str, tpl_id: str, tmp_path,
) -> str:
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert r.status_code == 201
    r = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id, "description": "tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        },
    )
    assert r.status_code == 201
    r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert r.status_code == 201
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
    assert r.status_code == 201
    return r.json()["id"]


async def _inject_park(
    session_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    event_key: str,
    prompt: str | None = None,
    response_schema: dict | None = None,
) -> None:
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
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    sql = """
        UPDATE sessions
        SET data = jsonb_set(jsonb_set(jsonb_set(jsonb_set(jsonb_set(data,
                     '{parked_status}', to_jsonb('parked'::text)),
                   '{parked_event_key}', to_jsonb($2::text)),
                 '{parked_until}', to_jsonb($3::text)),
               '{parked_at}', to_jsonb($4::text)),
             '{parked_state}', $5::jsonb),
            updated_at = now()
        WHERE id = $1
    """
    conn = await _pg()
    try:
        await conn.execute(
            sql, session_id, event_key,
            parked_until.isoformat(), now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


async def _read_park_fields(session_id: str) -> dict:
    """Return parked_status + parked_state.resume_event_payload via psql."""
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            "SELECT data->>'parked_status' AS parked_status, "
            "data->'parked_state'->'resume_event_payload' AS payload "
            "FROM sessions WHERE id = $1",
            session_id,
        )
        if row is None:
            return {}
        # asyncpg returns the JSONB field as a Python value if it's a
        # primitive type, but as a string if it's an object/array. Be
        # defensive about both.
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "parked_status": row["parked_status"],
            "resume_event_payload": payload,
        }
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
    pid = f"llm-rc-{unique_suffix}"
    aid = f"ag-rc-{unique_suffix}"
    wp_id = f"wp-rc-{unique_suffix}"
    tpl_id = f"tpl-rc-{unique_suffix}"
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
# T0780 — respond on sleep-parked session returns 404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0780_ask_user_respond_404_when_parked_on_sleep(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0780 — POST /v1/sessions/{id}/ask_user/respond on a session
    parked on a NON-ask_user tool (sleep) must return 404; the row
    stays parked. Mirror of T0759 for the GET, defending the respond
    endpoint's tool-name guard.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-sleep-{unique_suffix}"
    try:
        await _inject_park(
            sid, tool_name="sleep",
            tool_call_id=tcid,
            event_key=f"timer:{tcid}",
        )
        r = await client.post(
            f"/v1/sessions/{sid}/ask_user/respond",
            json={"tool_call_id": tcid, "response": "irrelevant"},
        )
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["status"] == 404
        assert body["type"].endswith("/not-found"), body
        # Row stays parked.
        fields = await _read_park_fields(sid)
        assert fields["parked_status"] == "parked", (
            "row flipped despite cross-tool 404"
        )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0782 — respond with schema-violating body returns 422
# ===========================================================================


@pytest.mark.asyncio
async def test_t0782_ask_user_respond_422_when_response_violates_schema(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0782 — When the parked yield supplied a JSON Schema in
    resume_metadata.response_schema, the respond endpoint validates
    the inbound response server-side. A schema-violating body
    returns 422 /errors/validation-error, NOT /errors/internal,
    and leaves the row parked.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-422-{unique_suffix}"
    try:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        await _inject_park(
            sid, tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            prompt="Provide a name",
            response_schema=schema,
        )

        # Schema requires `name` — submit `{wrong: "field"}` → 422.
        r = await client.post(
            f"/v1/sessions/{sid}/ask_user/respond",
            json={"tool_call_id": tcid, "response": {"wrong": "field"}},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["status"] == 422
        assert body["type"].endswith("/validation-error"), body
        # Detail references the schema failure.
        detail = (body.get("detail") or "").lower()
        assert "schema" in detail or "required" in detail or "name" in detail
        # Row stays parked (no accidental flip on a validation failure).
        fields = await _read_park_fields(sid)
        assert fields["parked_status"] == "parked"

        # Sanity: a VALID body would succeed (don't actually run it
        # here — we don't want to leave the row resumable mid-cleanup).
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0783 — respond on valid parked session flips → resumable + stamps payload
# ===========================================================================


@pytest.mark.asyncio
async def test_t0783_ask_user_respond_flips_to_resumable_and_stamps_payload(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0783 — POST /v1/sessions/{id}/ask_user/respond on a valid
    parked session must:

    * return 202 Accepted
    * publish on the event bus → listener mark_resumable() flips
      parked_status to 'resumable'
    * stamp resume_event_payload with {"response": <body.response>}

    End-to-end pin for the M3 happy path. Verified via psql so we
    see the actual row state after the listener round-trip.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-202-{unique_suffix}"
    try:
        await _inject_park(
            sid, tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            prompt="What is your name?",
        )

        r = await client.post(
            f"/v1/sessions/{sid}/ask_user/respond",
            json={"tool_call_id": tcid, "response": "Alice"},
        )
        assert r.status_code == 202, r.text
        # Body confirms acceptance.
        assert r.json().get("status") == "accepted"

        # Listener processes the bus event asynchronously. Poll for
        # parked_status='resumable' for up to 5s.
        for _ in range(50):
            await asyncio.sleep(0.1)
            fields = await _read_park_fields(sid)
            if fields.get("parked_status") == "resumable":
                break
        assert fields.get("parked_status") == "resumable", (
            f"row never flipped to resumable: {fields}"
        )
        # The resume_event_payload carries the operator's response.
        payload = fields.get("resume_event_payload")
        assert payload == {"response": "Alice"}, (
            f"unexpected payload stamped: {payload!r}"
        )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0784 — cancel-yielded-tool publishes the YieldCancelled marker
# ===========================================================================


@pytest.mark.asyncio
async def test_t0784_cancel_yielded_tool_publishes_cancelled_marker(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0784 — POST /v1/sessions/{id}/yields/{tcid}/cancel on a
    valid parked session publishes a YieldCancelled marker payload
    onto the event bus. The listener flips parked_status to
    'resumable' and stamps resume_event_payload with the
    ``__yield_cancelled__`` marker + reason + cancelled_at.

    Pin for the M3 cancel-yielded-tool happy path; mirrors T0783
    for the cancel surface.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-cancel-{unique_suffix}"
    reason = "operator changed their mind"
    try:
        await _inject_park(
            sid, tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            prompt="?",
        )

        r = await client.post(
            f"/v1/sessions/{sid}/yields/{tcid}/cancel",
            json={"reason": reason},
        )
        assert r.status_code == 202, r.text

        # Poll for resumable flip + cancel marker stamped.
        for _ in range(50):
            await asyncio.sleep(0.1)
            fields = await _read_park_fields(sid)
            if fields.get("parked_status") == "resumable":
                break
        assert fields.get("parked_status") == "resumable", (
            f"row never flipped to resumable: {fields}"
        )
        payload = fields.get("resume_event_payload") or {}
        assert payload.get("__yield_cancelled__") is True, (
            f"expected cancel marker, got {payload!r}"
        )
        assert payload.get("reason") == reason, payload
        assert "cancelled_at" in payload, payload
    finally:
        await _cleanup(client, cleanup_urls)
