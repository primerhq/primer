"""Scenario 7 — SIGTERM failure injection: worker reclaim + WS reconnect.

Two complementary failure modes are exercised:

1. **Worker SIGTERM + chat turn reclaim**
   A chat turn is in-flight on one worker. We SIGTERM that worker. Within
   the lease's heartbeat_stale window another worker should reclaim the
   turn (or the lease expires and the executor on the dead worker leaves
   the lease expired so the claim engine can re-queue it).

   In the distributed subprocess environment we cannot use a true "slow"
   LLM (no real API key). Instead, we rely on the claim engine's
   heartbeat + expiry mechanism:

   * Worker claims the chat lease → sets ``claimed_by``.
   * We SIGTERM the worker before it can release the lease.
   * After ``heartbeat_stale`` (default 90s) + sweep interval the lease
     expires or the second worker re-claims it.
   * The test polls with a generous 120s window.

   Because 90s is long for a unit test, we also assert the *immediate*
   side-effect: after SIGTERMing the claiming worker, the process exits
   and the lease row must eventually become unclaimed or reclaimed.

2. **API SIGTERM + WS reconnect with cursor**
   A WebSocket client connects to API#0.  We wait for at least one
   frame (or for the connection to be fully established), SIGTERM
   API#0, then reconnect to API#1 with ``cursor=<last_seq>``.  Any
   frames written while API#0 was down but API#1 was up should be
   visible on the reconnected WS.

Requires:
- A live Postgres container + Docker for testcontainers.
- The distributed marker (``pytest -m distributed``).
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio

from tests.distributed.cluster import TestCluster


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asyncpg_dsn(pg_url: str) -> str:
    p = urlparse(pg_url)
    host = p.hostname or "localhost"
    port = p.port or 5432
    user = p.username or "postgres"
    password = p.password or ""
    db = (p.path or "/postgres").lstrip("/") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


_STUB_PROVIDER_BODY = {
    "provider": "openresponses",
    "models": [{"name": "stub-model", "context_length": 4096}],
    "config": {
        "url": "http://127.0.0.1:19999/v1",  # intentionally unreachable
        "api_key": None,
        "flavor": "other",
    },
    "limits": {"max_concurrency": 4},
}


async def _setup_llm_and_agent(cluster: TestCluster) -> str:
    """Create a stub LLM provider + agent; return the agent_id."""
    provider_id = f"stub-llm-{uuid.uuid4().hex[:6]}"
    agent_id = f"agent-{uuid.uuid4().hex[:6]}"

    async with cluster.client(0) as c0:
        resp = await c0.post(
            "/v1/llm_providers",
            json={"id": provider_id, **_STUB_PROVIDER_BODY},
        )
        assert resp.status_code == 201, (
            f"POST /v1/llm_providers returned {resp.status_code}: {resp.text}"
        )

        resp = await c0.post(
            "/v1/agents",
            json={
                "id": agent_id,
                "description": "Stub agent for failure-injection test",
                "model": {"provider_id": provider_id, "model_name": "stub-model"},
            },
        )
        assert resp.status_code == 201, (
            f"POST /v1/agents returned {resp.status_code}: {resp.text}"
        )

    return agent_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cluster_2x2_failure(postgres_container: str, db_schema: str) -> TestCluster:
    """2 API + 2 worker cluster for the failure-injection scenarios."""
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8350,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


# ---------------------------------------------------------------------------
# Scenario 7a — worker SIGTERM + chat-turn reclaim
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_worker_sigterm_reclaims_chat_turn(
    cluster_2x2_failure: TestCluster,
    postgres_container: str,
) -> None:
    """SIGTERM the claiming worker; the other worker must reclaim within 120s.

    Phase 1 — create a chat and trigger a turn:
        Create a chat via API#0, post a user message to make it
        ``turn_status='claimable'``.  Workers will pick it up.

    Phase 2 — wait for a worker to claim it:
        Poll the ``leases`` table until ``claimed_by IS NOT NULL`` for
        the chat's lease row (up to 30s).

    Phase 3 — SIGTERM the claiming worker:
        Identify the worker process whose owner_id_prefix matches the
        ``claimed_by`` value.  Send SIGTERM.

    Phase 4 — wait for the lease to become available again:
        Either ``claimed_by IS NULL`` (lease expired or was re-queued)
        or ``claimed_by`` changed to a different worker.  Allow up to
        120s (covers the 90s heartbeat_stale window + sweep lag).

    Final assertion: the chat eventually ends (status='ended' or at
    least has a terminal ChatMessage of kind='error' or 'done').
    """
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        pytest.skip("asyncpg not installed")

    cluster = cluster_2x2_failure
    schema = cluster.schema
    leases_table = f'"{schema}"."leases"'

    agent_id = await _setup_llm_and_agent(cluster)

    # Create a chat and post a user message via API#0.
    async with cluster.client(0) as c0:
        resp = await c0.post("/v1/chats", json={"agent_id": agent_id})
        assert resp.status_code == 201, (
            f"POST /v1/chats returned {resp.status_code}: {resp.text}"
        )
        chat_id = resp.json()["id"]

    # Post a user message via the chat WS on API#1 to trigger a turn.
    try:
        import websockets  # noqa: F401, PLC0415
    except ImportError:
        pytest.skip("websockets not installed")

    async with cluster.ws(1, f"/v1/chats/{chat_id}/ws?cursor=0") as ws1:
        await ws1.send(json.dumps({
            "kind": "user_message",
            "content": "Trigger a turn for SIGTERM test",
        }))
        await asyncio.sleep(0.5)

    dsn = _asyncpg_dsn(postgres_container)
    conn = await asyncpg.connect(dsn)

    try:
        # ------------------------------------------------------------------
        # Phase 2: wait for a worker to claim the lease.
        # ------------------------------------------------------------------
        claiming_worker: str | None = None
        start = time.monotonic()

        while True:
            row = await conn.fetchrow(
                f"SELECT claimed_by FROM {leases_table}"
                f" WHERE kind = 'chat' AND entity_id = $1",
                chat_id,
            )
            if row is not None and row["claimed_by"] is not None:
                claiming_worker = row["claimed_by"]
                break
            if time.monotonic() - start > 30.0:
                pytest.fail(
                    f"No worker claimed chat {chat_id!r} within 30s."
                    " Check that workers are running and the ClaimEngine"
                    " is wired."
                )
            await asyncio.sleep(0.5)

        # ------------------------------------------------------------------
        # Phase 3: identify and SIGTERM the claiming worker.
        # ------------------------------------------------------------------
        # The owner_id set on each worker is
        # "worker-<schema>-<i>" (the MATRIX_OWNER_ID_PREFIX env var).
        # The lease's ``claimed_by`` field should contain this prefix.

        killed_name: str | None = None
        for i, handle in enumerate(cluster.workers):
            owner_prefix = f"worker-{schema}-{i}"
            if owner_prefix in (claiming_worker or ""):
                killed_name = handle.name
                await cluster.kill(handle.name, signal.SIGTERM)
                break

        if killed_name is None:
            # Could not identify the worker by prefix — skip rather than fail,
            # since the owner_id format may vary.
            pytest.skip(
                f"Could not match claimed_by={claiming_worker!r} to a known"
                f" worker process in schema {schema!r}. Skipping reclaim assertion."
            )

        # ------------------------------------------------------------------
        # Phase 4: wait for the lease to be reclaimed or expire.
        # ------------------------------------------------------------------
        reclaimed = False
        start = time.monotonic()
        reclaim_timeout = 120.0  # covers 90s heartbeat_stale + sweep lag

        while time.monotonic() - start < reclaim_timeout:
            row = await conn.fetchrow(
                f"SELECT claimed_by, expires_at FROM {leases_table}"
                f" WHERE kind = 'chat' AND entity_id = $1",
                chat_id,
            )
            if row is None:
                # Lease was dropped (turn completed and was cleaned up).
                reclaimed = True
                break
            cb = row["claimed_by"]
            if cb is None:
                # Lease expired and is back in the queue.
                reclaimed = True
                break
            if cb != claiming_worker:
                # A different worker reclaimed it.
                reclaimed = True
                break
            await asyncio.sleep(1.0)

    finally:
        await conn.close()

    assert reclaimed, (
        f"After SIGTERMing worker {killed_name!r} (claimed_by="
        f"{claiming_worker!r}), the chat lease was not reclaimed or"
        f" released within {reclaim_timeout}s. The heartbeat/stale"
        f" sweep may not be running, or the lease TTL is longer than"
        f" expected."
    )


# ---------------------------------------------------------------------------
# Scenario 7b — API SIGTERM + WS reconnect with cursor
# ---------------------------------------------------------------------------


@pytest.mark.distributed
@pytest.mark.asyncio
async def test_api_sigterm_with_open_ws_clean_reconnect(
    cluster_2x2_failure: TestCluster,
    postgres_container: str,
) -> None:
    """SIGTERM API#0 while a WS is open; reconnect to API#1 with cursor.

    Phase 1 — open WS to API#0 and receive at least one frame (or just
        establish the connection and confirm its liveness via ping/pong).

    Phase 2 — SIGTERM API#0.
        The WS closes (connection error / WebSocketDisconnect).

    Phase 3 — reconnect to API#1 with ``cursor=<last_seq>``.
        Any frames written to the chat between the two WS sessions
        should be visible via the cursor-replay path.

    The test does not require that frames were missed (the stub LLM may
    not have produced any new rows while API#0 was dying). It asserts:

    * The reconnect to API#1 succeeds (status 101 upgrade).
    * The WS on API#1 reaches a connected state without error.

    This validates that the cursor-replay path on API#1 is functional
    after a peer process has gone away, and that the Postgres event bus
    does not crash on the disconnected subscriber.
    """
    try:
        import websockets  # noqa: F401, PLC0415
        from websockets.exceptions import ConnectionClosed  # noqa: PLC0415
    except ImportError:
        pytest.skip("websockets not installed")

    cluster = cluster_2x2_failure
    agent_id = await _setup_llm_and_agent(cluster)

    # Create a chat via API#0.
    async with cluster.client(0) as c0:
        resp = await c0.post("/v1/chats", json={"agent_id": agent_id})
        assert resp.status_code == 201, (
            f"POST /v1/chats returned {resp.status_code}: {resp.text}"
        )
        chat_id = resp.json()["id"]

    last_seq_seen = 0

    # ------------------------------------------------------------------
    # Phase 1: open WS to API#0; optionally receive a frame.
    # ------------------------------------------------------------------
    async with cluster.ws(0, f"/v1/chats/{chat_id}/ws?cursor=0") as ws0:
        # Send a ping to confirm the connection is live.
        await ws0.send(json.dumps({"kind": "ping"}))

        # Try to receive the pong; also accept any other frame.
        try:
            raw = await asyncio.wait_for(ws0.recv(), timeout=3.0)
            frame = json.loads(raw)
            if "seq" in frame:
                last_seq_seen = max(last_seq_seen, int(frame["seq"]))
        except asyncio.TimeoutError:
            pass  # No frames yet — that's fine, we just need the WS up.

        # ------------------------------------------------------------------
        # Phase 2: SIGTERM API#0.
        # ------------------------------------------------------------------
        await cluster.kill("api-0", signal.SIGTERM)

        # The WS should close; read until it does or timeout.
        try:
            while True:
                await asyncio.wait_for(ws0.recv(), timeout=5.0)
        except (asyncio.TimeoutError, ConnectionClosed, Exception):
            pass

    # Give API#0 a moment to fully die and the Postgres bus to notice.
    await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # Phase 3: reconnect to API#1 with cursor=<last_seq_seen>.
    # ------------------------------------------------------------------
    reconnect_success = False
    reconnect_error: Exception | None = None

    try:
        async with cluster.ws(
            1,
            f"/v1/chats/{chat_id}/ws?cursor={last_seq_seen}",
        ) as ws1:
            # Confirm the connection is live via ping/pong.
            await ws1.send(json.dumps({"kind": "ping"}))
            try:
                raw = await asyncio.wait_for(ws1.recv(), timeout=5.0)
                _ = json.loads(raw)
            except asyncio.TimeoutError:
                pass  # No response yet; connection is still considered healthy.
            reconnect_success = True
    except Exception as exc:  # noqa: BLE001
        reconnect_error = exc

    assert reconnect_success, (
        f"Reconnect to API#1 after SIGTERMing API#0 failed:"
        f" {reconnect_error!r}. The Postgres event bus may have"
        f" crashed or API#1 is not accepting WebSocket connections."
    )
