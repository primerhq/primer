"""Fakes for the tests of the tool catalog and ``describe_tool``: a toolset provider over a fixed tool list, and a registry that finds it.

``Scripted`` is a ``ToolsetProvider`` whose roles and pauses are scripted per tool (an undeclared tool fails closed to ``admin``, as a real provider does), that
records the principal each listing was asked for and every call it was asked to run, and that can be made to fail halfway, to never answer, or to ask for OAuth
consent. ``Registry`` resolves a toolset the way ``ProviderRegistry.get_toolset`` does: the scripted ones by id, anything else through a real registry when there
is one, else with the real "does not exist" error.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import AuthRequiredError, NotFoundError
from primer.toolset._describe import make_tool

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}}


def scripted_tool(tool_id: str, toolset_id: str = "rec", **declared: Any) -> Tool:
    """A valid tool descriptor with no examples (``make_tool`` flags such as ``yields`` go in ``declared``)."""
    return make_tool(id=tool_id, toolset_id=toolset_id, purpose="Do it.", when="Use when testing.", args_schema=SCHEMA, examples=[], **declared)


class Scripted(ToolsetProvider):
    """A provider over a fixed tool list.

    ``roles`` answers ``required_role`` (else ``"admin"``), ``yielding`` ``is_yielding``. ``fail_after`` makes the listing raise after that many tools,
    ``block`` makes it wait forever, ``auth`` makes it ask for OAuth consent, ``raises`` makes it raise that exception before any tool. ``listed_as`` is the
    principal of each listing, ``calls`` each call run.
    """

    def __init__(
        self,
        tools: list[Tool],
        *,
        roles: dict[str, str] | None = None,
        yielding: frozenset[str] = frozenset(),
        fail_after: int | None = None,
        block: bool = False,
        auth: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._tools = tools
        self._roles = roles or {}
        self._yielding = yielding
        self._fail_after = fail_after
        self._block = block
        self._auth = auth
        self._raises = raises
        self.listed_as: list[str | None] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        self.listed_as.append(principal)
        if self._block:
            await asyncio.Event().wait()
        if self._raises is not None:
            raise self._raises
        if self._auth:
            raise AuthRequiredError("consent needed", auth_url="https://auth.example/consent", state="state-1")
        for index, tool in enumerate(self._tools):
            if self._fail_after is not None and index >= self._fail_after:
                raise RuntimeError("the listing broke halfway")
            yield tool

    def required_role(self, tool_name: str) -> str:
        return self._roles.get(tool_name, "admin")

    def is_yielding(self, tool_name: str) -> bool:
        return tool_name in self._yielding

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx: Any = None) -> ToolCallResult:
        self.calls.append((tool_name, arguments))
        return ToolCallResult(output="ran", is_error=False)


class Registry:
    """Resolves a toolset by id: the scripted providers first, then ``real`` (a ``ProviderRegistry``) when given, else the real not-found error.

    Every other attribute is the real registry's, so a toolset built over this stand-in can still invalidate and look things up.
    """

    def __init__(self, providers: dict[str, ToolsetProvider], real: Any = None) -> None:
        self._providers = providers
        self._real = real

    async def get_toolset(self, toolset_id: str) -> ToolsetProvider:
        if toolset_id in self._providers:
            return self._providers[toolset_id]
        if self._real is not None:
            return await self._real.get_toolset(toolset_id)
        raise NotFoundError(f"Toolset {toolset_id!r} does not exist")

    def __getattr__(self, name: str) -> Any:
        if self._real is None:
            raise AttributeError(name)
        return getattr(self._real, name)
