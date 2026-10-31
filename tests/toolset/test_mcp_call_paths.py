"""MCP tool calls: a failed stdio handshake is classified, structured-only results reach the model, and a call that never answers is bounded.

* AG-M15: a stdio server that answers ``initialize`` with a JSON-RPC error made ``list_tools`` raise a classified ``ProviderError`` but ``call`` (and
  the task-mode requests) a raw ``MCPError``, because those enter the session outside their ``try`` and ``_enter_stdio_session`` re-raised it bare.
  The loop answers only a ``PrimerError`` as a tool error, so the turn failed.
* AG-L3: a result with only ``structuredContent`` (no content blocks) reached the model as ``""``. It is now the structured content as JSON.
* W32: the stdio ``ClientSession`` had no read timeout, so a server that stopped answering ``tools/call`` held the turn (and its claim) until a Stop.
  Every session gets ``McpConfig.read_timeout_seconds`` (default 300 s, the SDK's HTTP read default), and the timeout is a tool error.

The first two are the review probe (``/tmp/review-agent/toolset/test_mcp_call_paths.py``) inverted.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import ToolCallPart
from primer.model.except_ import PrimerError, ProviderError
from primer.model.principal import PrincipalRef
from primer.model.provider import HttpConfig, McpConfig, StdioConfig, TransportType
from primer.toolset.mcp import McpToolsetProvider

# Answers ``initialize`` with a JSON-RPC error, then waits.
INIT_ERROR = """
import json, sys
req = json.loads(sys.stdin.readline())
sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": "init boom"}}) + "\\n")
sys.stdout.flush()
sys.stdin.readline()
"""

# A server with one tool. ``tools/call`` answers MODE: "structured" (structuredContent only), "text" (a text block beside it), "silent" (nothing).
SERVER = """
import json, sys
MODE = sys.argv[1]
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    req = json.loads(line)
    m, i = req.get("method"), req.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "s", "version": "1"}}})
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": i, "result": {"tools": [{"name": "weather", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object", "properties": {"temp": {"type": "number"}}}}]}})
    elif m == "tools/call":
        if MODE == "silent":
            continue
        content = [{"type": "text", "text": "21.5 C"}] if MODE == "text" else []
        send({"jsonrpc": "2.0", "id": i, "result": {"content": content, "structuredContent": {"temp": 21.5}, "isError": False}})
    elif i is not None:
        send({"jsonrpc": "2.0", "id": i, "result": {}})
