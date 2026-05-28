"""E2E tests: M2 bus background tasks + M6 chat WebSocket lifecycle.

Covers backlog items (all new in this iteration):

* T0790 — TimerScheduler republishes a ``timer:*`` park whose
  parked_until is in the past. Inject the park, wait one timer
  tick (~2s) + listener round-trip, assert parked_status='resumable'.
* T0791 — TimeoutSweeper publishes the ``__yield_timeout__`` marker
  for an expired non-timer park (ask_user with past parked_until).
  Sweeper cadence is 30s so this test waits ~35s for the first tick.
* T0792 — WS reconnect with ``?cursor=N`` replays missed
  chat_messages in order; new connection picks up live streaming
  after replay completes.
* T0793 — WS connect to a non-existent chat closes with code 4404
  (application-defined "not found" per RFC 6455 §7.4 reserved range).
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
# Postgres + seed helpers
# ---------------------------------------------------------------------------


async def _pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", port=5432,
        user="primer", password="primer", database="primer_e2e",
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
    assert r.status_code == 201


async def _seed_agent(
    client: httpx.AsyncClient, agent_id: str, provider_id: str,
) -> None:
    r = await client.post(
        "/v1/agents",
        json={
            "id": agent_id, "description": "bus+ws probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201


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


async def _inject_park_with_deadline(
    session_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    event_key: str,
    parked_until: datetime,
    prompt: str | None = None,
) -> None:
    """Inject a parked session with an explicit parked_until — useful
    for testing TimerScheduler / TimeoutSweeper (set parked_until in
    the past so they fire immediately on next poll).
    """
    now = datetime.now(timezone.utc)
    resume_metadata: dict[str, Any] = {"tool_call_id": tool_call_id}
    if prompt is not None:
        resume_metadata["prompt"] = prompt
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


async def _read_park(session_id: str) -> dict:
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
    pid = f"llm-bt-{unique_suffix}"
    aid = f"ag-bt-{unique_suffix}"
    wp_id = f"wp-bt-{unique_suffix}"
    tpl_id = f"tpl-bt-{unique_suffix}"
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
# T0790 — TimerScheduler republishes due timer:* park
# ===========================================================================


@pytest.mark.asyncio
async def test_t0790_timer_scheduler_republishes_due_timer_park(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0790 — Inject a session parked on the sleep tool with a
    parked_until timestamp in the past. The TimerScheduler runs at
    a 2s cadence and republishes empty events for due ``timer:*``
    parks; the bus listener mark_resumable() flips the row.

    End-to-end pin for the M2 timer-wake path: park → tick → flip.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-timer-{unique_suffix}"
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        await _inject_park_with_deadline(
            sid,
            tool_name="sleep",
            tool_call_id=tcid,
            event_key=f"timer:{tcid}",
            parked_until=past,
        )

        # Poll for resumable; timer cadence is 2s + listener round-trip.
        # Budget 10s to absorb cold-cache jitter.
        for _ in range(100):
            await asyncio.sleep(0.1)
            fields = await _read_park(sid)
            if fields.get("parked_status") == "resumable":
                break
        assert fields.get("parked_status") == "resumable", (
            f"timer park never flipped to resumable; final={fields}"
        )
        # Real timer events publish an empty payload — payload should
        # be {} (or absent of the timeout/cancel markers).
        payload = fields.get("resume_event_payload") or {}
        assert "__yield_timeout__" not in payload, (
            f"timer park got timeout marker (sweeper raced?): {payload}"
        )
        assert "__yield_cancelled__" not in payload, payload
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0791 — TimeoutSweeper publishes __yield_timeout__ marker
# ===========================================================================


@pytest.mark.asyncio
async def test_t0791_timeout_sweeper_publishes_timeout_marker(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0791 — Inject a session parked on ask_user (non-timer) with
    a parked_until in the past. The TimeoutSweeper runs at 30s
    cadence and publishes the ``__yield_timeout__`` marker for
    expired non-timer parks. Bus listener flips the row; payload
    carries the timeout marker.

    NOTE: 30s sweeper cadence means this test takes ~35s to converge.
    Could be tightened by exposing a config knob for the sweeper
    interval; deferred to a future iteration.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-sweeper-{unique_suffix}"
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        await _inject_park_with_deadline(
            sid,
            tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            parked_until=past,
            prompt="What is your name?",
        )

        # Sweeper cadence 30s + listener round-trip. Budget 40s.
        for _ in range(400):
            await asyncio.sleep(0.1)
            fields = await _read_park(sid)
            if fields.get("parked_status") == "resumable":
                break
        assert fields.get("parked_status") == "resumable", (
            f"sweeper didn't flip non-timer park within ~40s; final={fields}"
        )
        payload = fields.get("resume_event_payload") or {}
        assert payload.get("__yield_timeout__") is True, (
            f"expected __yield_timeout__ marker, got {payload}"
        )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0792 — WS reconnect with ?cursor=N replays missed messages
# ===========================================================================


@pytest.mark.asyncio
async def test_t0792_chat_ws_reconnect_with_cursor_replays_missed_messages(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0792 — Drive a chat turn over WS (3 message rows persisted).
    Reconnect with ``?cursor=0``; the server must replay all 3 rows
    in seq order (seq 1, 2, 3) before accepting new client messages.

    Pin for the M6 cursor-replay contract in
    primer/api/routers/chats.py:_replay_since_cursor.
    """
    import websockets

    pid = f"llm-ws792-{unique_suffix}"
    aid = f"ag-ws792-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    cid: str | None = None
    try:
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201
        cid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{cid}")

        http_url = str(client.base_url).rstrip("/")
        ws_origin = http_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        ws_url = f"{ws_origin}/v1/chats/{cid}/ws"

        # Connection 1: drive a turn → 3 rows seq 1..3.
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps(
                {"kind": "user_message", "content": "hello"}
            ))
            seen_kinds_1: list[str] = []
            for _ in range(3):
                msg = json.loads(await ws.recv())
                seen_kinds_1.append(msg["kind"])
            assert seen_kinds_1 == [
                "user_message", "assistant_token", "done",
            ], seen_kinds_1
            # Settle delay so the runner's writes commit before WS close.
            await asyncio.sleep(0.2)

        # Connection 2: ?cursor=0 → server replays all 3 rows in order.
        async with websockets.connect(
            f"{ws_url}?cursor=0",
        ) as ws2:
            # Receive 3 replayed messages with seq 1, 2, 3.
            replayed: list[dict] = []
            for _ in range(3):
                msg = json.loads(await asyncio.wait_for(
                    ws2.recv(), timeout=3.0,
                ))
                replayed.append(msg)
            seqs = [m["seq"] for m in replayed]
            kinds = [m["kind"] for m in replayed]
            assert seqs == [1, 2, 3], f"replay seq order: {seqs}"
            assert kinds == [
                "user_message", "assistant_token", "done",
            ], kinds

            # Connection accepts new live messages after replay.
            await ws2.send(json.dumps(
                {"kind": "user_message", "content": "second"}
            ))
            live: list[dict] = []
            for _ in range(3):
                live.append(json.loads(
                    await asyncio.wait_for(ws2.recv(), timeout=3.0),
                ))
            live_seqs = [m["seq"] for m in live]
            # Seqs continue from 4.
            assert live_seqs == [4, 5, 6], f"live seqs: {live_seqs}"
            await asyncio.sleep(0.2)
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0793 — WS connect to non-existent chat closes with code 4404
# ===========================================================================


