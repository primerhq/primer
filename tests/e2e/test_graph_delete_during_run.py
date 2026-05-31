"""E2E test: DELETE Graph mid-execute of a graph-bound RUNNING session.

Backlog item:

* T0737 — Create a graph-bound session with ``auto_start=true`` so the
  worker pool dispatches a ``WorkspaceGraphExecutor`` immediately;
  race a ``DELETE /v1/graphs/{gid}`` against that dispatch. The
  contract: ``DELETE`` returns a clean 204 (or, in the unlikely event
  the worker already removed it, 404); the session converges to a
  terminal status (``ended`` / ``failed`` / ``cancelled``) within
  ~20s; ``last_error.type`` (if present) never references
  ``/errors/internal``.

  Pins the documented "DELETE-during-execute race" — a destructive
  signal must not leak ``/errors/internal`` even when the executor is
  mid-flight against the same row that was just removed.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest


# ---------------------------------------------------------------------------
# Seed helpers (mirror the patterns in test_more_yields_and_graph.py)
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
            "description": "graph-delete-race probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent failed: {r.text}"


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0737 — DELETE Graph during graph-bound RUNNING session
# ===========================================================================


@pytest.mark.asyncio
async def test_t0737_delete_graph_during_running_graph_session(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0737 — Race a ``DELETE /v1/graphs/{gid}`` against worker-pool
    dispatch of a graph-bound session with ``auto_start=true``. The
    contract guards the documented destructive-signal-during-execute
    invariant: clean envelopes throughout, even when the executor is
    mid-flight against the row that was just removed.

    Acceptable terminal states (the dispatch order against the
    placeholder LLM and the worker tick cadence determines which):

    * ``ended`` — graph executor completed before delete reached it
      (rare with a 9999-port placeholder LLM but possible if the
      first agent turn failed fast enough to converge).
    * ``failed`` — graph load found the row missing, surfaced via
      ``_handle_fatal``.
    * ``cancelled`` — explicit cancel never issued by this test, so
      not expected but tolerated to avoid brittleness.

    Hard assertions:

    * ``DELETE /v1/graphs/{gid}`` returns 204 (success). A 404 is
      also clean (the executor's load path removed it first — still
      a documented envelope, not a 5xx leak).
    * Session reaches a terminal status within 20s.
    * If ``last_error`` is populated, its ``type`` MUST NOT contain
      the substring ``internal`` (no ``/errors/internal`` envelope
      leak under the race).
    """
    pid = f"llm-t737-{unique_suffix}"
    aid = f"ag-t737-{unique_suffix}"
    gid = f"gr-t737-{unique_suffix}"
    wp_id = f"wp-t737-{unique_suffix}"
    tpl_id = f"tpl-t737-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)

    # Local workspace so the executor at least gets to dispatch.
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
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
    wid = r.json()["id"]

    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        # graph cleanup is idempotent; main test path deletes it,
        # but listing it here tolerates earlier-stage failures.
        f"/v1/graphs/{gid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    sid: str | None = None

    try:
        # Seed a valid graph (agent → terminal). The executor will
        # actually try to drive the agent turn; placeholder LLM at
        # 127.0.0.1:9999 (unreachable) makes the agent fail fast.
        r = await client.post(
            "/v1/graphs",
            json={
                "id": gid,
                "description": "delete-during-run probe",
                "entry_node_id": "begin",
                "nodes": [
                    {"id": "begin", "kind": "begin"},
                    {"id": "n1", "kind": "agent", "agent_id": aid},
                    {"id": "end", "kind": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert r.status_code == 201, r.text

        # auto_start=True so the worker pool picks it up immediately
        # (claim cadence ~2s). We want the DELETE to land WHILE the
        # dispatch is in flight.
        r = await client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": gid},
                "auto_start": True,
            },
        )
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        cleanup_urls.insert(
            0, f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        )

        # Brief settle (≤2s) so the worker has a chance to pick up
        # the row — the race window is meaningful only if the
        # executor is mid-flight when DELETE lands. Cap at one
        # claim-poll interval; if dispatch hasn't happened yet, the
        # test still pins the DELETE-then-dispatch ordering (graph
        # gone before worker touches it).
        await asyncio.sleep(1.5)

        # DELETE the graph. Acceptable: 204 (normal) or 404 (the
        # worker's load path removed the row first, e.g. via a
        # cascading cleanup).
        r = await client.delete(f"/v1/graphs/{gid}")
        assert r.status_code in (204, 404), (
            f"DELETE /v1/graphs/{gid} returned unexpected "
            f"{r.status_code}: {r.text}"
        )

        # Poll the session top-level GET until it reaches a terminal
        # status, OR 20s elapses. The placeholder LLM + missing
        # graph row should converge well within budget.
        deadline = time.monotonic() + 20.0
        terminal_seen = False
        body: dict = {}
        last_status: str | None = None
        while time.monotonic() < deadline:
            gr = await client.get(f"/v1/sessions/{sid}")
            # Envelope must be 2xx — even mid-race the read path
            # must not 5xx.
            assert gr.status_code == 200, (
                f"GET /v1/sessions/{sid} returned {gr.status_code}: {gr.text}"
            )
            body = gr.json()
            last_status = body.get("status")
            if last_status in ("ended", "failed", "cancelled"):
                terminal_seen = True
                break
            await asyncio.sleep(0.5)

        assert terminal_seen, (
            f"graph-bound session {sid!r} did not reach terminal "
            f"within 20s after DELETE; last status={last_status!r}"
        )

        # The session's last_error (if populated) must not be the
        # internal-error envelope — that would mean a 5xx-class
        # exception leaked into the row's failure record.
        last_error = body.get("last_error")
        if last_error:
            err_type = last_error.get("type", "") or ""
            assert "internal" not in err_type, (
                f"last_error.type contains 'internal': "
                f"{last_error!r} — DELETE-during-execute leaked a 500."
            )

        # Sanity: the graph row is gone via the API.
        r = await client.get(f"/v1/graphs/{gid}")
        assert r.status_code == 404, (
            f"graph {gid!r} still GET-able after DELETE: {r.status_code}"
        )
    finally:
        await _cleanup(client, cleanup_urls)
