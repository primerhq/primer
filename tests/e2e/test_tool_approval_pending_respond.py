"""E2E tests for the §2 tool-approval pending/respond endpoints.

The §2 yielding-tools surface for operator-approval gates parks the
session/chat row on a special ``_approval`` tool. The HTTP endpoints
at::

    GET  /v1/sessions/{id}/tool_approval/pending
    POST /v1/sessions/{id}/tool_approval/respond
    GET  /v1/chats/{id}/tool_approval/pending
    POST /v1/chats/{id}/tool_approval/respond

read the parked_state blob and expect a specific shape inside
``yielded.resume_metadata.original_call``. Without LM Studio nothing
drives that park in production today, so we use the same direct-JSONB
injection pattern as ``test_yields_with_injected_park.py`` to set the
row up out-of-band, then exercise the endpoint over real HTTP.

Covered backlog items:

* T0833 — sessions tool_approval/pending on _approval park → 200 with
  the documented fields (tool_call_id / tool_name / arguments /
  parked_at), no /errors/internal envelope.
* T0834 — sessions tool_approval/pending when parked on a different
  tool (ask_user) → 404 /errors/not-found; envelope never leaks the
  ask_user resume_metadata.
* T0835 — sessions tool_approval/respond with the correct
  tool_call_id → 202 {"status":"accepted"}.
* T0836 — chats tool_approval/pending on a fresh chat with no park →
  404 /errors/not-found; clean RFC 7807 envelope.
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
# Seed helpers — minimum infra to create a session bound to an agent so we
# can inject a parked_state blob onto it.
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
            "description": "approval probe",
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
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_ladder(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> tuple[str, list[str]]:
    pid = f"llm-appr-{unique_suffix}"
    aid = f"ag-appr-{unique_suffix}"
    wp_id = f"wp-appr-{unique_suffix}"
    tpl_id = f"tpl-appr-{unique_suffix}"
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


# ---------------------------------------------------------------------------
# Park injection: matches the shape primer.api.routers.tool_approval
# reads via _approval_blob_or_404 + _build_pending_response.
# ---------------------------------------------------------------------------


async def _inject_approval_park(
    *,
    table: str,
    row_id: str,
    tool_call_id: str,
    inner_tool_name: str = "shell_exec",
    arguments: dict | None = None,
    policy_id: str = "p-approval-fixture",
    approval_type: str = "required",
    gate_reason: str = "operator gate",
) -> None:
    """Inject an _approval-shaped parked_state onto a session OR chat row.

    The tool_approval router reads:

        yielded.tool_name           # must be '_approval'
        yielded.resume_metadata.original_call.id / .name / .arguments
        yielded.resume_metadata.policy_id / .approval_type / .gate_reason
        yielded.timeout             # optional; drives timeout_at_iso
    """
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    arguments = arguments if arguments is not None else {"cmd": "ls -la"}
    parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "_approval",
            "event_key": f"approval:{row_id}:{tool_call_id}",
            "timeout": 600.0,
            "resume_metadata": {
                "tool_call_id": tool_call_id,
                "original_call": {
                    "id": tool_call_id,
                    "name": inner_tool_name,
                    "arguments": arguments,
                },
                "policy_id": policy_id,
                "approval_type": approval_type,
                "gate_reason": gate_reason,
            },
        },
        "llm_messages": [],
        "turn_no": 0,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    sql = f"""
        UPDATE {table}
        SET data = jsonb_set(
                     jsonb_set(
                       jsonb_set(
                         jsonb_set(
                           jsonb_set(data,
                             '{{parked_status}}', to_jsonb('parked'::text)),
                           '{{parked_event_key}}', to_jsonb($2::text)),
                         '{{parked_until}}', to_jsonb($3::text)),
                       '{{parked_at}}', to_jsonb($4::text)),
                     '{{parked_state}}', $5::jsonb
                   ),
            updated_at = now()
        WHERE id = $1
    """
    conn = await _pg()
    try:
        await conn.execute(
            sql,
            row_id,
            parked_state["yielded"]["event_key"],
            parked_until.isoformat(),
            now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


async def _inject_ask_user_park(
    session_id: str,
    *,
    tool_call_id: str,
    prompt: str = "Hi?",
) -> None:
    """Inject a vanilla ask_user park (NOT _approval) for cross-tool 404 test."""
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    parked_state: dict[str, Any] = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "ask_user",
            "event_key": f"ask_user:{session_id}:{tool_call_id}",
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
            parked_state["yielded"]["event_key"],
            parked_until.isoformat(),
            now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


async def _read_parked_status(table: str, row_id: str) -> str | None:
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            f"SELECT data->>'parked_status' AS parked_status "
            f"FROM {table} WHERE id = $1",
            row_id,
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


# ===========================================================================
# T0833 — sessions tool_approval/pending on _approval park → 200
# ===========================================================================


@pytest.mark.asyncio
async def test_t0833_sessions_tool_approval_pending_returns_200_on_approval_park(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0833 — When a session is parked on the _approval tool, GET
    /v1/sessions/{id}/tool_approval/pending must return 200 with the
    documented envelope (tool_call_id / tool_name / arguments /
    parked_at present), reading from parked_state.yielded
    .resume_metadata.original_call.

    Pins primer/api/routers/tool_approval.py:_build_pending_response.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-appr-{unique_suffix}"
    try:
        await _inject_approval_park(
            table="sessions",
            row_id=sid,
            tool_call_id=tcid,
            inner_tool_name="shell_exec",
            arguments={"cmd": "rm -rf /tmp/x"},
            policy_id="p-shell-required",
            approval_type="required",
            gate_reason="dangerous shell command",
        )
        assert await _read_parked_status("sessions", sid) == "parked"

        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tool_call_id"] == tcid, body
        assert body["tool_name"] == "shell_exec", body
        assert body["arguments"] == {"cmd": "rm -rf /tmp/x"}, body
        # parked_at is an ISO timestamp on a parked row.
        assert isinstance(body["parked_at"], str) and body["parked_at"], body
        # timeout_at is computed from parked_at + timeout=600s.
        assert isinstance(body["timeout_at"], str) and body["timeout_at"], body
        # Optional metadata round-trips.
        assert body.get("policy_id") == "p-shell-required", body
        assert body.get("approval_type") == "required", body
        assert body.get("gate_reason") == "dangerous shell command", body
        # No 5xx leak.
        body_str = json.dumps(body)
        assert "/errors/internal" not in body_str, body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0834 — sessions tool_approval/pending on ask_user park → 404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0834_sessions_tool_approval_pending_404_when_parked_on_ask_user(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0834 — A session parked on the ask_user tool (not _approval)
    must cause GET /tool_approval/pending to return 404 with the
    RFC 7807 not-found envelope. The ask_user prompt must NOT leak
    through the tool_approval endpoint.

    Mirror of T0759 for the tool_approval surface. Defends the
    cross-tool isolation in _approval_blob_or_404 at
    primer/api/routers/tool_approval.py.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-mix-{unique_suffix}"
    secret_prompt = f"please-do-not-leak-{unique_suffix}"
    try:
        await _inject_ask_user_park(
            sid, tool_call_id=tcid, prompt=secret_prompt,
        )
        assert await _read_parked_status("sessions", sid) == "parked"

        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["status"] == 404, body
        assert body["type"].endswith("/not-found"), body
        body_str = json.dumps(body)
        # Critical: ask_user content must not leak through this endpoint.
        assert secret_prompt not in body_str, (
            f"ask_user prompt leaked through tool_approval endpoint: {body}"
        )
        assert "ask_user" not in body_str.lower() or "_approval" in body_str, body
        assert "/errors/internal" not in body_str, body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0835 — sessions tool_approval/respond with correct tool_call_id → 202
# ===========================================================================


@pytest.mark.asyncio
async def test_t0835_sessions_tool_approval_respond_returns_202(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0835 — POSTing to /tool_approval/respond with the parked
    yield's tool_call_id and a valid decision must return
    202 {"status":"accepted"}. Pins
    primer/api/routers/tool_approval.py:post_session_tool_approval_respond.

    A tool_call_id mismatch is a separate negative test; this one
    confirms the golden path lands on 202 and never leaks /errors/internal.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-ok-{unique_suffix}"
    try:
        await _inject_approval_park(
            table="sessions",
            row_id=sid,
            tool_call_id=tcid,
            inner_tool_name="delete_session",
            arguments={"id": "victim"},
        )

        r = await client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={
                "tool_call_id": tcid,
                "decision": "approved",
                "reason": "operator confirmed",
            },
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body == {"status": "accepted"}, body
        # And no internal-leak envelope.
        body_str = json.dumps(body)
        assert "/errors/internal" not in body_str, body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0836 — chats tool_approval/pending on no-park chat → 404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0836_chats_tool_approval_pending_404_when_no_park(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0836 — A freshly-created chat with parked_status=NULL must
    cause GET /v1/chats/{id}/tool_approval/pending to return a
    clean 404 /errors/not-found envelope, never /errors/internal.

    Pins the chat-surface mirror of the session pending endpoint
    at primer/api/routers/tool_approval.py:get_chat_tool_approval_pending.
    """
    pid = f"llm-chat-appr-{unique_suffix}"
    aid = f"ag-chat-appr-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    try:
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201, r.text
        chat_id = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{chat_id}")

        r = await client.get(f"/v1/chats/{chat_id}/tool_approval/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["status"] == 404, body
        assert body["type"].endswith("/not-found"), body
        body_str = json.dumps(body)
        assert "/errors/internal" not in body_str, body
    finally:
        await _cleanup(client, cleanup_urls)
