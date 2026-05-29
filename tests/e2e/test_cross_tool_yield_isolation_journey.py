"""E2E: cross-tool yield isolation journey across 3 parked sessions.

Multi-subsystem journey that parks THREE sessions on different
yielding tools in parallel, then verifies the per-tool REST surfaces
return the right rows and the cancel-yielded-tool path only affects
its target — no cross-tool leakage.

Subsystems exercised in one test:

  1. Session lifecycle + park state machine across 3 distinct
     yielding-tool kinds: ``ask_user``, ``sleep``, ``_approval``.
  2. Per-tool REST endpoint families:
       - GET /v1/sessions/{id}/ask_user/pending
       - GET /v1/sessions/{id}/tool_approval/pending
  3. Cross-tool 404 envelope contract — each endpoint returns 404
     for sessions parked on a different tool, with the RFC 7807
     ``/errors/not-found`` type and NO bleed-through of the other
     tool's resume_metadata into the response body.
  4. Cancel-yielded-tool path
     (POST /v1/sessions/{id}/yields/{tcid}/cancel) on one of the
     three sessions; bus listener flips the row to resumable +
     stamps the __yield_cancelled__ marker.
  5. Postgres state-machine read-back: psql confirms only the
     cancelled session has the cancel marker in
     parked_state.resume_event_payload; the other two remain in
     'parked' state with their original parked_state intact.

Covers backlog item T0854.

The asyncpg injection pattern is the same one used by T0759-T0784
and T0833-T0836 — direct JSONB UPDATE to set up the park state
the worker would otherwise write, since no production code path
drives these three park kinds without LM Studio + a tools-capable
model.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest


# ---------------------------------------------------------------------------
# Postgres connection (matches scripts/e2e/bringup.sh defaults)
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
# Seed helpers (provider + agent + workspace + 3 sessions)
# ---------------------------------------------------------------------------


async def _seed_llm_provider(client: httpx.AsyncClient, pid: str) -> None:
    r = await client.post("/v1/llm_providers", json={
        "id": pid,
        "provider": "ollama",
        "config": {"url": "http://127.0.0.1:9999"},
        "models": [{"name": "fake-model", "context_length": 4096}],
        "limits": {"max_concurrency": 1},
    })
    assert r.status_code == 201, f"seed LLM failed: {r.text}"


async def _seed_agent(
    client: httpx.AsyncClient, agent_id: str, provider_id: str,
) -> None:
    r = await client.post("/v1/agents", json={
        "id": agent_id,
        "description": "T0854 cross-tool isolation probe",
        "model": {"provider_id": provider_id, "model_name": "fake-model"},
        "tools": [],
        "system_prompt": ["probe"],
    })
    assert r.status_code == 201, f"seed agent failed: {r.text}"


async def _seed_workspace(
    client: httpx.AsyncClient, wp_id: str, tpl_id: str, tmp_path,
) -> str:
    r = await client.post("/v1/workspace_providers", json={
        "id": wp_id, "provider": "local",
        "config": {"kind": "local", "root_path": str(tmp_path)},
    })
    assert r.status_code == 201, r.text
    r = await client.post("/v1/workspace_templates", json={
        "id": tpl_id, "description": "T0854 tpl",
        "provider_id": wp_id, "backend": {"kind": "local"},
    })
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


# ---------------------------------------------------------------------------
# Park injection — covers all three yielding-tool shapes the live
# REST surface knows about (ask_user, sleep, _approval).
# ---------------------------------------------------------------------------


async def _inject_park(
    session_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    event_key: str,
    prompt: str | None = None,
    requested_seconds: float | None = None,
    approval_metadata: dict | None = None,
) -> None:
    """Inject parked_* fields onto a session row.

    Shape parameters:

    - ``tool_name == "ask_user"`` — adds a ``prompt`` into
      resume_metadata; readable via /ask_user/pending.
    - ``tool_name == "sleep"`` — adds ``requested_seconds`` (defaults
      to 30s); not directly readable via the public REST surface
      until resume.
    - ``tool_name == "_approval"`` — embeds an ``original_call``
      dict in resume_metadata so /tool_approval/pending can build
      its response envelope.
    """
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    resume_metadata: dict[str, Any] = {"tool_call_id": tool_call_id}
    if prompt is not None:
        resume_metadata["prompt"] = prompt
    if tool_name == "sleep":
        resume_metadata["requested_seconds"] = requested_seconds or 30.0
    if tool_name == "_approval" and approval_metadata is not None:
        resume_metadata.update(approval_metadata)

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
            sql, session_id, event_key,
            parked_until.isoformat(), now.isoformat(),
            json.dumps(parked_state),
        )
    finally:
        await conn.close()


async def _read_park_state(session_id: str) -> dict:
    """Return parked_status + parked_state for the session row."""
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            "SELECT data->>'parked_status' AS parked_status, "
            "data->'parked_state' AS parked_state "
            "FROM sessions WHERE id = $1",
            session_id,
        )
        if row is None:
            return {}
        ps = row["parked_state"]
        if isinstance(ps, str):
            ps = json.loads(ps)
        return {"parked_status": row["parked_status"], "parked_state": ps}
    finally:
        await conn.close()


# ===========================================================================
# T0854 — Cross-tool yield isolation across 3 parallel parks
# ===========================================================================


@pytest.mark.asyncio
async def test_t0854_cross_tool_yield_isolation_journey(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0854 — Park three sessions on three different yielding tools
    in the same workspace; verify the per-tool REST surface only
    matches its own tool, and cancel-yielded-tool only affects its
    target.

    Steps:

      1. Seed LLM provider + agent + workspace (single ladder).
      2. Create 3 sessions (auto_start=False) under that workspace.
      3. Inject parks:
           - Session A → ask_user (with a secret prompt to detect leakage)
           - Session B → sleep   (with requested_seconds=30)
           - Session C → _approval (with original_call metadata)
      4. Cross-tool 404 primer:
           - GET A/ask_user/pending      → 200 (matches)
           - GET A/tool_approval/pending → 404 (cross-tool)
           - GET B/ask_user/pending      → 404 (sleep, not ask_user)
           - GET B/tool_approval/pending → 404 (cross-tool)
           - GET C/ask_user/pending      → 404 (cross-tool)
           - GET C/tool_approval/pending → 200 (matches)
      5. No /errors/internal leakage AND no resume_metadata cross-talk
         in any 404 envelope.
      6. Cancel session B's sleep yield via
         POST /v1/sessions/B/yields/{tcid}/cancel.
      7. psql-confirm only B has the __yield_cancelled__ marker in
         parked_state.resume_event_payload + parked_status=resumable.
         A and C remain in parked_status=parked with no resume payload.

    8 routers + 3 distinct yielding tools + 1 cancel API + 1 bus
    listener + JSONB state machine.
    """
    pid = f"t854-llm-{unique_suffix}"
    aid = f"t854-ag-{unique_suffix}"
    wp_id = f"t854-wp-{unique_suffix}"
    tpl_id = f"t854-tpl-{unique_suffix}"
    workspace_id: str | None = None
    sid_a: str | None = None
    sid_b: str | None = None
    sid_c: str | None = None
    cleanup_urls: list[str] = []
    try:
        # ----- 1+2. Seed ladder + 3 sessions ------------------------------
        await _seed_llm_provider(client, pid)
        await _seed_agent(client, aid, pid)
        workspace_id = await _seed_workspace(
            client, wp_id, tpl_id, tmp_path,
        )
        sid_a = await _seed_session(client, workspace_id, aid)
        sid_b = await _seed_session(client, workspace_id, aid)
        sid_c = await _seed_session(client, workspace_id, aid)
        cleanup_urls = [
            f"/v1/workspaces/{workspace_id}/sessions/{sid_a}/cancel",
            f"/v1/workspaces/{workspace_id}/sessions/{sid_b}/cancel",
            f"/v1/workspaces/{workspace_id}/sessions/{sid_c}/cancel",
            f"/v1/workspaces/{workspace_id}",
            f"/v1/workspace_templates/{tpl_id}",
            f"/v1/workspace_providers/{wp_id}",
            f"/v1/agents/{aid}",
            f"/v1/llm_providers/{pid}",
        ]

        # ----- 3. Inject 3 distinct parks ---------------------------------
        tcid_a = f"tc-aa-{unique_suffix}"
        tcid_b = f"tc-bb-{unique_suffix}"
        tcid_c = f"tc-cc-{unique_suffix}"
        secret_ask_prompt = f"DO-NOT-LEAK-{unique_suffix}"

        await _inject_park(
            sid_a, tool_name="ask_user",
            tool_call_id=tcid_a,
            event_key=f"ask_user:{sid_a}:{tcid_a}",
            prompt=secret_ask_prompt,
        )
        await _inject_park(
            sid_b, tool_name="sleep",
            tool_call_id=tcid_b,
            event_key=f"timer:{tcid_b}",
            requested_seconds=30.0,
        )
        await _inject_park(
            sid_c, tool_name="_approval",
            tool_call_id=tcid_c,
            event_key=f"approval:{sid_c}:{tcid_c}",
            approval_metadata={
                "original_call": {
                    "id": tcid_c,
                    "name": "delete_workspace",
                    "arguments": {"id": "ws-target"},
                },
                "policy_id": "p-isolation",
                "approval_type": "required",
                "gate_reason": "cross-tool isolation probe",
            },
        )

        # Sanity: all three rows show as parked.
        for sid in (sid_a, sid_b, sid_c):
            assert (await _read_park_state(sid))["parked_status"] == "parked"

        # ----- 4. Cross-tool 404 primer -----------------------------------
        # Session A (ask_user) — only ask_user/pending should match.
        r = await client.get(f"/v1/sessions/{sid_a}/ask_user/pending")
        assert r.status_code == 200, r.text
        body_a = r.json()
        assert body_a["tool_call_id"] == tcid_a, body_a
        assert body_a["prompt"] == secret_ask_prompt, body_a

        r = await client.get(f"/v1/sessions/{sid_a}/tool_approval/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["type"].endswith("/not-found"), body
        # Critical: ask_user prompt must not leak through the
        # tool_approval endpoint.
        assert secret_ask_prompt not in json.dumps(body), body

        # Session B (sleep) — neither endpoint should match.
        r = await client.get(f"/v1/sessions/{sid_b}/ask_user/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["type"].endswith("/not-found"), body
        # No requested_seconds leakage either.
        assert "requested_seconds" not in r.text, r.text

        r = await client.get(f"/v1/sessions/{sid_b}/tool_approval/pending")
        assert r.status_code == 404, r.text
        assert r.json()["type"].endswith("/not-found"), r.text

        # Session C (_approval) — only tool_approval/pending should match.
        r = await client.get(f"/v1/sessions/{sid_c}/ask_user/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["type"].endswith("/not-found"), body
        # _approval's policy_id / gate_reason / original_call must not
        # leak through the ask_user endpoint.
        assert "p-isolation" not in json.dumps(body), body
        assert "delete_workspace" not in json.dumps(body), body

        r = await client.get(f"/v1/sessions/{sid_c}/tool_approval/pending")
        assert r.status_code == 200, r.text
        body_c = r.json()
        assert body_c["tool_name"] == "delete_workspace", body_c
        assert body_c["policy_id"] == "p-isolation", body_c
        assert body_c["gate_reason"] == "cross-tool isolation probe", body_c

        # ----- 5. Global /errors/internal sweep ----------------------------
        # Re-probe everything (cheap), confirm no path leaked a 5xx
        # /errors/internal envelope at any point.
        for sid, ep in (
            (sid_a, "ask_user"),
            (sid_a, "tool_approval"),
            (sid_b, "ask_user"),
            (sid_b, "tool_approval"),
            (sid_c, "ask_user"),
            (sid_c, "tool_approval"),
        ):
            r = await client.get(f"/v1/sessions/{sid}/{ep}/pending")
            assert "/errors/internal" not in r.text, (
                f"GET /v1/sessions/{sid}/{ep}/pending leaked an internal "
                f"envelope: {r.text!r}"
            )

        # ----- 6. Cancel session B's sleep yield --------------------------
        r = await client.post(
            f"/v1/sessions/{sid_b}/yields/{tcid_b}/cancel",
            json={"reason": "t0854 operator cancel of sleep"},
        )
        assert r.status_code in (200, 202), r.text

        # ----- 7. psql state-machine read-back ----------------------------
        # The bus listener round-trip is asynchronous; poll up to 10s
        # for B's row to land in parked_status='resumable' with the
        # cancel marker in resume_event_payload.
        import asyncio

        deadline = asyncio.get_event_loop().time() + 10.0
        b_state: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            b_state = await _read_park_state(sid_b)
            ps = b_state.get("parked_state") or {}
            payload = ps.get("resume_event_payload")
            if (
                b_state.get("parked_status") == "resumable"
                and isinstance(payload, dict)
                and payload.get("__yield_cancelled__") is True
            ):
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(
                f"session B did not flip to resumable+cancelled within 10s; "
                f"final state: {b_state!r}"
            )
        b_payload = b_state["parked_state"]["resume_event_payload"]
        assert b_payload["reason"] == "t0854 operator cancel of sleep", b_payload

        # A and C remain parked with no resume payload — proves the
        # cancel ONLY targeted session B.
        a_state = await _read_park_state(sid_a)
        assert a_state.get("parked_status") == "parked", a_state
        assert (a_state.get("parked_state") or {}).get(
            "resume_event_payload"
        ) is None, a_state

        c_state = await _read_park_state(sid_c)
        assert c_state.get("parked_status") == "parked", c_state
        assert (c_state.get("parked_state") or {}).get(
            "resume_event_payload"
        ) is None, c_state
    finally:
        # Best-effort unwind. Cancel endpoints are POST, not DELETE;
        # everything else is DELETE.
        async def _try_call(url: str) -> None:
            try:
                if url.endswith("/cancel"):
                    await client.post(url)
                else:
                    await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
        for url in cleanup_urls:
            await _try_call(url)
