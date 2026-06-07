"""SMK MCP integration tests.

Direction A (Primer as MCP client): mount the in-repo stdio/http fixture
servers as toolsets, select tools, call one end to end, and confirm failure
isolation. Direction B (Primer as server): the /v1/mcp exposure allowlist,
system-only floor, and disable. The external-client list/invoke (MCP-07/08)
and endpoint auth (MCP-10) need an MCP client speaking to /v1/mcp.

The external-MCP variants (``*_external_*``) drive the SAME journeys against a
REAL third-party MCP server, ``open-websearch`` (https://github.com/aas-ee/open-websearch),
a no-API-key web-search MCP that speaks both stdio and streamable-HTTP. They are
gated on the ``mcp:stdio`` / ``mcp:http`` capabilities and read their connection
details from ``tests/testconfig.yaml``. Results from a live web-search server are
non-deterministic, so these assert the contract (mount succeeds, the tool
catalogue is non-empty and includes the expected ``search`` tool, and a scripted
agent's tool call round-trips through the real server) rather than exact content.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# External open-websearch MCP server: testconfig accessors + http daemon.
# ---------------------------------------------------------------------------

# open-websearch exposes a `search` tool plus several site-specific fetchers.
_EXPECTED_OWS_TOOL = "search"


def _ows_stdio_cfg() -> dict:
    """The testconfig ``mcp.stdio`` block (command + env) for open-websearch."""
    return load_config().get("mcp", {}).get("stdio", {})


@pytest.fixture
def ows_stdio_mount() -> dict:
    """The ``config`` body for mounting open-websearch over stdio.

    Reads the command (``npx -y open-websearch@latest``) and env (``MODE=stdio``)
    from testconfig so the test stays in sync with the operator's config.
    """
    cfg = _ows_stdio_cfg()
    stdio: dict = {"command": list(cfg["command"])}
    if cfg.get("env"):
        stdio["env"] = dict(cfg["env"])
    return {"transport": "stdio", "config": stdio}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ows_http_url():
    """Yield the /mcp URL of a freshly spawned open-websearch HTTP daemon.

    testconfig pins ``mcp.http.url`` at ``:3000``, but that port may be taken by
    an unrelated service, so the daemon is launched on a free port and its URL is
    yielded instead. The first ``npx`` invocation may download the package, so
    startup is allowed up to ~90s. The process is started in its own session and
    the whole process group is terminated (then SIGKILLed) on teardown so the
    node subprocess npx spawns cannot leak.
    """
    port = _free_port()
    env = dict(os.environ, PORT=str(port), MODE="http")
    proc = subprocess.Popen(
        ["npx", "-y", "open-websearch@latest"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.time() + 120
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"open-websearch http daemon exited early (rc={proc.returncode})"
                )
            try:
                # A bare GET on /mcp is rejected (4xx) by the MCP endpoint, but
                # any HTTP response means the daemon is listening; only a
                # ConnectError means it is not up yet.
                httpx.get(url, timeout=2.0)
                ready = True
                break
            except httpx.ConnectError:
                time.sleep(0.5)
        if not ready:
            raise RuntimeError("open-websearch http daemon did not become reachable")
        yield url
    finally:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None
        if pgid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:  # pragma: no cover
                    continue


# ---------------------------------------------------------------------------
# In-repo (hermetic) MCP fixture servers (echo/bump). These are always the
# in-repo stdio/http fixture servers regardless of testconfig.mcp, so the
# hermetic SMK-MCP journeys keep asserting echo/bump + the marker file even
# when testconfig points the external lane at a real third-party MCP server.
# ---------------------------------------------------------------------------


@pytest.fixture
def inrepo_stdio_command() -> list[str]:
    return ["uv", "run", "python", "-m", "tests._support.mcp.stdio_server"]


@pytest.fixture
def inrepo_http_url():
    port = _free_port()
    env = dict(
        os.environ,
        PRIMER_TEST_MCP_HTTP_HOST="127.0.0.1",
        PRIMER_TEST_MCP_HTTP_PORT=str(port),
    )
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "tests._support.mcp.http_server"],
        env=env,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                httpx.get(url, timeout=1.0)
                break
            except httpx.ConnectError:
                time.sleep(0.2)
        yield url
    finally:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None
        if pgid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:  # pragma: no cover
                    continue


async def _mount_stdio(authed_client, command, suffix) -> str:
    tid = f"mcp-stdio-{suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "stdio", "config": {"command": command}}},
    )
    assert r.status_code in (200, 201), r.text
    return tid


@smk("SMK-MCP-01")
async def test_mount_stdio_lists_tools(authed_client, inrepo_stdio_command, unique_suffix):
    tid = await _mount_stdio(authed_client, inrepo_stdio_command, unique_suffix)
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    assert tools.status_code == 200, tools.text
    names = {t["id"] for t in tools.json().get("items", tools.json().get("tools", []))}
    assert {"echo", "bump"} <= names, names
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-02")
async def test_mount_http_lists_tools(authed_client, inrepo_http_url, unique_suffix):
    tid = f"mcp-http-{unique_suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "http", "config": {"url": inrepo_http_url}}},
    )
    assert r.status_code in (200, 201), r.text
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    assert tools.status_code == 200, tools.text
    names = {t["id"] for t in tools.json().get("items", tools.json().get("tools", []))}
    assert {"echo", "bump"} <= names, names
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-04", "SMK-MCP-03")
async def test_call_mounted_mcp_tool_end_to_end(
    authed_client, mock_llm, inrepo_stdio_command, unique_suffix, tmp_path
):
    tid = await _mount_stdio(authed_client, inrepo_stdio_command, unique_suffix)
    registry, base_url = mock_llm
    sc = f"scripted:mcp04-{unique_suffix}"
    marker = str(tmp_path / "mcp_marker.txt")
    # MCP-03: scope the agent to exactly one tool from the mounted server.
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=[f"{tid}__bump"],
        rules=[
            Rule(when_tool_offered="bump", when_tool_result=False,
                 emit_tool=f"{tid}__bump", emit_args={"marker_path": marker}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    final = await wait_terminal(authed_client, sid)
    assert final.get("status") == "ended", final
    # the external MCP server actually received the call: its marker file exists
    import os
    assert os.path.exists(marker), "MCP bump tool did not run"
    assert open(marker).read().strip() == "1"
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-05")
async def test_mounted_server_failure_isolated(authed_client, unique_suffix):
    tid = f"mcp-bad-{unique_suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "stdio",
                         "config": {"command": [f"nonexistent-xyz-{unique_suffix}"]}}},
    )
    assert r.status_code in (200, 201), r.text
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    # a broken server yields a clean envelope, never /errors/internal, and the
    # platform stays healthy
    if tools.status_code >= 400:
        assert tools.json()["type"] != "/errors/internal", tools.text
    health = await authed_client.get("/v1/health")
    assert health.status_code == 200
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-06", "SMK-MCP-11")
async def test_exposure_enable_and_disable(authed_client):
    enable = await authed_client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": ["misc__uuid_v4", "system__call_tool"]},
    )
    assert enable.status_code in (200, 204), enable.text
    got = await authed_client.get("/v1/mcp_exposure")
    assert got.json()["enabled"] is True
    assert "misc__uuid_v4" in got.json()["allowed_tools"]
    disable = await authed_client.put("/v1/mcp_exposure", json={"enabled": False})
    assert disable.status_code in (200, 204), disable.text
    assert (await authed_client.get("/v1/mcp_exposure")).json()["enabled"] is False


@smk("SMK-MCP-09")
async def test_system_only_floor_rejects_user_toolset(authed_client, inrepo_stdio_command, unique_suffix):
    tid = await _mount_stdio(authed_client, inrepo_stdio_command, unique_suffix)
    # a tool from a user-defined toolset must not be allowlistable for exposure
    r = await authed_client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": [f"{tid}__echo"]},
    )
    assert r.status_code == 422, r.text
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-07", "SMK-MCP-08", "SMK-MCP-10", status="partial")
@requires("mcp:client")
async def test_external_mcp_client_roundtrip():
    pytest.skip("needs an external MCP client speaking to /v1/mcp (testconfig.mcp.client_bearer_token)")


# ===========================================================================
# External real-server MCP journeys (open-websearch over stdio + http).
# ===========================================================================


async def _mount_mcp(authed_client, tid: str, config: dict) -> None:
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp", "config": config},
    )
    assert r.status_code in (200, 201), r.text


async def _list_tool_names(authed_client, tid: str) -> set[str]:
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    assert tools.status_code == 200, tools.text
    body = tools.json()
    items = body.get("tools", body.get("items", []))
    return {t["id"] for t in items}


async def _drive_search_through_real_mcp(
    authed_client, mock_llm, tid: str, suffix: str, tmp_path
) -> dict:
    """Scripted agent emits a ``search`` tool call against the REAL mounted
    open-websearch server; the second turn fires only once the live tool result
    flows back. Returns the terminal session row.

    A scripted mock LLM (not the real qwen model) is used deliberately so the
    tool call is emitted deterministically every run; the open-websearch server,
    the MCP transport, and the result round-trip are all REAL.
    """
    registry, base_url = mock_llm
    scenario = f"scripted:ows-{suffix}"
    scoped_tool = f"{tid}__{_EXPECTED_OWS_TOOL}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=suffix, scenario=scenario,
        tools=[scoped_tool],
        rules=[
            # First turn (no tool result yet): call the real search tool.
            Rule(when_tool_result=False, emit_tool=scoped_tool,
                 emit_args={"query": "python programming language",
                            "limit": 3, "engines": ["bing"]}),
            # Second turn fires ONLY because the live MCP result came back.
            Rule(when_tool_result=True, emit_text="search complete"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    final = await wait_terminal(authed_client, sid, timeout_s=120)
    assert final.get("status") == "ended", final
    # The second turn proves the tool call round-tripped through the real server:
    # the `when_tool_result=True` rule only matches once a tool result is present.
    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    assert tl.json().get("total", 0) >= 2, tl.json()
    return final


@smk("SMK-MCP-01", "SMK-MCP-03", "SMK-MCP-04")
@requires("mcp:stdio")
async def test_external_stdio_open_websearch(
    authed_client, mock_llm, ows_stdio_mount, unique_suffix, tmp_path
):
    """Mount the REAL open-websearch server over stdio, confirm its tool
    catalogue, scope an agent to its ``search`` tool, and drive a live tool call
    end to end through a scripted agent."""
    tid = f"mcp-ext-stdio-{unique_suffix}"
    await _mount_mcp(authed_client, tid, ows_stdio_mount)
    try:
        names = await _list_tool_names(authed_client, tid)
        assert _EXPECTED_OWS_TOOL in names, names
        assert len(names) >= 1, names
        await _drive_search_through_real_mcp(
            authed_client, mock_llm, tid, unique_suffix, tmp_path)
    finally:
        await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-02", "SMK-MCP-03", "SMK-MCP-04")
@requires("mcp:http")
async def test_external_http_open_websearch(
    authed_client, mock_llm, ows_http_url, unique_suffix, tmp_path
):
    """Spawn a REAL open-websearch streamable-HTTP daemon, mount it over the
    http transport, confirm its tool catalogue, and drive a live tool call end
    to end. The daemon's lifecycle is managed by the ``ows_http_url`` fixture
    (started in setup, process group terminated in teardown)."""
    tid = f"mcp-ext-http-{unique_suffix}"
    await _mount_mcp(
        authed_client, tid, {"transport": "http", "config": {"url": ows_http_url}})
    try:
        names = await _list_tool_names(authed_client, tid)
        assert _EXPECTED_OWS_TOOL in names, names
        assert len(names) >= 1, names
        await _drive_search_through_real_mcp(
            authed_client, mock_llm, tid, unique_suffix, tmp_path)
    finally:
        await authed_client.delete(f"/v1/toolsets/{tid}")
