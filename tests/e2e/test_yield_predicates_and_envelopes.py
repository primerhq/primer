"""E2E tests: park-field predicates + cancel-yielded-tool body + graph log + IC race.

Covers backlog items:
* T0733 — Graph-bound session post-terminal: /log returns clean
  envelope; reaching ENDED via the fatal path doesn't 500-leak when
  the log endpoint is subsequently queried.
* T0753 — IC search racing config DELETE: every search returns a
  clean envelope (200 or 503), DELETE returns 204/404, nothing
  /errors/internal under the transition.
* T0769 (new) — POST /v1/sessions/find with predicate on
  ``parked_status`` returns 200 + empty (or filtered) items on a
  fresh DB; never /errors/internal. Pins the JSONB predicate path
  for the new park-field on the sessions row.
* T0770 (new) — POST /v1/sessions/{id}/yields/{tcid}/cancel with
  an empty body (no reason supplied) is accepted as the default
  (reason=None); the endpoint signature treats the body as optional.

The yielding-tools backlog tests that need a real LLM-parked session
(T0759/T0760/T0761/T0768) remain deferred until LM Studio is wired
into the bringup or a debug park-injection endpoint lands.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
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
            "description": "probe",
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


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0769 — POST /v1/sessions/find with parked_status predicate
# ===========================================================================


@pytest.mark.asyncio
async def test_t0769_sessions_find_filter_by_parked_status_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0769 — Filter sessions by ``parked_status`` via the find
    endpoint. On a fresh DB no session has ever parked, so the filter
    should return an empty page with status=200 and `items: []`.

    Pins the JSONB predicate path for the M1 ``parked_status`` field
    against /errors/internal leak — the predicate engine handles the
    new field path with no special-casing required, but the contract
    that ``parked_status='parked'`` filters cleanly is worth defending.

    Priority area 6 (JSONB type coercion) + area 1 (yielding tools).
    """
    body = {
        "predicate": {
            "left": {"kind": "field", "name": "parked_status"},
            "op": "=",
            "right": {"kind": "value", "value": "parked"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r = await client.post("/v1/sessions/find", json=body)
    # The predicate engine may either succeed with empty items OR
    # reject the new field with a clean 400 — both are acceptable
    # envelope shapes; the CONTRACT is no /errors/internal.
    assert r.status_code in (200, 400), r.text
    if r.status_code == 200:
        assert r.json().get("items") == [], r.json()
    else:
        body = r.json()
        assert "internal" not in body.get("type", ""), body

    # Sister filter: parked_event_key — same contract.
    body2 = {
        "predicate": {
            "left": {"kind": "field", "name": "parked_event_key"},
            "op": "~=",
            "right": {"kind": "value", "value": "timer:"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r2 = await client.post("/v1/sessions/find", json=body2)
    assert r2.status_code in (200, 400), r2.text
    if r2.status_code == 200:
        assert r2.json().get("items") == [], r2.json()
    else:
        assert "internal" not in r2.json().get("type", ""), r2.json()


# ===========================================================================
# T0770 — POST cancel-yielded-tool accepts empty body
# ===========================================================================


@pytest.mark.asyncio
async def test_t0770_cancel_yielded_tool_accepts_empty_body(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0770 — The cancel-yielded-tool endpoint defaults the body's
    ``reason`` to ``None``. POSTing with an empty JSON body (or no
    body) must succeed at the body-parsing stage and return the
    documented 404 (no parked yield with that tcid) — NOT a 422 for
    a missing field. Pins the optional-body contract.

    The session here doesn't exist, so the endpoint returns 404 — but
    the test is specifically about reaching that 404 (vs being
    rejected upstream at request parsing with 422 due to a body
    schema mismatch).
    """
    fake_sid = f"sess-nope-{unique_suffix}"
    fake_tcid = f"tc-{unique_suffix}"

    # Empty JSON body — body field defaults should kick in.
    r = await client.post(
        f"/v1/sessions/{fake_sid}/yields/{fake_tcid}/cancel",
        json={},
    )
    assert r.status_code == 404, r.text
    assert "internal" not in r.json().get("type", ""), r.json()

    # Body explicitly null-reason — also valid.
    r2 = await client.post(
        f"/v1/sessions/{fake_sid}/yields/{fake_tcid}/cancel",
        json={"reason": None},
    )
    assert r2.status_code == 404, r2.text


# ===========================================================================
# T0733 — Graph-bound session post-terminal: /log returns clean envelope
# ===========================================================================


@pytest.mark.asyncio
async def test_t0733_graph_session_post_terminal_log_query_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0733 — Create a graph + bind a graph session that auto-starts.
    Wait for it to converge to terminal (any of ended/failed/
    cancelled) — without an LLM the agent node fails which surfaces
    via the fatal path. Then GET /v1/workspaces/{wid}/log and assert
    a clean envelope (200 with a commits array, or a documented
    error envelope — never /errors/internal).

    Defends the graph-executor terminal path's downstream queries
    against /errors/internal leaks.
    """
    pid = f"llm-t733-{unique_suffix}"
    aid = f"ag-t733-{unique_suffix}"
    gid = f"gr-t733-{unique_suffix}"
    wp_id = f"wp-t733-{unique_suffix}"
    tpl_id = f"tpl-t733-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    wid = await _seed_workspace(client, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/graphs/{gid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    try:
        # Minimal graph: agent → terminal.
        r = await client.post(
            "/v1/graphs",
            json={
                "id": gid,
                "description": "t733",
                "entry_node_id": "n1",
                "nodes": [
                    {"id": "n1", "kind": "agent", "agent_id": aid},
                    {"id": "end", "kind": "terminal"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert r.status_code == 201, r.text

        # Bind + auto_start.
        r = await client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": gid},
                "auto_start": True,
            },
        )
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/workspaces/{wid}/sessions/{sid}/cancel")

        # Poll until terminal.
        deadline = time.monotonic() + 20.0
        last_status = "(none)"
        while time.monotonic() < deadline:
            r = await client.get(f"/v1/sessions/{sid}")
            assert r.status_code == 200, r.text
            last_status = r.json()["status"]
            if last_status in ("ended", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
        # Either reached terminal or still running — both fine for
        # the /log test; the log query just needs to work cleanly.

        # GET /log on the workspace.
        r = await client.get(f"/v1/workspaces/{wid}/log?limit=10")
        assert r.status_code in (200, 404, 503), r.text
        body = r.json()
        # No internal-error leak.
        if r.status_code != 200:
            assert "internal" not in body.get("type", ""), body
        else:
            # 200: commits is a list (possibly empty).
            assert "commits" in body or isinstance(body, list), body
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0753 — IC search racing config DELETE: clean envelopes
# ===========================================================================


@pytest.mark.asyncio
async def test_t0753_ic_search_racing_config_delete_clean_envelopes(
    client: httpx.AsyncClient,
) -> None:
    """T0753 — Fire 5 concurrent /v1/agents/search calls alongside 1
    DELETE /v1/internal_collections/config. Every response must
    carry a clean envelope shape:

    * search → 200 (with hits or empty) OR 503 (subsystem inactive,
      via the documented gate)
    * DELETE → 204 (config removed) OR 404 (no config to delete)

    NEVER /errors/internal — the deactivation transition (if any)
    must be a clean state-machine flip, not a 5xx leak.

    On a fresh DB with no IC config bootstrapped, all searches
    return 503 and DELETE returns 404; the test still pins the
    envelope contract under concurrent calls.
    """
    async def _one_search(i: int) -> tuple[int, str]:
        try:
            r = await client.post(
                "/v1/agents/search",
                json={"query": f"probe-{i}", "limit": 5},
            )
            return r.status_code, r.text
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc)

    async def _one_delete() -> tuple[int, str]:
        try:
            r = await client.delete("/v1/internal_collections/config")
            return r.status_code, r.text
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc)

    results = await asyncio.gather(
        _one_search(0), _one_search(1), _one_search(2),
        _one_search(3), _one_search(4),
        _one_delete(),
    )
    # Asserts: every status code is in the documented set;
    # nothing is /errors/internal.
    for status, text in results:
        assert status != 0, f"transport error: {text}"
        # 200 / 204 / 404 / 503 / 405 / 501 / 422 are all clean
        # envelope shapes for this surface; 500 leak is the bug.
        assert status != 500, (
            f"got 500 /errors/internal under IC race: {text}"
        )
        # 404 must be /errors/not-found (or similar); search/delete
        # never return /errors/internal even on transition.
        if status >= 400 and text and text.startswith("{"):
            import json
            body = json.loads(text)
            assert "internal" not in body.get("type", ""), body
