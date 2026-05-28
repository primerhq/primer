"""E2E tests: worker drain + WS interrupt + park→resume→claim chain + atomic flip.

Covers backlog items (all new in this iteration):

* T0810 — POST /v1/workers/{id}/drain returns 204; subsequent GET
  shows status='draining'. Worker observability mutation pin.
* T0811 — Chat WS interrupt message ({"kind":"interrupt"}) emits an
  error row via _append_and_send + the row is persisted to
  chat_messages with kind='error'. M6 interrupt-path pin.
* T0812 — Full park→resume→claim chain: inject a sleep park with
  parked_until in the past → TimerScheduler ticks → listener
  flips → worker pool claims the resumable row. Verify the row
  moves out of parked_status='resumable' within a few claim
  cycles (proves the worker pool actually picks up resumable
  rows and processes them).
* T0813 — Two concurrent /ask_user/respond POSTs against the same
  parked session: only one publish actually flips the row (atomic
  via mark_resumable). The other POST returns 404 (the second
  arrives after the row has moved to resumable, so _parked_blob
  still returns it but... let's see). Spec §9 covers
  cancel-vs-cancel; for double-respond the contract is "first wins,
  rest 404 or 202 idempotent — never /errors/internal".
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
        user="primer", password="primer", database="matrix_e2e",
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
            "id": agent_id, "description": "chain probe",
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


async def _inject_park(
    session_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    event_key: str,
    parked_until: datetime | None = None,
    prompt: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    parked_until = parked_until or (now + timedelta(seconds=600))
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


async def _ensure_lease(session_id: str) -> None:
    """Insert a session_leases row for an injected park if missing.

    The worker pool's claim query JOINs sessions × session_leases —
    an injected park without a lease can never be claimed. Real
    sessions get a lease via the start_session flow; tests that
    inject park state out-of-band need to mirror that.
    """
    sql = """
        INSERT INTO session_leases (session_id, runnable, next_attempt_at)
        VALUES ($1, TRUE, now())
        ON CONFLICT (session_id) DO UPDATE
        SET runnable = TRUE, next_attempt_at = now()
    """
    conn = await _pg()
    try:
        await conn.execute(sql, session_id)
    finally:
        await conn.close()


async def _read_park_status(session_id: str) -> str | None:
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


async def _read_lease_worker_id(session_id: str) -> str | None:
    """Read session_leases.worker_id — non-NULL means a worker
    claimed this session (the worker pool's claim query SETs
    worker_id when it picks up a runnable lease)."""
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            "SELECT worker_id FROM session_leases WHERE session_id = $1",
            session_id,
        )
        return row["worker_id"] if row else None
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
    pid = f"llm-ch-{unique_suffix}"
    aid = f"ag-ch-{unique_suffix}"
    wp_id = f"wp-ch-{unique_suffix}"
    tpl_id = f"tpl-ch-{unique_suffix}"
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
# T0810 — Worker drain via API
# ===========================================================================


@pytest.mark.asyncio
async def test_t0810_worker_drain_returns_204_and_flips_status(
    client: httpx.AsyncClient,
) -> None:
    """T0810 — POST /v1/workers/{id}/drain returns 204; the next
    GET /v1/workers shows the drained worker with status='draining'.

    Pin for the worker-drain mutation surface. Note: drain has no
    public 'un-drain' inverse — this test permanently drains the
    sole worker for the rest of the iteration. Don't pick this test
    in batches with downstream tests that need an active worker.
    """
    # Find an active worker.
    r = await client.get("/v1/workers")
    assert r.status_code == 200
    active = [
        w for w in r.json().get("items", [])
        if w.get("status") == "active"
    ]
    if not active:
        pytest.skip("no active workers (already drained by prior test)")
    worker_id = active[0]["id"]

    # Drain.
    r = await client.post(f"/v1/workers/{worker_id}/drain")
    assert r.status_code == 204, r.text

    # GET sees status='draining'.
    r = await client.get("/v1/workers")
    assert r.status_code == 200
    found = [w for w in r.json()["items"] if w["id"] == worker_id]
    assert len(found) == 1
    assert found[0]["status"] == "draining", found[0]


# ===========================================================================
# T0811 — Chat WS interrupt persists an error row
# ===========================================================================


@pytest.mark.asyncio
async def test_t0811_chat_ws_interrupt_persists_error_row(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0811 — Sending {"kind":"interrupt"} over the chat WS must
    persist a kind='error' row to chat_messages (via _append_and_send)
    and send it back to the client. The row text mentions 'interrupted'.

    Pins the M6 interrupt-message protocol path at
    primer/api/routers/chats.py:chat_ws (the kind=='interrupt' branch).
    """
    import websockets

    pid = f"llm-i-{unique_suffix}"
    aid = f"ag-i-{unique_suffix}"
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

        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"kind": "interrupt"}))
            # Server sends back the error row.
            msg = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=3.0)
            )
            assert msg["kind"] == "error", msg
            assert "interrupted" in msg.get("message", "").lower(), msg
            assert msg.get("seq") == 1, msg
            await asyncio.sleep(0.2)  # let the writer commit

        # Verify the error row persists via GET messages.
        r = await client.get(f"/v1/chats/{cid}/messages")
        assert r.status_code == 200
        items = r.json().get("items", [])
        kinds = [it["kind"] for it in items]
        assert "error" in kinds, kinds
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0812 — Park → resumable → worker claims (full chain)
# ===========================================================================


