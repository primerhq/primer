"""MCP-protocol :class:`ToolsetProvider` implementation.

Connects to an MCP server over stdio (subprocess; long-lived session
held for the provider's lifetime) or HTTP (streamable-http transport;
one short-lived session per call). Both transports share the
request-translation logic in this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from matrix.common.mcp_errors import classify_mcp_exception
from matrix.int.toolset import ToolsetProvider
from matrix.model.chat import Tool, ToolCallResult
from matrix.model.except_ import AuthRequiredError, ConfigError
from matrix.model.provider import (
    HttpConfig,
    McpConfig,
    StdioConfig,
    TransportType,
)
from matrix.toolset.oauth.handler import MatrixOAuthHandler


logger = logging.getLogger(__name__)


class McpToolsetProvider(ToolsetProvider):
    """MCP-protocol tool source.

    Stdio servers run as a long-lived subprocess (lazy-started on first
    call, kept alive for the provider's lifetime, terminated by
    :meth:`aclose`). HTTP servers open a short-lived
    ``streamable_http`` session per call (cheap; the SDK has no
    long-lived equivalent for stateless HTTP MCP).

    The ``oauth`` constructor argument and :meth:`complete_oauth` method
    are accepted unconditionally so sub-project #10 can wire OAuth in
    without breaking callers built against this version. In this
    sub-project ``oauth`` MUST be ``None``; passing anything else is
    accepted (the field is stored) but unused.
    """

    def __init__(
        self,
        toolset_id: str,
        config: McpConfig,
        *,
        oauth: MatrixOAuthHandler | None = None,
        client_name: str = "matrix",
        client_version: str = "0.0.1",
        allowed_stdio_commands: frozenset[str] | None = None,
    ) -> None:
        """``allowed_stdio_commands``: when set, ``stdio_cfg.command[0]``
        must match one of these strings exactly or :meth:`_ensure_stdio_session`
        raises :class:`ConfigError`. ``None`` (the default) means no
        allowlist is enforced -- the caller has decided either that all
        Toolset rows are operator-trusted or that the auth layer
        upstream gates who can create stdio toolsets at all. Operators
        running multi-tenant deployments SHOULD set an allowlist.
        """
        self._toolset_id = toolset_id
        self._config = config
        self._oauth = oauth
        self._client_name = client_name
        self._client_version = client_version
        self._allowed_stdio_commands = allowed_stdio_commands

        # Stdio long-lived state. Populated lazily on first use.
        self._stdio_lock = asyncio.Lock()
        self._stdio_session: ClientSession | None = None
        self._stdio_exit_stack: AsyncExitStack | None = None

    # ---------- public API ------------------------------------------------

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        async with self._open_session(principal=principal) as session:
            try:
                result = await session.list_tools()
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

        for mcp_tool in result.tools:
            yield self._mcp_tool_to_matrix(mcp_tool)

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
    ) -> ToolCallResult:
        async with self._open_session(principal=principal) as session:
            try:
                result = await session.call_tool(tool_name, arguments=arguments)
            except Exception as exc:
                raise classify_mcp_exception(exc) from exc

            return self._mcp_call_result_to_matrix(result)

    async def complete_oauth(self, *, code: str, state: str) -> None:
        """Finish an OAuth flow started by an earlier AuthRequiredError."""
        if self._oauth is None:
            raise ConfigError("OAuth not configured for this provider")
        await self._oauth.complete_oauth(code=code, state_id=state)

    async def aclose(self) -> None:
        """Tear down any long-lived stdio subprocess. No-op for HTTP."""
        async with self._stdio_lock:
            if self._stdio_exit_stack is not None:
                await self._stdio_exit_stack.aclose()
                self._stdio_exit_stack = None
                self._stdio_session = None

    # ---------- session management ---------------------------------------

    @asynccontextmanager
    async def _open_session(self, *, principal: str | None = None):
        """Yield a ready :class:`mcp.ClientSession` for one operation.

        Stdio: returns the single long-lived session (starting it on
        first call). HTTP: opens a fresh session per call. ``principal``
        is forwarded into the OAuth flow for HTTP transports; passing it
        as a parameter (rather than via an instance field) closes the
        race where two concurrent requests would clobber each other's
        principal.

        Subclasses may override this entirely (used in tests to inject a
        pre-built session over in-memory streams).
        """
        if self._config.transport == TransportType.STDIO:
            session = await self._ensure_stdio_session()
            yield session
            return

        if self._config.transport == TransportType.HTTP:
            assert isinstance(self._config.config, HttpConfig)
            http_cfg: HttpConfig = self._config.config

            from mcp.client.streamable_http import streamablehttp_client

            base_headers: dict[str, str] = (
                dict(http_cfg.headers) if http_cfg.headers else {}
            )
            if self._oauth is not None:
                # May raise AuthRequiredError -- intended bubble-out.
                auth_headers = await self._oauth.authorize(principal=principal)
                base_headers.update(auth_headers)

            stack = AsyncExitStack()
            try:
                streams = await stack.enter_async_context(
                    streamablehttp_client(
                        url=http_cfg.url,
                        headers=base_headers if base_headers else None,
                    )
                )
                # mcp >= 1.16 yields (read, write, get_session_id);
                # older releases yield (read, write).
                if len(streams) >= 2:
                    read, write = streams[0], streams[1]
                else:  # pragma: no cover - defensive for older mcp
                    raise ConfigError(
                        "streamablehttp_client returned an unexpected stream tuple"
                    )

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except (ConfigError, AuthRequiredError):
                await stack.aclose()
                raise
            except Exception as exc:
                await stack.aclose()
                raise classify_mcp_exception(exc) from exc

            try:
                yield session
            finally:
                await stack.aclose()
            return

        raise ConfigError(f"unknown transport {self._config.transport!r}")

    async def _ensure_stdio_session(self) -> ClientSession:
        async with self._stdio_lock:
            if self._stdio_session is not None:
                return self._stdio_session

            assert isinstance(self._config.config, StdioConfig)
            stdio_cfg: StdioConfig = self._config.config
            # Allowlist enforcement: when an operator-supplied allowlist
            # is in effect, refuse to launch any binary not on it.
            if self._allowed_stdio_commands is not None:
                if stdio_cfg.command[0] not in self._allowed_stdio_commands:
                    raise ConfigError(
                        f"toolset {self._toolset_id!r}: stdio command "
                        f"{stdio_cfg.command[0]!r} is not in the allowlist; "
                        "set `allowed_stdio_commands` on the registry / "
                        "AppConfig to permit it."
                    )
            params = StdioServerParameters(
                command=stdio_cfg.command[0],
                args=list(stdio_cfg.command[1:]),
                env=dict(stdio_cfg.env) if stdio_cfg.env else None,
            )

            stack = AsyncExitStack()
            try:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except FileNotFoundError as exc:
                await stack.aclose()
                raise ConfigError(
                    f"toolset {self._toolset_id!r}: stdio command "
                    f"{stdio_cfg.command[0]!r} could not be launched "
                    f"(executable not found on PATH)"
                ) from exc
            except PermissionError as exc:
                await stack.aclose()
                raise ConfigError(
                    f"toolset {self._toolset_id!r}: stdio command "
                    f"{stdio_cfg.command[0]!r} could not be launched "
                    f"(permission denied)"
                ) from exc
            except Exception:
                await stack.aclose()
                raise

            self._stdio_exit_stack = stack
            self._stdio_session = session
            logger.info(
                "Started stdio MCP subprocess for toolset %r", self._toolset_id
            )
            return session

    # ---------- translation ----------------------------------------------

    def _mcp_tool_to_matrix(self, t: mcp_types.Tool) -> Tool:
        return Tool(
            id=t.name,
            description=t.description or "",
            toolset_id=self._toolset_id,
            schema=t.inputSchema or {"type": "object", "properties": {}},
        )

    def _mcp_call_result_to_matrix(
        self,
        r: mcp_types.CallToolResult,
    ) -> ToolCallResult:
        text_chunks: list[str] = []
        non_text: list[dict[str, Any]] = []
        for item in r.content:
            if isinstance(item, mcp_types.TextContent):
                text_chunks.append(item.text)
                continue
            non_text.append(item.model_dump(mode="json"))

        if non_text:
            text_chunks.append(
                "```json\n" + json.dumps(non_text, indent=2) + "\n```"
            )

        extended: dict[str, Any] | None = None
        if non_text:
            extended = {
                "content": [item.model_dump(mode="json") for item in r.content],
            }

        return ToolCallResult(
            output="\n".join(text_chunks),
            is_error=bool(r.isError),
            extended=extended,
        )
