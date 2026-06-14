"""E2E tests: WatcherManager + cancel-on-resumable + multi-clause predicate + workers.

Covers backlog items (all new in this iteration):

* T0800 — WatcherManager fires on a file change in the workspace.
  Inject a parked-on-watch_files row → touch a file in the workspace
  root → watcher polls mtime → publishes {"changes":[...]} on the
  bus → listener flips row to resumable. End-to-end M4 pin.
* T0801 — POST /v1/sessions/{id}/yields/{tcid}/cancel on a session
  whose parked_status is ALREADY 'resumable' returns 202 cleanly
  (idempotent — the publish is a no-op but the endpoint accepts).
* T0802 — POST /v1/sessions/find with a multi-clause AND predicate
  (workspace_id + status) returns 200 with the correct filtered
  rows; no /errors/internal under predicate composition.
* T0803 — GET /v1/workers returns the registered worker the worker
  pool created at bringup time.
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
            "id": agent_id, "description": "watcher+predicate probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201


async def _seed_workspace(
    client: httpx.AsyncClient, wp_id: str, tpl_id: str, tmp_path,
) -> tuple[str, Path]:
    """Return (workspace_id, workspace_root_path).

    The local backend uses tmp_path as the provider root; the
    workspace's on-disk root is tmp_path/<workspace_id>/. We return
    tmp_path so callers can construct full paths to write files into
    the workspace.
    """
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
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
    wid = r.json()["id"]
    # Workspace materialise happens lazily. Touch a file via the API
    # to force materialise — that gives us a known root + lets us
    # write follow-up files via the API too.
    return wid, tmp_path / wid


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


async def _wait_for_worker_idle(conn: asyncpg.Connection, session_id: str) -> None:
    """Poll until the worker has finished processing the session's initial turn.

    A freshly created session (auto_start=False) is still enqueued in the
    claim engine and the live worker claims + runs it immediately (completes
    in one turn because there are no instructions). If we inject the park
    before the worker's on_release UPDATE lands, the UPDATE overwrites our
    injection (it replaces the entire data JSONB, clearing any fields we set).
    We poll until turn_no >= 1, which confirms on_release has already written
    its final row and no further writes are pending.
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
    resume_metadata: dict[str, Any] | None = None,
    parked_status: str = "parked",
) -> None:
    """Inject park state. Accepts an explicit parked_status so callers
    can set up 'resumable' rows directly for race tests.

    Always waits for the worker's initial-turn on_release to complete
    before writing. The on_release UPDATE replaces the entire data JSONB,
    so any injection written before it lands is silently wiped. This
    applies to both 'parked' and 'resumable' injections. The parked_state
    blob matches ParkedState.to_jsonable() exactly, including the top-level
    tool_call_id and the event_keys/graph_checkpoint fields.
    """
    now = datetime.now(timezone.utc)
    parked_until = now + timedelta(seconds=600)
    rm = dict(resume_metadata or {})
    rm.setdefault("tool_call_id", tool_call_id)
    parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": tool_name,
            "event_key": event_key,
            "timeout": 600.0,
            "resume_metadata": rm,
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
        SET data = jsonb_set(jsonb_set(jsonb_set(jsonb_set(jsonb_set(jsonb_set(data,
                     '{parked_status}', to_jsonb($6::text)),
                   '{parked_event_key}', to_jsonb($2::text)),
                 '{parked_event_keys}', 'null'::jsonb),
               '{parked_until}', to_jsonb($3::text)),
             '{parked_at}', to_jsonb($4::text)),
           '{parked_state}', $5::jsonb),
            updated_at = now()
        WHERE id = $1
    """
    conn = await _pg()
    try:
        # Always wait for the worker to finish the initial turn before
        # injecting. The on_release UPDATE replaces the full data JSONB,
        # so any injection written before on_release is overwritten.
        # This applies to 'resumable' injections too (T0801 creates a
        # fresh session with auto_start=False and injects 'resumable'
        # directly — the worker must have settled first).
        await _wait_for_worker_idle(conn, session_id)
        await conn.execute(
            sql, session_id, event_key,
            parked_until.isoformat(), now.isoformat(),
            json.dumps(parked_state), parked_status,
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


async def _poll_for_resumable(
    conn: asyncpg.Connection,
    session_id: str,
    *,
    timeout: float = 5.0,
) -> dict:
    """Poll the DB in a tight loop for parked_status='resumable'.

    The listener flips the row to 'resumable' then mark_resumable() wakes
    the live worker which re-claims and clears the park fields within ~1-20 ms.
    Coarse polling (100-200 ms intervals) reliably misses that window.

    This function queries in a tight loop (no sleep) over a pre-warmed
    connection so each round-trip is ~1 ms, small enough to catch the
    transient 'resumable' snapshot before the worker clears it.

    Returns the first snapshot with parked_status='resumable', or the
    last snapshot seen before the timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        row = await conn.fetchrow(
            "SELECT data->>'parked_status' AS parked_status, "
            "data->'parked_state'->'resume_event_payload' AS payload "
            "FROM sessions WHERE id = $1",
            session_id,
        )
        if row is None:
            break
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        last = {
            "parked_status": row["parked_status"],
            "resume_event_payload": payload,
        }
        if last["parked_status"] == "resumable":
            return last
    return last


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0800 — WatcherManager fires on file change → row flips to resumable
# ===========================================================================


