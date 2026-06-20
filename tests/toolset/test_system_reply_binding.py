"""Tests for workspace reply-binding tools in the system toolset."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.api.registries import ProviderRegistry
from primer.model.channel import Channel
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import CursorPageResponse, OffsetPageResponse
from primer.model.workspace import Workspace, WorkspaceChannelLink, WorkspaceRuntimeMeta
from primer.toolset.system import build_system_toolset


# ===========================================================================
# In-memory fakes (mirrors test_system.py)
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str) -> Any | None:
        return self._data.get(id)

    async def create(self, e: Any) -> Any:
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e: Any) -> Any:
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id: str) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        from primer.model.storage import OffsetPage

        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor = (
            str(offset + page.length) if offset + page.length < len(items) else None
        )
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls: type) -> _Storage:
        return self._stores.setdefault(cls, _Storage())

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def pr(sp: _SP) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def system_toolset(sp: _SP, pr: ProviderRegistry):
    provider = build_system_toolset(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
    )
    pr._system_toolset_provider = provider  # type: ignore[attr-defined]
    return provider


def _make_workspace(ws_id: str = "ws-1") -> Workspace:
    return Workspace(
        id=ws_id,
        template_id="tpl-1",
        provider_id="wp-1",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://localhost:5959",
            token=SecretStr("tok"),
        ),
    )


def _make_channel(ch_id: str = "chan-1") -> Channel:
    return Channel(
        id=ch_id,
        provider_id="cp-1",
        provider="slack",
        external_id="C12345",
    )


# ===========================================================================
# Catalog checks
# ===========================================================================


@pytest.mark.asyncio
async def test_set_workspace_reply_binding_is_exposed(system_toolset):
    tools = {t.id async for t in system_toolset.list_tools()}
    assert "set_workspace_reply_binding" in tools


@pytest.mark.asyncio
async def test_clear_workspace_reply_binding_is_exposed(system_toolset):
    tools = {t.id async for t in system_toolset.list_tools()}
    assert "clear_workspace_reply_binding" in tools


@pytest.mark.asyncio
async def test_old_channel_association_tools_are_not_exposed(system_toolset):
    """The renamed-away association tools must not appear."""
    tools = {t.id async for t in system_toolset.list_tools()}
    assert "set_workspace_channel_association" not in tools
    assert "clear_workspace_channel_association" not in tools


# ===========================================================================
# Functional tests
# ===========================================================================


@pytest.mark.asyncio
async def test_set_workspace_reply_binding_persists(sp: _SP, system_toolset):
    ws = _make_workspace()
    ch = _make_channel()
    await sp.get_storage(Workspace).create(ws)
    await sp.get_storage(Channel).create(ch)

    result = await system_toolset.call(
        tool_name="set_workspace_reply_binding",
        arguments={"workspace_id": "ws-1", "channel_id": "chan-1"},
    )
    assert not result.is_error, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["workspace_id"] == "ws-1"
    assert payload["channel_id"] == "chan-1"

    stored = await sp.get_storage(Workspace).get("ws-1")
    assert stored is not None
    assert stored.reply_binding is not None
    assert stored.reply_binding.channel_id == "chan-1"


@pytest.mark.asyncio
async def test_set_workspace_reply_binding_unknown_workspace(sp: _SP, system_toolset):
    result = await system_toolset.call(
        tool_name="set_workspace_reply_binding",
        arguments={"workspace_id": "no-such-ws", "channel_id": "chan-1"},
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "not-found"


@pytest.mark.asyncio
async def test_set_workspace_reply_binding_unknown_channel(sp: _SP, system_toolset):
    ws = _make_workspace("ws-2")
    await sp.get_storage(Workspace).create(ws)

    result = await system_toolset.call(
        tool_name="set_workspace_reply_binding",
        arguments={"workspace_id": "ws-2", "channel_id": "no-such-channel"},
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "not-found"


@pytest.mark.asyncio
async def test_clear_workspace_reply_binding_removes_link(sp: _SP, system_toolset):
    ws = _make_workspace("ws-3")
    ws = ws.model_copy(
        update={"reply_binding": WorkspaceChannelLink(channel_id="chan-x")}
    )
    await sp.get_storage(Workspace).create(ws)

    result = await system_toolset.call(
        tool_name="clear_workspace_reply_binding",
        arguments={"workspace_id": "ws-3"},
    )
    assert not result.is_error, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["workspace_id"] == "ws-3"

    stored = await sp.get_storage(Workspace).get("ws-3")
    assert stored is not None
    assert stored.reply_binding is None


@pytest.mark.asyncio
async def test_clear_workspace_reply_binding_unknown_workspace(sp: _SP, system_toolset):
    result = await system_toolset.call(
        tool_name="clear_workspace_reply_binding",
        arguments={"workspace_id": "no-such-ws"},
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "not-found"