"""


def _stdio(script: str, *argv: str, **config) -> McpToolsetProvider:
    cfg = McpConfig(transport=TransportType.STDIO, config=StdioConfig(command=[sys.executable, "-c", script, *argv]), **config)
    return McpToolsetProvider("ts-probe", cfg)


# ---- AG-M15 ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "dispatch",
    [
        pytest.param(lambda p: p.call(tool_name="anything", arguments={}), id="call"),
        pytest.param(lambda p: p._call_task_mode(session_id="s-1", tool_call_id="c", tool_name="anything", arguments={}, principal=None), id="task_mode"),
        pytest.param(lambda p: p.poll_task_status("t-1"), id="poll_task_status"),
        pytest.param(lambda p: p.fetch_task_result("t-1"), id="fetch_task_result"),
        pytest.param(lambda p: p.cancel_task("t-1"), id="cancel_task"),
    ],
)
async def test_a_failed_stdio_handshake_on_any_request_is_a_classified_provider_error(dispatch) -> None:
    with pytest.raises(PrimerError) as raised:
        await dispatch(_stdio(INIT_ERROR))

    assert isinstance(raised.value, ProviderError)
    assert (raised.value.code, str(raised.value)) == ("-32603", "[-32603] init boom")


@pytest.mark.timeout(60)
async def test_characterization_a_failed_stdio_handshake_on_a_listing_keeps_its_provider_error() -> None:
    with pytest.raises(ProviderError) as raised:
        [t async for t in _stdio(INIT_ERROR).list_tools()]

    assert (raised.value.code, str(raised.value), type(raised.value.__cause__).__name__) == ("-32603", "[-32603] init boom", "MCPError")


@pytest.mark.timeout(60)
async def test_a_failed_stdio_handshake_is_a_tool_error_and_not_a_failed_turn() -> None:
    class _Listed(McpToolsetProvider):
        """Lists without a handshake (a listing would fail the same way), so the call is what meets the failing server."""

        async def _fetch_mcp_tools(self, *, principal=None):
            return [mcp_types.Tool(name="anything", inputSchema={"type": "object"})]

    cfg = McpConfig(transport=TransportType.STDIO, config=StdioConfig(command=[sys.executable, "-c", INIT_ERROR]))
    manager = ToolExecutionManager(
        toolset_providers={"ext": _Listed("ext", cfg)},  # type: ignore[dict-item]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )

    result = await manager.execute(ToolCallPart(id="c", name="ext__anything", arguments={}))

    assert (result.error, result.output) == (True, "[-32603] init boom")


# ---- AG-L3 -----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_a_structured_content_only_result_reaches_the_model_as_json() -> None:
    result = await _stdio(SERVER, "structured").call(tool_name="weather", arguments={})

    assert (result.output, result.is_error, result.extended) == (json.dumps({"temp": 21.5}), False, None)


@pytest.mark.timeout(60)
async def test_characterization_a_text_block_beside_structured_content_is_the_output() -> None:
    result = await _stdio(SERVER, "text").call(tool_name="weather", arguments={})

    assert (result.output, result.is_error, result.extended) == ("21.5 C", False, None)


async def test_characterization_an_empty_result_with_no_structured_content_is_still_empty() -> None:
    provider = _stdio(SERVER, "structured")

    result = provider._mcp_call_result_to_primer(mcp_types.CallToolResult(content=[], is_error=True))

    assert (result.output, result.is_error, result.extended) == ("", True, None)


# ---- W32 -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_a_stdio_call_that_never_answers_fails_within_the_read_timeout() -> None:
    provider = _stdio(SERVER, "silent", read_timeout_seconds=1.0)
    started = time.monotonic()

    with pytest.raises(PrimerError):
        await provider.call(tool_name="weather", arguments={})

    assert time.monotonic() - started < 30, "the call was not bounded by its read timeout"


@pytest.mark.timeout(60)
async def test_a_call_that_never_answers_is_a_tool_error_through_the_manager() -> None:
    manager = ToolExecutionManager(
        toolset_providers={"ext": _stdio(SERVER, "silent", read_timeout_seconds=1.0)},  # type: ignore[dict-item]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )

    result = await manager.execute(ToolCallPart(id="c", name="ext__weather", arguments={}))

    assert result.error is True


def test_the_read_timeout_defaults_to_300_seconds_and_must_be_positive() -> None:
    cfg = McpConfig(transport=TransportType.STDIO, config=StdioConfig(command=["x"]))
    assert cfg.read_timeout_seconds == 300.0
    with pytest.raises(ValueError):
        McpConfig(transport=TransportType.STDIO, config=StdioConfig(command=["x"]), read_timeout_seconds=0)


@pytest.mark.parametrize("transport", [TransportType.HTTP, TransportType.SSE])
async def test_http_sessions_get_the_read_timeout_and_keep_the_sdk_client_defaults(monkeypatch, transport) -> None:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))
    sessions: list[dict] = []
    clients: list[dict] = []
    transports: list[dict] = []

    def _session(*args, **kwargs):
        sessions.append(kwargs)
        return session

    @asynccontextmanager
    async def _transport(**kwargs):
        transports.append(kwargs)
        yield (object(), object())

    @asynccontextmanager
    async def _client(**kwargs):
        clients.append(kwargs)
        yield object()

    monkeypatch.setattr("primer.toolset.mcp.ClientSession", _session)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", _transport)
    monkeypatch.setattr("mcp.client.sse.sse_client", _transport)
    monkeypatch.setattr("mcp.shared._httpx_utils.create_mcp_http_client", _client)
    provider = McpToolsetProvider("ts1", McpConfig(transport=transport, config=HttpConfig(url="http://example.test/mcp"), read_timeout_seconds=42.0))

    assert [t async for t in provider.list_tools()] == []

    assert [s.get("read_timeout_seconds") for s in sessions] == [42.0]
    # The HTTP client keeps the SDK's own timeouts: nothing but headers and auth is passed to it.
    assert all(set(c) == {"headers", "auth"} for c in clients)
    assert all("timeout" not in t and "sse_read_timeout" not in t for t in transports)