@pytest.mark.asyncio
async def test_t0800_watcher_fires_on_file_change_and_flips_row(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0800 — End-to-end M4 pin. Inject a session parked on
    watch_files with a single path. Touch the file via API/PUT.
    WatcherManager (running in the primer lifespan) polls mtimes
    every 500ms; on detecting the change it publishes
    {"changes":[...]} on the bus. Listener flips row → resumable.

    Verifies the M4 wake path with no LLM driver: watch park →
    file touch → watcher tick → bus publish → listener flip.
    """
    pid = f"llm-w800-{unique_suffix}"
    aid = f"ag-w800-{unique_suffix}"
    wp_id = f"wp-w800-{unique_suffix}"
    tpl_id = f"tpl-w800-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid, ws_root = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    sid = await _seed_session(client, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    tcid = f"tc-watch-{unique_suffix}"
    target_path = "watched.txt"
    try:
        # Seed the watched file via API so the workspace root exists.
        # The local backend materialises the workspace dir on first PUT.
        r = await client.put(
            f"/v1/workspaces/{wid}/files?path={target_path}",
            json={"content": "initial", "encoding": "text"},
        )
        assert r.status_code in (200, 201, 204), r.text

        # Sister write uses the same body shape — re-using here for
        # the post-park file mutation.
        async def _put_file(content: str) -> None:
            r2 = await client.put(
                f"/v1/workspaces/{wid}/files?path={target_path}",
                json={"content": content, "encoding": "text"},
            )
            assert r2.status_code in (200, 201, 204), r2.text

        # Inject the watch_files park.
        await _inject_park(
            sid,
            tool_name="watch_files",
            tool_call_id=tcid,
            event_key=f"watch:{sid}:{tcid}",
            resume_metadata={
                "paths": [target_path],
                "batch_window_ms": 100,
                "workspace_id": wid,
                "tool_call_id": tcid,
                "registered_at_iso": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Allow the WatcherManager (2s scan cadence) one tick to
        # notice the new park and start a LocalWorkspaceWatcher for
        # it. Then mutate the file to trigger a change.
        # Open the poll connection before the touch so it is warm and
        # ready for the tight-poll loop that follows.
        conn = await _pg()
        try:
            await asyncio.sleep(3.0)
            await _put_file("changed content")

            # Poll for resumable. The watcher polls mtimes every 500ms,
            # then waits batch_window_ms (100ms here) for more changes,
            # then publishes. Listener flips. Budget 15s to absorb cold
            # paths.
            # After the listener flips the row to 'resumable', the live
            # worker receives pg_notify and clears the park within ~5-20 ms.
            # Coarse 200 ms polling reliably misses the window. Use a
            # tight loop (no sleep) over the pre-warmed connection.
            fields = await _poll_for_resumable(conn, sid, timeout=15.0)
        finally:
            await conn.close()

        assert fields.get("parked_status") == "resumable", (
            f"watch park never flipped after file change; final={fields}"
        )
        # Payload carries the changes list.
        payload = fields.get("resume_event_payload") or {}
        assert "changes" in payload, (
            f"expected 'changes' in payload, got {payload}"
        )
        changes = payload["changes"]
        assert any(c.get("path") == target_path for c in changes), (
            f"expected change for {target_path!r}, got {changes}"
        )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0801 — Cancel-yielded-tool on a resumable session returns 202
# ===========================================================================


@pytest.mark.asyncio
async def test_t0801_cancel_yielded_tool_on_resumable_session_returns_202(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0801 — The cancel-yielded-tool endpoint accepts both
    parked AND resumable rows (a row that's already been published
    against but not yet resumed by a worker is still cancellable).
    POSTing cancel on a resumable row returns 202; the publish is
    a no-op via mark_resumable idempotency.
    """
    pid = f"llm-c801-{unique_suffix}"
    aid = f"ag-c801-{unique_suffix}"
    wp_id = f"wp-c801-{unique_suffix}"
    tpl_id = f"tpl-c801-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid, _ = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    sid = await _seed_session(client, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    tcid = f"tc-r801-{unique_suffix}"
    # Open the poll connection before inject so it is warm and ready.
    conn = await _pg()
    try:
        # Inject as resumable directly (skip the parked → resumable
        # flip). Raw SQL does NOT call mark_resumable, so the worker
        # is not notified yet — the session sits in 'resumable' state
        # without a lease entry until something calls mark_resumable.
        await _inject_park(
            sid,
            tool_name="ask_user",
            tool_call_id=tcid,
            event_key=f"ask_user:{sid}:{tcid}",
            resume_metadata={
                "prompt": "?", "tool_call_id": tcid,
            },
            parked_status="resumable",
        )

        # Cancel returns 202. The handler publishes to the bus and
        # returns before the listener has processed the event. The
        # listener will eventually call mark_resumable, which creates
        # a lease + pg_notify, waking the worker. The worker then
        # claims and clears the park within ~5-20 ms after notify.
        # We poll in a tight loop immediately after the 202 to catch
        # the 'resumable' state before the worker clears it.
        r = await client.post(
            f"/v1/sessions/{sid}/yields/{tcid}/cancel",
            json={"reason": "post-resumable cancel"},
        )
        assert r.status_code == 202, r.text
        # Row stays resumable until the worker claims it (no double-flip).
        # Tight-poll to catch the state before the worker clears it.
        fields = await _poll_for_resumable(conn, sid, timeout=5.0)
        assert fields["parked_status"] == "resumable", (
            f"expected resumable, got: {fields}"
        )
    finally:
        await conn.close()
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0802 — Multi-clause AND predicate on /v1/sessions/find
# ===========================================================================


@pytest.mark.asyncio
async def test_t0802_sessions_find_multi_clause_and_predicate(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0802 — POST /v1/sessions/find with a binary AND predicate
    (workspace_id == X AND status == 'ended') returns the rows
    matching BOTH clauses; rows matching only one (different
    workspace or different status) are filtered out.

    Sessions created with auto_start=False are still processed by the
    live worker immediately, so they reach 'ended' quickly. The test
    waits for both sessions to settle before issuing the find query.

    Pins the predicate composition path in primer/api/routers/sessions
    and the backend's AND handling.
    """
    pid = f"llm-p802-{unique_suffix}"
    aid = f"ag-p802-{unique_suffix}"
    wp_id = f"wp-p802-{unique_suffix}"
    tpl_id = f"tpl-p802-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid_a, _ = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    # Create a second workspace too — gives us a session NOT on wid_a.
    wp_id_b = f"wp-p802b-{unique_suffix}"
    tpl_id_b = f"tpl-p802b-{unique_suffix}"
    wid_b, _ = await _seed_workspace(
        client, wp_id_b, tpl_id_b, tmp_path / "b",
    )
    sid_on_a = await _seed_session(client, wid_a, aid)
    sid_on_b = await _seed_session(client, wid_b, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid_a}/sessions/{sid_on_a}/cancel",
        f"/v1/workspaces/{wid_b}/sessions/{sid_on_b}/cancel",
        f"/v1/workspaces/{wid_a}",
        f"/v1/workspaces/{wid_b}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_templates/{tpl_id_b}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/workspace_providers/{wp_id_b}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    # Open a direct DB connection to wait for sessions to settle.
    pg = await _pg()
    try:
        # Sessions created with auto_start=False are still claimed and
        # processed by the live worker (the claim engine enqueues them
        # regardless of auto_start). The worker completes quickly since
        # there are no LLM instructions. Wait for both to reach 'ended'
        # before issuing the find query, so the status filter is stable.
        for sid in (sid_on_a, sid_on_b):
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                row = await pg.fetchrow(
                    "SELECT data->>'status' AS st FROM sessions WHERE id = $1",
                    sid,
                )
                if row and row["st"] == "ended":
                    break
                await asyncio.sleep(0.05)
    finally:
        await pg.close()

    try:
        # AND predicate: workspace_id == wid_a AND status == 'ended'.
        # Sessions here have no LLM instructions so they end immediately;
        # filtering on 'ended' is stable once the worker has settled.
        body = {
            "predicate": {
                "kind": "predicate",
                "left": {
                    "kind": "predicate",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "op": "=",
                    "right": {"kind": "value", "value": wid_a},
                },
                "op": "and",
                "right": {
                    "kind": "predicate",
                    "left": {"kind": "field", "name": "status"},
                    "op": "=",
                    "right": {"kind": "value", "value": "ended"},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 100},
        }
        r = await client.post("/v1/sessions/find", json=body)
        assert r.status_code == 200, r.text
        items = r.json().get("items", [])
        ids = {it["id"] for it in items}
        # sid_on_a IS in the result; sid_on_b is NOT (wrong workspace).
        assert sid_on_a in ids, (
            f"expected sid_on_a {sid_on_a!r} in result, got {ids}"
        )
        assert sid_on_b not in ids, (
            f"sid_on_b {sid_on_b!r} leaked into wid_a-filtered result"
        )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0803 — GET /v1/workers returns the registered worker
# ===========================================================================


@pytest.mark.asyncio
async def test_t0803_workers_list_returns_registered_worker(
    client: httpx.AsyncClient,
) -> None:
    """T0803 — The bringup launches primer with --run-worker, which
    registers one worker into the scheduler. GET /v1/workers must
    return at least that one worker with a non-empty id; envelope
    is the standard {"items": [...]} shape.
    """
    r = await client.get("/v1/workers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body, body
    items = body["items"]
    assert len(items) >= 1, f"expected ≥1 worker, got {items}"
    # Each item has a non-empty id.
    for w in items:
        assert w.get("id"), f"worker missing id: {w}"
        # Status is one of the documented values.
        assert w.get("status") in ("active", "draining", "dead"), (
            f"unexpected worker status: {w}"
        )