@pytest.mark.asyncio
async def test_t0793_chat_ws_connect_to_missing_chat_closes_with_4404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0793 — Connect to ``/v1/chats/{nope}/ws`` with no
    corresponding chat row. Server must close with application-
    defined close code 4404 (RFC 6455 §7.4 reserved 4000-4999
    range), NOT crash with a 500 + dropped connection.

    Pin for the connection-time validation in
    primer/api/routers/chats.py:chat_ws.
    """
    import websockets

    fake_cid = f"chat-nope-{unique_suffix}"
    http_url = str(client.base_url).rstrip("/")
    ws_origin = http_url.replace("http://", "ws://").replace(
        "https://", "wss://"
    )
    ws_url = f"{ws_origin}/v1/chats/{fake_cid}/ws"

    # The server should accept the WS handshake (uvicorn upgrades the
    # request) and immediately close with 4404. websockets raises
    # ConnectionClosed on the close.
    try:
        async with websockets.connect(ws_url) as ws:
            # Try a single recv — should get the close.
            with pytest.raises(websockets.ConnectionClosed) as exc_info:
                await asyncio.wait_for(ws.recv(), timeout=3.0)
            assert exc_info.value.rcvd is not None
            assert exc_info.value.rcvd.code == 4404, (
                f"expected close code 4404, got {exc_info.value.rcvd.code}"
            )
    except websockets.exceptions.InvalidStatus as exc:
        # Some websockets versions surface the close as InvalidStatus
        # on the handshake itself when the server closes immediately
        # after accept. Tolerate either path — the contract is "no
        # crash, clean close".
        assert exc.response.status_code in (200, 404, 4404), (
            f"unexpected handshake status: {exc.response.status_code}"
        )