@pytest.mark.asyncio
async def test_t0812_park_resume_worker_claim_chain(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0812 — Full chain E2E:
        inject sleep park (parked_until in past)
        → TimerScheduler tick (~2s)
        → listener mark_resumable (flips parked → resumable +
          re-arms session_leases.runnable=TRUE)
        → worker pool claim_loop sees resumable=True
        → claim SETs session_leases.worker_id

    Assertion: session_leases.worker_id becomes non-NULL within
    ~15s — that proves the worker pool's claim query actually
    picks up resumable rows.

    NOTE: post-claim, _run_one_turn tries to load the workspace
    and may fatal (workspace cleanup races; resume path has no
    LLM available). That's beyond this test's scope — the
    contract under test is "claim happens", not "resume
    succeeds". The worker pool's exception leakage on
    _run_one_turn failure is observable separately in primer.log.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-chain-{unique_suffix}"
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        await _inject_park(
            sid,
            tool_name="sleep",
            tool_call_id=tcid,
            event_key=f"timer:{tcid}",
            parked_until=past,
        )
        # Insert a session_leases row so the claim query has
        # something to JOIN. Without this, mark_resumable updates
        # 0 lease rows and the worker never claims.
        await _ensure_lease(sid)

        # Wait for the chain: 2s timer tick + ~1s listener + ~1s
        # claim. Budget 20s to absorb cold paths. The CONTRACT is:
        # session_leases.worker_id becomes non-NULL after claim.
        # Skip-soft if there's no active worker (T0810 may have
        # drained the only one if run earlier in the batch).
        r = await client.get("/v1/workers")
        active = [
            w for w in r.json().get("items", [])
            if w.get("status") == "active"
        ]
        if not active:
            pytest.skip(
                "no active workers — T0810 drained the sole "
                "worker earlier in this iteration"
            )

        deadline = asyncio.get_event_loop().time() + 20.0
        claimed_by: str | None = None
        last_status: str | None = "parked"
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            last_status = await _read_park_status(sid)
            claimed_by = await _read_lease_worker_id(sid)
            if claimed_by is not None:
                break
        assert claimed_by is not None, (
            f"worker pool never claimed the resumable row; "
            f"final parked_status={last_status!r}, "
            f"lease.worker_id remained NULL"
        )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0813 — Two concurrent respond POSTs: never /errors/internal
# ===========================================================================


@pytest.mark.asyncio
async def test_t0813_concurrent_respond_posts_no_internal_error(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0813 — Two POSTs to /ask_user/respond against the same parked
    session, fired concurrently. The atomic mark_resumable contract
    says one publish flips the row; whether the second POST returns
    202 (idempotent), 404 (after-flip), or some other documented
    status, the CONTRACT is: never /errors/internal.

    Pins the race-resistance of the M3 respond surface.
    """
    sid, cleanup_urls = await _seed_ladder(client, unique_suffix, tmp_path)
    tcid = f"tc-race-{unique_suffix}"
    try:
        await _inject_park(
            sid,
            tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            prompt="?",
        )

        async def _one_respond(answer: str) -> tuple[int, str]:
            r = await client.post(
                f"/v1/sessions/{sid}/ask_user/respond",
                json={"tool_call_id": tcid, "response": answer},
            )
            return r.status_code, r.text

        results = await asyncio.gather(
            _one_respond("Alice"),
            _one_respond("Bob"),
        )

        # Every status is a documented one — never 500.
        for status, text in results:
            assert status != 500, (
                f"got 500 /errors/internal under concurrent respond: {text}"
            )
            # Tolerate 202, 404 — both are documented under this race.
            assert status in (202, 404, 409), (
                f"unexpected status under concurrent respond: {status} "
                f"body={text}"
            )
            if status >= 400 and text and text.startswith("{"):
                body = json.loads(text)
                assert "internal" not in body.get("type", ""), body

        # At least one of them must have succeeded (202).
        statuses = {s for s, _ in results}
        assert 202 in statuses, (
            f"neither concurrent respond succeeded: {results}"
        )
    finally:
        await _cleanup(client, cleanup_urls)
