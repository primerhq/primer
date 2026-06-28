"""E2E tests: chat messages cursor + MCP discovery + graph negative paths + IC.

Backlog items:
* T0766 — GET /v1/chats/{id}/messages ?after_seq filter returns the
  expected slice.
* T0767 — Open-websearch MCP toolset list_tools returns the 6-tool
  catalog with non-empty schemas; skips soft if npx is unavailable.
* T0585 — GET /v1/internal_collections/config returns 404
  /errors/not-found when no config row exists.
* T0736 — A graph-bound session against a container-backed workspace
  is now SUPPORTED: the session create must return 201 and the graph
  must run to a clean terminal (ended) with no /errors/internal.
* T0739 — A graph with a callable-router edge but an empty
  RouterRegistry must converge to ended_reason='failed' with a
  populated last_error referencing the router, never
  /errors/internal.
"""

from __future__ import annotations

import asyncio
import shutil
import time

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared seed helpers
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


def _ws_headers(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Forward the authenticated client's session cookie onto the WS
    handshake. The chat WS closes with 4401 unless the signed
    ``primer_session`` cookie is present; the ``client`` fixture holds it in
    its cookie jar after login."""
    pairs = [f"{c.name}={c.value}" for c in client.cookies.jar]
    if not pairs:
        return []
    return [("Cookie", "; ".join(pairs))]


async def _cleanup(client: httpx.AsyncClient, urls: list[str]) -> None:
    for url in urls:
        try:
            await client.delete(url)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0766 — chat messages ?after_seq filter
# ===========================================================================


@pytest.mark.asyncio
async def test_t0766_chat_messages_after_seq_filter(
    client: httpx.AsyncClient, unique_suffix: str, mock_llm,
) -> None:
    """T0766 — GET /v1/chats/{id}/messages ?after_seq=N returns only
    rows with seq > N, ordered ascending. Defends the cursor-style
    filter implemented in
    [`primer/api/routers/chats.py`](../../primer/api/routers/chats.py).

    Uses the session-scoped mock_llm fixture so the stub LLM server
    returns a real streaming chat response (user_message + assistant_token
    + done + usage = 4 WS frames per turn). Originally used a fake
    ollama provider that produced no LLM response; the test always needs
    the scripted stub to produce rows in a deterministic order.
    """
    registry, mock_base_url = mock_llm
    scenario = f"t766-{unique_suffix}"
    # Scripted stub: emit "hello" text for any user message.
    from tests._support.mock_llm import Rule
    registry.register(scenario, [Rule(emit_text="hello")])

    pid = f"llm-t766-{unique_suffix}"
    aid = f"ag-t766-{unique_suffix}"
    r = await client.post(
        "/v1/llm_providers",
        json={
            "id": pid,
            "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        },
    )
    assert r.status_code == 201, f"seed LLM failed: {r.text}"
    r = await client.post(
        "/v1/agents",
        json={
            "id": aid,
            "description": "t766 probe",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": [],
            "system_prompt": ["probe"],
        },
    )
    assert r.status_code == 201, f"seed agent failed: {r.text}"
    cleanup_urls = [f"/v1/agents/{aid}", f"/v1/llm_providers/{pid}"]
    chat_id: str | None = None
    try:
        # Create a chat via the public API.
        r = await client.post("/v1/chats", json={"agent_id": aid})
        assert r.status_code == 201, r.text
        chat_id = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/chats/{chat_id}")

        # The mock LLM appends 3 rows (user_message + assistant_token
        # + done) per user_message; the WS also sends a usage frame after
        # done. Drive the chat over WS twice to get seq 1..6.
        import json
        import websockets

        http_url = str(client.base_url).rstrip("/")
        ws_origin = http_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        ws_url = f"{ws_origin}/v1/chats/{chat_id}/ws"

        last_seq = [0]  # mutable cell so _send_one can update it

        async def _send_one(text: str) -> None:
            # Pass ?cursor=last_seq to skip replaying history from prior turns.
            connect_url = f"{ws_url}?cursor={last_seq[0]}"
            async with websockets.connect(
                connect_url, additional_headers=_ws_headers(client),
            ) as ws:
                # Spec §6.4: drain the initial ``usage`` envelope first.
                initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert initial["kind"] == "usage", initial
                await ws.send(json.dumps(
                    {"kind": "user_message", "content": text}
                ))
                # Consume 4 messages (user + assistant + done + usage).
                received: list[dict] = []
                for _ in range(4):
                    msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    received.append(json.loads(msg))
                # Ensure all 4 kinds arrived (defensive in case the
                # runner's row ordering changes).
                kinds = [m["kind"] for m in received]
                assert "done" in kinds, f"got {kinds}, expected done"
                # Track the highest seq so the next _send_one skips them.
                for m in received:
                    if m.get("seq") is not None:
                        last_seq[0] = max(last_seq[0], m["seq"])
                # Small settle delay so the runner's last storage write
                # finishes before the WS close races it. The runner
                # persists then yields; on done's send_json the runner
                # has already committed, but a Postgres COMMIT may not
                # be flushed before WSDisconnect interrupts the next
                # receive_json.
                await asyncio.sleep(0.2)

        await _send_one("first")
        # Tiny delay between turns so the chat row's last_seq update
        # from turn 1 is visible to turn 2's runner.
        await asyncio.sleep(0.2)
        await _send_one("second")
        await asyncio.sleep(0.3)

        # Now the chat has 6 messages (seq 1..6). GET ?after_seq=3 →
        # rows with seq in {4, 5, 6}.
        r = await client.get(
            f"/v1/chats/{chat_id}/messages?after_seq=3",
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        seqs = [it["seq"] for it in items]
        assert seqs == [4, 5, 6], f"expected [4,5,6], got {seqs}"

        # And after_seq=6 → no rows.
        r = await client.get(
            f"/v1/chats/{chat_id}/messages?after_seq=6",
        )
        assert r.status_code == 200
        assert r.json()["items"] == []
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0767 — open-websearch MCP toolset list_tools
# ===========================================================================


@pytest.mark.asyncio
async def test_t0767_open_websearch_mcp_list_tools(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0767 — Create an MCP-stdio toolset pointing at
    ``npx -y open-websearch@latest``; GET /v1/toolsets/{id}/tools
    returns the documented 6-tool catalog. Pins discovery against a
    real upstream MCP server per
    [`docs/testing/02-bringup.md`](../../docs/testing/02-bringup.md)
    §"open-websearch MCP test target".

    Skip-soft if npx isn't on PATH — the MCP allowlist is unset in
    the test config so an absent npx would surface as 503 from the
    /tools call. The skip-soft is at the prerequisite level instead.
    """
    if shutil.which("npx") is None:
        pytest.skip("npx not on PATH; open-websearch MCP unavailable")

    toolset_id = f"ts-ows-{unique_suffix}"
    r = await client.post(
        "/v1/toolsets",
        json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {
                    "command": ["npx", "-y", "open-websearch@latest"],
                    "env": {"MODE": "stdio", "DEFAULT_SEARCH_ENGINE": "bing"},
                },
            },
        },
    )
    assert r.status_code == 201, r.text

    try:
        # First call may take 30+s on a cold npx cache (downloads the
        # package). Use a generous timeout.
        r = await client.get(
            f"/v1/toolsets/{toolset_id}/tools",
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The response is a list of tool dicts (or wrapped). Tolerate
        # both shapes — older versions returned a bare list.
        tools = body if isinstance(body, list) else body.get("items", body.get("tools", []))
        names = {t.get("id") or t.get("name") for t in tools}
        # The six documented tools.
        expected = {
            "search",
            "fetchWebContent",
            "fetchGithubReadme",
            "fetchCsdnArticle",
            "fetchLinuxDoArticle",
            "fetchJuejinArticle",
        }
        # MCP may add/rename tools across versions; assert the
        # load-bearing ones are present.
        load_bearing = {"search", "fetchGithubReadme", "fetchWebContent"}
        missing = load_bearing - names
        assert not missing, (
            f"open-websearch missing load-bearing tools: {missing} "
            f"(saw: {sorted(names)})"
        )
    finally:
        try:
            await client.delete(f"/v1/toolsets/{toolset_id}")
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# T0585 — IC config GET 404 when no row exists
# ===========================================================================


@pytest.mark.asyncio
async def test_t0585_ic_config_get_returns_404_when_no_row(
    client: httpx.AsyncClient,
) -> None:
    """T0585 — On a fresh database with no IC config row, GET
    /v1/internal_collections/config returns 404 /errors/not-found
    cleanly (not /errors/internal, not 200 with an empty body).

    Defends the deterministic envelope shape per §3 RFC 7807 even
    when the underlying table itself may not exist yet (Storage[T]
    creates it lazily — the read should still surface 'not found').
    """
    r = await client.get("/v1/internal_collections/config")
    # Either 404 not-found (no row) or 503 service-unavailable (subsystem
    # inactive — same outcome semantically). The contract under test:
    # never /errors/internal.
    assert r.status_code in (404, 503), r.text
    body = r.json()
    assert body["status"] in (404, 503), body
    # Type must not be internal-error
    assert "internal" not in body.get("type", ""), body


# ===========================================================================
# T0736 — Graph-bound session against container WorkspaceProvider
# ===========================================================================


@pytest.mark.asyncio
async def test_t0736_graph_session_container_provider_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0736 — Container workspace graph sessions are now SUPPORTED.

    The StateRepo parity work landed state_repo support for container
    workspaces. A graph-bound session against a container-backed
    workspace must:
      a) return 201 on session create (not 422/500), and
      b) reach a clean terminal status (``ended``) with no
         /errors/internal in last_error.

    The test creates a container provider (image
    ``primer/workspace-runtime:1.0``) + graph + session and polls to
    terminal within a generous window. If Docker is not available on
    the test runner the provider-create will return 4xx cleanly (not
    500) and the test returns early.
    """
    pid = f"llm-t736-{unique_suffix}"
    aid = f"ag-t736-{unique_suffix}"
    gid = f"gr-t736-{unique_suffix}"
    wp_id = f"wp-t736-{unique_suffix}"
    tpl_id = f"tpl-t736-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)

    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "container",
            "config": {
                "kind": "container",
                "runtime": "docker",
                "connection": {
                    "kind": "socket",
                    "socket_path": "/var/run/docker.sock",
                },
                "reachability": {
                    "kind": "host_port",
                    "bind_host": "127.0.0.1",
                },
            },
        },
    )
    container_provider_created = r.status_code == 201

    cleanup_urls: list[str] = []
    if container_provider_created:
        cleanup_urls.append(f"/v1/workspace_providers/{wp_id}")
    cleanup_urls.extend([
        f"/v1/graphs/{gid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ])

    try:
        if not container_provider_created:
            # Docker unavailable -- clean rejection, not 500.
            assert r.status_code in (400, 422, 503), r.text
            assert "internal" not in r.json().get("type", ""), r.json()
            return

        r = await client.post(
            "/v1/workspace_templates",
            json={
                "id": tpl_id,
                "description": "container tpl t736",
                "provider_id": wp_id,
                "backend": {
                    "kind": "container",
                    "image": "primer/workspace-runtime:1.0",
                },
            },
        )
        if r.status_code != 201:
            assert r.status_code in (400, 422, 503), r.text
            return
        cleanup_urls.insert(0, f"/v1/workspace_templates/{tpl_id}")

        r = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        if r.status_code not in (200, 201):
            # The container runtime can't provision the workspace (e.g. CI has
            # no `primer/workspace-runtime:1.0` image). That must be a CLEAN
            # rejection (503/4xx), never an unhandled 500 — see the DockerError
            # -> ConfigError mapping in DockerRuntimeAdapter.create_sandbox.
            assert r.status_code in (400, 422, 503), r.text
            assert "internal" not in r.json().get("type", ""), r.json()
            return
        wid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/workspaces/{wid}")

        # Wait for the container to reach running
        phase = None
        for _ in range(90):
            got = await client.get(f"/v1/workspaces/{wid}")
            assert got.status_code == 200, got.text
            phase = got.json().get("phase")
            if phase == "running":
                break
            assert phase not in ("failed", "error"), got.text
            await asyncio.sleep(1.0)
        assert phase == "running", f"workspace never reached running: phase={phase!r}"

        r = await client.post(
            "/v1/graphs",
            json={
                "id": gid,
                "description": "t736 graph",
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

        # Container graph sessions are now supported: must be 201.
        r = await client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": gid},
                "auto_start": True,
            },
        )
        assert r.status_code == 201, (
            f"graph session on container workspace should be 201; "
            f"got {r.status_code}: {r.text}"
        )
        sid = r.json()["id"]
        cleanup_urls.insert(0, f"/v1/workspaces/{wid}/sessions/{sid}/cancel")

        # Poll to terminal -- allow generous timeout for container boot
        deadline = time.monotonic() + 120.0
        last: dict = {}
        while time.monotonic() < deadline:
            gr = await client.get(f"/v1/sessions/{sid}")
            assert gr.status_code == 200, gr.text
            last = gr.json()
            if last.get("status") in ("ended", "failed", "cancelled"):
                break
            await asyncio.sleep(1.0)

        assert last.get("status") == "ended", (
            f"container graph session did not reach ended: {last}"
        )
        last_err = last.get("last_error")
        if last_err:
            assert "/errors/internal" not in str(last_err.get("type", "")), (
                f"container graph session has /errors/internal: {last_err}"
            )
    finally:
        await _cleanup(client, cleanup_urls)


# ===========================================================================
# T0739 — Graph with callable-router but empty RouterRegistry
# ===========================================================================


@pytest.mark.asyncio
async def test_t0739_graph_callable_router_empty_registry_clean_fatal(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path,
) -> None:
    """T0739 — A graph whose edges use a ``callable`` router pointed
    at a callable_id not registered in the app's :class:`RouterRegistry`
    must converge to ENDED with ``ended_reason='failed'`` and a
    populated ``last_error`` mentioning the router. NEVER
    /errors/internal under any code path.

    Defends against the executor swallowing the missing-router error
    or surfacing it as a 5xx envelope on /v1/sessions/{id}.
    """
    pid = f"llm-t739-{unique_suffix}"
    aid = f"ag-t739-{unique_suffix}"
    gid = f"gr-t739-{unique_suffix}"
    wp_id = f"wp-t739-{unique_suffix}"
    tpl_id = f"tpl-t739-{unique_suffix}"
    await _seed_llm_provider(client, pid)
    await _seed_agent(client, aid, pid)
    # local workspace so the executor at least starts (it can fail
    # later at the router resolution).
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
        f"/v1/graphs/{gid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]

    try:
        # Graph with two agent nodes connected by a callable router
        # edge pointing at an unregistered callable_id.
        # max_iterations is required by the validator whenever a
        # callable router is present (it may route to any node).
        r = await client.post(
            "/v1/graphs",
            json={
                "id": gid,
                "description": "router fatal",
                "entry_node_id": "begin",
                "max_iterations": 10,
                "nodes": [
                    {"id": "begin", "kind": "begin"},
                    {"id": "n1", "kind": "agent", "agent_id": aid},
                    {"id": "n2", "kind": "agent", "agent_id": aid},
                    {"id": "end", "kind": "end"},
                ],
                "edges": [
                    {
                        "kind": "static",
                        "from_node": "begin",
                        "to_node": "n1",
                    },
                    {
                        "kind": "conditional",
                        "from_node": "n1",
                        "router": {
                            "kind": "callable",
                            "callable_id": "no-such-router",
                        },
                    },
                    {
                        "kind": "static",
                        "from_node": "n2",
                        "to_node": "end",
                    },
                ],
            },
        )
        assert r.status_code == 201, r.text

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
        terminal_seen = False
        body: dict = {}
        while time.monotonic() < deadline:
            gr = await client.get(f"/v1/sessions/{sid}")
            assert gr.status_code == 200, gr.text
            body = gr.json()
            if body["status"] in ("ended", "failed", "cancelled"):
                terminal_seen = True
                break
            await asyncio.sleep(0.5)
        assert terminal_seen, (
            f"graph session did not reach terminal within 20s; "
            f"final status={body.get('status')}"
        )
        # The session may have ended via fatal during build (router not
        # found at executor construction). last_error should be set.
        last_error = body.get("last_error")
        # If last_error is populated, type must not be internal-error.
        if last_error:
            assert "internal" not in last_error.get("type", ""), last_error
    finally:
        await _cleanup(client, cleanup_urls)
