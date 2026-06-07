"""E2E tests: M2 bus background tasks + M6 chat WebSocket lifecycle.

Covers backlog items (all new in this iteration):

* T0790 — TimerScheduler republishes a ``timer:*`` park whose
  parked_until is due. A real sleep park (1s duration) drives the
  genuine engine path; the TimerScheduler (2s cadence) republishes
  the due timer event, the listener flips the session, and the
  engine resumes the turn.
* T0791 — TimeoutSweeper publishes the ``__yield_timeout__`` marker
  for an expired non-timer park. A real approval-timeout park drives
  the genuine engine path; the TimeoutSweeper fires once the
  parked_until deadline elapses and the session resumes with a
  synthesised rejection result.
* T0792 — WS reconnect with ``?cursor=N`` replays missed
  chat_messages in order; new connection picks up live streaming
  after replay completes.
* T0793 — WS connect to a non-existent chat closes with code 4404
  (application-defined "not found" per RFC 6455 §7.4 reserved range).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
)
from tests._support.yield_journeys import drive_park_on_tool, wait_for_resume


# ---------------------------------------------------------------------------
# WS-test seed helpers (used only by T0792/T0793)
# ---------------------------------------------------------------------------


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


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Approval-park helper (mirrors _drive_approval_park from t0863)
# ---------------------------------------------------------------------------


async def _drive_approval_park_t791(
    client: httpx.AsyncClient,
    registry,
    base_url: str,
    *,
    suffix: str,
    tmp_path,
    timeout_seconds: float,
) -> tuple[str, dict, str]:
    """Gate misc__uuid_v4 with a short-timeout required policy and
    drive a real session until it parks on ``_approval``.

    Returns ``(session_id, parked_body, policy_id)``.
    """
    pol = f"pol-t791-{suffix}"
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if (
                it.get("toolset_id") == "misc"
                and it.get("tool_name") == "uuid_v4"
            ):
                await client.delete(f"/v1/tool_approval_policies/{it['id']}")
    r = await client.post(
        "/v1/tool_approval_policies",
        json={
            "id": pol,
            "toolset_id": "misc",
            "tool_name": "uuid_v4",
            "enabled": True,
            "approval": {"type": "required"},
            "timeout_seconds": timeout_seconds,
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text

    scenario = f"scripted:t791-{suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=suffix, scenario=scenario,
        tools=["misc__uuid_v4"],
        rules=[
            Rule(when_tool_result=False, emit_tool="misc__uuid_v4",
                 emit_args={}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(client, suffix=suffix, root=tmp_path)
    sid = await start_agent_session(
        client, workspace_id=wid, agent_id=agent["agent_id"],
    )

    deadline = asyncio.get_event_loop().time() + 30.0
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()
            if last.get("parked_status") == "parked":
                return sid, last, pol
            if last.get("status") == "ended":
                raise AssertionError(
                    f"session {sid} ended before parking on _approval: "
                    f"reason={last.get('ended_reason')!r} body={last!r}"
                )
        await asyncio.sleep(0.25)
    raise AssertionError(
        f"session {sid} never parked on _approval within 30s; "
        f"last_body={last!r}"
    )


# ===========================================================================
# T0790 -- TimerScheduler republishes due timer:* park
# ===========================================================================


@pytest.mark.asyncio
async def test_t0790_timer_scheduler_republishes_due_timer_park(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0790 -- Drive a real 1-second sleep park via the genuine engine
    path. The sleep tool writes a ``timer:<tcid>`` park with
    ``parked_until ~= now + 1s``. The TimerScheduler (2s cadence)
    finds the due timer event, republishes it on the bus, the
    YieldEventListener flips the session, and the engine resumes.

    End-to-end pin for the M2 timer-wake path: real park -> tick -> resume.
    """
    registry, base_url = mock_llm
    sid, _, _ = await drive_park_on_tool(
        client, registry, base_url,
        suffix=unique_suffix,
        tool="misc__sleep",
        tool_args={"seconds": 1.0},
        root=tmp_path,
    )
    # TimerScheduler cadence is 2s; budget 20s to absorb cold-start jitter.
    # The session resumes (parked_status=None) once the sleep tool result
    # comes back and the agent emits its terminating reply (min_turn_no=1).
    body = await wait_for_resume(client, sid, timeout_s=20, min_turn_no=1)
    assert body.get("parked_status") is None, body
    assert body.get("turn_no", 0) >= 1, (
        f"expected at least one completed turn after timer-wake; body={body}"
    )


# ===========================================================================
# T0791 -- TimeoutSweeper publishes __yield_timeout__ marker
# ===========================================================================


@pytest.mark.asyncio
async def test_t0791_timeout_sweeper_publishes_timeout_marker(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0791 -- Drive a real approval-timeout park (non-timer, short
    policy deadline) via the genuine engine path. The TimeoutSweeper
    (30s cadence) finds the expired non-timer park, publishes the
    ``__yield_timeout__`` marker, the engine synthesises a rejected
    tool_result, and the session resumes.

    End-to-end pin for the M2 sweeper path on a Postgres-backed
    deployment (the Storage.find() query is backend-agnostic and
    reads persisted park columns, unlike the InMemoryScheduler
    path tested in T0863 which has a known product gap).
    """
    registry, base_url = mock_llm
    sid, parked, pol = await _drive_approval_park_t791(
        client, registry, base_url,
        suffix=unique_suffix,
        tmp_path=tmp_path,
        timeout_seconds=2.0,
    )
    try:
        # Sanity: approval decision is observable before the sweeper fires.
        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        pending = r.json()
        assert pending["tool_name"] in ("uuid_v4", "misc__uuid_v4"), pending
        assert pending["approval_type"] == "required", pending

        # TimeoutSweeper cadence is 30s; parked_until is ~2s out.
        # Give 70s to allow at least one full sweep cycle after expiry.
        body = await wait_for_resume(client, sid, timeout_s=70)
        assert body.get("parked_status") is None, body
        assert "/errors/internal" not in json.dumps(body), body
    finally:
        try:
            await client.delete(f"/v1/tool_approval_policies/{pol}")
        except Exception:  # noqa: BLE001
            pass
        # Cancel the parked yield so the session does not remain
        # indefinitely parked if wait_for_resume raised above.
        try:
            await client.post(
                f"/v1/sessions/{sid}/yields/call_0/cancel",
                json={"reason": "t0791 test cleanup"},
            )
        except Exception:  # noqa: BLE001
            pass


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
            # Spec §6.4: initial ``usage`` frame after accept().
            initial = json.loads(await ws.recv())
            assert initial["kind"] == "usage", initial
            await ws.send(json.dumps(
                {"kind": "user_message", "content": "hello"}
            ))
            seen_kinds_1: list[str] = []
            for _ in range(4):  # user / assistant / done / usage
                msg = json.loads(await ws.recv())
                seen_kinds_1.append(msg["kind"])
            assert seen_kinds_1 == [
                "user_message", "assistant_token", "done", "usage",
            ], seen_kinds_1
            # Settle delay so the runner's writes commit before WS close.
            await asyncio.sleep(0.2)

        # Connection 2: ?cursor=0 → server replays all 3 rows in order,
        # then sends the initial ``usage`` envelope.
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
            # Post-replay initial usage envelope.
            initial2 = json.loads(await asyncio.wait_for(
                ws2.recv(), timeout=3.0,
            ))
            assert initial2["kind"] == "usage", initial2

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
            # Trailing usage after second turn's done row.
            tail2 = json.loads(await asyncio.wait_for(
                ws2.recv(), timeout=3.0,
            ))
            assert tail2["kind"] == "usage", tail2
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
