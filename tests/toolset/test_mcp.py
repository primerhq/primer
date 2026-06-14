"""Tests for primer.toolset.mcp.McpToolsetProvider -- stdio transport.

Exercised against an in-memory MCP server (no real subprocess). The
HTTP-transport tests live in the same file (added in Task 6).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as mcp_types
import pytest
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session

from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import ConfigError, UnsupportedContentError
from primer.model.provider import HttpConfig, McpConfig, StdioConfig, TransportType
from primer.toolset.mcp import McpToolsetProvider


# ---------- in-memory MCP server fixtures ---------------------------------


def _make_test_server() -> Server:
    """A minimal MCP server exposing two tools: 'echo' and 'fail'."""
    server = Server("primer-test-server")

    @server.list_tools()
    async def _list() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="echo",
                description="echo arguments back as text",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            mcp_types.Tool(
                name="fail",
                description="always returns an error result",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]):
        if name == "echo":
            return [
                mcp_types.TextContent(
                    type="text", text=f"echo:{arguments.get('text', '')}"
                )
            ]
        if name == "fail":
            raise ValueError("simulated tool failure")
        raise ValueError(f"unknown tool {name}")

    return server


# ---------- a McpToolsetProvider subclass that accepts a pre-built session


class _InMemoryMcpToolsetProvider(McpToolsetProvider):
    """McpToolsetProvider variant that bypasses real transport setup.

    Tests drive the provider against a pre-built ``ClientSession``
    yielded by ``create_connected_server_and_client_session``. The base
    class supplies the request-translation logic; this subclass just
    swaps out the session-acquisition path.
    """

    def __init__(self, toolset_id: str, session) -> None:
        config = McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=["true"]),
        )
        super().__init__(toolset_id=toolset_id, config=config)
        self._injected_session = session

    @asynccontextmanager
    async def _open_session(self, *, principal=None):  # type: ignore[override]
        yield self._injected_session


# ---------- tests --------------------------------------------------------


class TestConstructor:
    def test_stdio_config_is_accepted(self) -> None:
        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["true"]),
            ),
        )
        assert provider is not None

    def test_complete_oauth_without_handler_raises_config_error(self) -> None:
        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["true"]),
            ),
        )

        async def _go() -> None:
            await provider.complete_oauth(code="x", state="y")

        with pytest.raises(ConfigError):
            asyncio.run(_go())


class TestListToolsStdio:
    async def test_lists_both_test_tools(self) -> None:
        server = _make_test_server()
        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=session)
            tools = [t async for t in provider.list_tools()]
        names = sorted(t.id for t in tools)
        assert names == ["echo", "fail"]

    async def test_tools_carry_provider_toolset_id(self) -> None:
        server = _make_test_server()
        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(
                toolset_id="ts-provider", session=session
            )
            tools = [t async for t in provider.list_tools()]
        for t in tools:
            assert t.toolset_id == "ts-provider"

    async def test_tools_preserve_input_schema(self) -> None:
        server = _make_test_server()
        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=session)
            tools = {t.id: t async for t in provider.list_tools()}
        assert tools["echo"].args_schema == {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }


class TestCallToolStdio:
    async def test_text_content_is_returned_as_output(self) -> None:
        server = _make_test_server()
        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=session)
            result = await provider.call(tool_name="echo", arguments={"text": "hi"})
        assert isinstance(result, ToolCallResult)
        assert "echo:hi" in result.output
        assert result.is_error is False

    async def test_failing_tool_returns_is_error_true(self) -> None:
        server = _make_test_server()
        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=session)
            result = await provider.call(tool_name="fail", arguments={})
        assert result.is_error is True

    async def test_unknown_tool_propagates_as_provider_error(self) -> None:
        from primer.model.except_ import ProviderError

        # The MCP lowlevel server catches handler exceptions and converts
        # them to ``CallToolResult(isError=True, content=[TextContent(...)])``
        # rather than letting them surface as McpError. Either path is a
        # valid "tool failed" signal -- accept whichever the SDK delivers.
        server = _make_test_server()
        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=session)
            try:
                result = await provider.call(
                    tool_name="never-defined", arguments={}
                )
            except (ProviderError, UnsupportedContentError):
                return  # acceptable: surfaced as a primer exception
            assert result.is_error is True
            assert "never-defined" in result.output


class TestContentMapping:
    async def test_image_content_serialises_to_extended(self) -> None:
        server = Server("img-test")

        @server.list_tools()
        async def _list() -> list[mcp_types.Tool]:
            return [
                mcp_types.Tool(
                    name="picture",
                    description="returns an image",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]

        @server.call_tool()
        async def _call(name: str, arguments: dict[str, Any]):
            return [
                mcp_types.TextContent(type="text", text="see image"),
                mcp_types.ImageContent(
                    type="image",
                    data="base64data",
                    mimeType="image/png",
                ),
            ]

        async with create_connected_server_and_client_session(server) as session:
            provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=session)
            result = await provider.call(tool_name="picture", arguments={})

        assert "see image" in result.output
        assert result.extended is not None
        assert "content" in result.extended
        assert any(item.get("type") == "image" for item in result.extended["content"])


# ---- HTTP transport tests --------------------------------------------------


class TestHttpTransportConstructor:
    def test_http_config_is_accepted(self) -> None:
        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url="http://localhost:9999/mcp"),
            ),
        )
        assert provider is not None


class TestHttpTransportRouting:
    """The HTTP success path is exercised by the gated integration smoke
    test. Here we only assert that a request against an unreachable URL
    surfaces as the right primer exception.
    """

    @pytest.mark.skip(
        reason="MCP SDK swallows transport exceptions in task groups; needs follow-up"
    )
    async def test_unreachable_endpoint_raises_primer_error(self) -> None:
        from primer.model.except_ import PrimerError

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url="http://127.0.0.1:1/does-not-exist"),
            ),
        )
        with pytest.raises(PrimerError):
            async for _ in provider.list_tools():
                pass


class TestHttpTransportSuccess:
    """Unit-level coverage of the HTTP success path by patching the SDK."""

    async def test_http_session_path_drives_initialise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        # Build a fake ClientSession that responds to list_tools.
        fake_session = MagicMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=None)
        fake_session.initialize = AsyncMock()
        fake_session.list_tools = AsyncMock(
            return_value=mcp_types.ListToolsResult(tools=[])
        )

        @asynccontextmanager
        async def fake_streamablehttp(**kwargs):
            yield (object(), object(), lambda: None)

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamablehttp_client",
            fake_streamablehttp,
        )
        monkeypatch.setattr(
            "primer.toolset.mcp.ClientSession",
            lambda *args, **kwargs: fake_session,
        )

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url="http://example.test/mcp"),
            ),
        )
        tools = [t async for t in provider.list_tools()]
        assert tools == []
        fake_session.initialize.assert_awaited_once()

    async def test_http_session_path_with_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        fake_session = MagicMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=None)
        fake_session.initialize = AsyncMock()
        fake_session.list_tools = AsyncMock(
            return_value=mcp_types.ListToolsResult(tools=[])
        )

        captured: dict[str, object] = {}

        @asynccontextmanager
        async def fake_streamablehttp(**kwargs):
            captured.update(kwargs)
            yield (object(), object(), lambda: None)

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamablehttp_client",
            fake_streamablehttp,
        )
        monkeypatch.setattr(
            "primer.toolset.mcp.ClientSession",
            lambda *args, **kwargs: fake_session,
        )

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(
                    url="http://example.test/mcp",
                    headers={"Authorization": "Bearer x"},
                ),
            ),
        )
        _ = [t async for t in provider.list_tools()]
        assert captured["headers"] == {"Authorization": "Bearer x"}

    async def test_http_session_path_initialise_failure_is_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception from session.initialize() is wrapped via classify_mcp_exception."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from primer.model.except_ import PrimerError

        fake_session = MagicMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=None)
        fake_session.initialize = AsyncMock(side_effect=RuntimeError("boom"))

        @asynccontextmanager
        async def fake_streamablehttp(**kwargs):
            yield (object(), object(), lambda: None)

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamablehttp_client",
            fake_streamablehttp,
        )
        monkeypatch.setattr(
            "primer.toolset.mcp.ClientSession",
            lambda *args, **kwargs: fake_session,
        )

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url="http://example.test/mcp"),
            ),
        )
        with pytest.raises(PrimerError):
            async for _ in provider.list_tools():
                pass


class _StdioLifecycle:
    """Test double tracking per-dispatch stdio subprocess lifecycle.

    ``starts`` counts how many subprocesses were launched (one
    ``stdio_client`` ``__aenter__`` each); ``closes`` counts how many
    were torn down (``__aexit__``). ``live`` is starts minus closes --
    the number of subprocesses currently running. A correct
    per-dispatch implementation always returns ``live == 0`` after every
    dispatch completes.
    """

    def __init__(self) -> None:
        self.starts = 0
        self.closes = 0
        self.initialises = 0

    @property
    def live(self) -> int:
        return self.starts - self.closes

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        outer = self

        @asynccontextmanager
        async def fake_stdio_client(params):
            outer.starts += 1
            try:
                yield (object(), object())
            finally:
                # Closing the stdio_client context is what terminates
                # the subprocess + closes the pipes in the real SDK.
                outer.closes += 1

        def make_session(*args, **kwargs):
            session = MagicMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=None)

            async def _initialize():
                outer.initialises += 1

            session.initialize = AsyncMock(side_effect=_initialize)
            session.list_tools = AsyncMock(
                return_value=mcp_types.ListToolsResult(tools=[])
            )
            session.call_tool = AsyncMock(
                return_value=mcp_types.CallToolResult(
                    content=[mcp_types.TextContent(type="text", text="ok")],
                    isError=False,
                )
            )
            return session

        monkeypatch.setattr("primer.toolset.mcp.stdio_client", fake_stdio_client)
        monkeypatch.setattr("primer.toolset.mcp.ClientSession", make_session)


class TestStdioSessionLifecycle:
    """Per-dispatch stdio lifecycle: a dispatch starts a subprocess and
    closes it when done; concurrent/sequential dispatches each get a
    fresh one; cleanup happens even on error."""

    def _provider(self) -> McpToolsetProvider:
        return McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["fake-mcp", "--root", "/tmp"]),
            ),
        )

    async def test_dispatch_starts_then_closes_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lc = _StdioLifecycle()
        lc.install(monkeypatch)
        provider = self._provider()

        tools = [t async for t in provider.list_tools()]

        assert tools == []
        assert lc.starts == 1
        # The subprocess must be terminated once the dispatch completes.
        assert lc.closes == 1
        assert lc.live == 0

    async def test_second_dispatch_starts_fresh_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lc = _StdioLifecycle()
        lc.install(monkeypatch)
        provider = self._provider()

        # Two separate dispatches: each must launch + tear down its own
        # subprocess and re-run the init handshake (no caching).
        await provider.call(tool_name="echo", arguments={})
        await provider.call(tool_name="echo", arguments={})

        assert lc.starts == 2
        assert lc.closes == 2
        assert lc.initialises == 2
        assert lc.live == 0

    async def test_concurrent_dispatches_do_not_share_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lc = _StdioLifecycle()
        lc.install(monkeypatch)
        provider = self._provider()

        await asyncio.gather(
            provider.call(tool_name="echo", arguments={}),
            provider.call(tool_name="echo", arguments={}),
            provider.call(tool_name="echo", arguments={}),
        )

        # Each concurrent dispatch gets its own subprocess; none shared.
        assert lc.starts == 3
        assert lc.closes == 3
        assert lc.live == 0

    async def test_multiple_calls_within_one_dispatch_reuse_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lc = _StdioLifecycle()
        lc.install(monkeypatch)
        provider = self._provider()

        # A single dispatch that issues several tool calls over one
        # _open_session context must reuse the one subprocess and only
        # tear it down at the end.
        async with provider._open_session() as session:
            await session.call_tool("echo", arguments={})
            await session.call_tool("echo", arguments={})
            await session.call_tool("echo", arguments={})
            assert lc.starts == 1
            assert lc.live == 1  # still alive mid-dispatch
            assert lc.initialises == 1  # handshake ran once for the dispatch

        # Closed at the end of the dispatch.
        assert lc.closes == 1
        assert lc.live == 0

    async def test_subprocess_closed_when_tool_call_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lc = _StdioLifecycle()
        lc.install(monkeypatch)
        provider = self._provider()

        from primer.model.except_ import PrimerError

        # call_tool raises mid-dispatch -- the subprocess must still be
        # torn down by the try/finally in _open_session.
        async def _boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        async with provider._open_session() as session:
            pass  # prime the lifecycle counters
        assert lc.live == 0  # sanity: that dispatch already closed

        # Now drive a failing call through the real `call` entrypoint.
        def make_failing_session(*args, **kwargs):
            from unittest.mock import AsyncMock, MagicMock

            session = MagicMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=None)
            session.initialize = AsyncMock()
            session.call_tool = AsyncMock(side_effect=RuntimeError("kaboom"))
            return session

        monkeypatch.setattr(
            "primer.toolset.mcp.ClientSession", make_failing_session
        )
        before_closes = lc.closes
        before_starts = lc.starts
        with pytest.raises(PrimerError):
            await provider.call(tool_name="echo", arguments={})

        # One new subprocess started and one closed despite the error.
        assert lc.starts == before_starts + 1
        assert lc.closes == before_closes + 1
        assert lc.live == 0

    async def test_stdio_session_initialise_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        starts = {"n": 0}
        closes = {"n": 0}

        fake_session = MagicMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=None)
        fake_session.initialize = AsyncMock(side_effect=RuntimeError("boom"))

        @asynccontextmanager
        async def fake_stdio_client(params):
            starts["n"] += 1
            try:
                yield (object(), object())
            finally:
                closes["n"] += 1

        monkeypatch.setattr(
            "primer.toolset.mcp.stdio_client",
            fake_stdio_client,
        )
        monkeypatch.setattr(
            "primer.toolset.mcp.ClientSession",
            lambda *args, **kwargs: fake_session,
        )

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.STDIO,
                config=StdioConfig(command=["fake-mcp"]),
            ),
        )
        with pytest.raises(RuntimeError, match="boom"):
            async for _ in provider.list_tools():
                pass
        # Even when initialize fails, the half-built subprocess is closed.
        assert starts["n"] == 1
        assert closes["n"] == 1

    async def test_aclose_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lc = _StdioLifecycle()
        lc.install(monkeypatch)
        provider = self._provider()

        # aclose has no long-lived state to free and must be safe to call
        # repeatedly, including before any dispatch.
        await provider.aclose()
        await provider.aclose()
        assert lc.starts == 0
        assert lc.live == 0


class TestSessionRequestErrors:
    """Cover the list_tools/call exception-wrapping branches."""

    async def test_list_tools_session_failure_is_classified(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from primer.model.except_ import PrimerError

        fake_session = MagicMock()
        fake_session.list_tools = AsyncMock(side_effect=RuntimeError("kaboom"))

        provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=fake_session)
        with pytest.raises(PrimerError):
            async for _ in provider.list_tools():
                pass

    async def test_call_session_failure_is_classified(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from primer.model.except_ import PrimerError

        fake_session = MagicMock()
        fake_session.call_tool = AsyncMock(side_effect=RuntimeError("kaboom"))

        provider = _InMemoryMcpToolsetProvider(toolset_id="ts1", session=fake_session)
        with pytest.raises(PrimerError):
            await provider.call(tool_name="echo", arguments={})


# ---- OAuth integration tests ----------------------------------------------


class TestMcpOAuthIntegration:
    def test_complete_oauth_with_handler_dispatches(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from primer.model.provider import OAuthConfig
        from primer.toolset.oauth.handler import PrimerOAuthHandler

        config = OAuthConfig(redirect_uri="https://app.example/cb")
        handler = PrimerOAuthHandler(
            oauth_config=config,
            mcp_url="https://mcp.example/mcp",
            toolset_id="ts1",
        )
        complete_mock = AsyncMock()
        monkeypatch.setattr(handler, "complete_oauth", complete_mock)

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url="https://mcp.example/mcp"),
            ),
            oauth=handler,
        )

        async def _go():
            await provider.complete_oauth(code="c", state="s")

        asyncio.run(_go())
        complete_mock.assert_awaited_once_with(code="c", state_id="s")

    async def test_oauth_authorize_raises_auth_required_propagates_to_list_tools(
        self, monkeypatch
    ) -> None:
        from primer.model.except_ import AuthRequiredError
        from primer.model.provider import OAuthConfig
        from primer.toolset.oauth.handler import PrimerOAuthHandler

        config = OAuthConfig(redirect_uri="https://app.example/cb")
        handler = PrimerOAuthHandler(
            oauth_config=config,
            mcp_url="https://mcp.example/mcp",
            toolset_id="ts1",
        )

        async def _raise(*, principal):
            raise AuthRequiredError(
                "consent",
                auth_url="https://idp.example/auth",
                state="state-id",
            )
        monkeypatch.setattr(handler, "authorize", _raise)

        provider = McpToolsetProvider(
            toolset_id="ts1",
            config=McpConfig(
                transport=TransportType.HTTP,
                config=HttpConfig(url="https://mcp.example/mcp"),
            ),
            oauth=handler,
        )
        with pytest.raises(AuthRequiredError):
            async for _ in provider.list_tools(principal="u-1"):
                pass
