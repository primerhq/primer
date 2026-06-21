"""Tests for the channel-binding management tools in the system toolset.

Covers the inbound channel-binding subscription tools
(``create_channel_binding`` / ``list_channel_bindings`` /
``delete_channel_binding``) and the renamed outbound reply-binding tools
(``set_reply_binding`` / ``clear_reply_binding``).
"""

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
from primer.model.trigger import (
    ChannelTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)
from primer.toolset.system import build_system_toolset


# ===========================================================================
# In-memory fakes (mirrors test_system_reply_binding.py)
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


async def _seed_channel_trigger(sp: _SP, trigger_id: str = "trg-ch-1") -> Trigger:
    trigger = Trigger(
        id=trigger_id,
        slug="ch-trigger",
        name="Channel trigger",
        config=ChannelTriggerConfig(provider_id="slack-1", channel_id="chan-1"),
        created_at=datetime.now(timezone.utc),
    )
    await sp.get_storage(Trigger).create(trigger)
    return trigger


# ===========================================================================
# create_channel_binding
# ===========================================================================


@pytest.mark.asyncio
async def test_create_channel_binding_creates_subscription_on_channel_trigger(
    sp: _SP, system_toolset
):
    await _seed_channel_trigger(sp)

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "event_matcher": {
                "event_type": "command.invoked",
                "command_name": "deploy",
            },
            "config": {
                "kind": "start_chat",
                "agent_id": "deployer",
            },
            "reply_target": "source_thread",
        },
    )
    assert not result.is_error, result.output

    subs = list(sp.get_storage(Subscription)._data.values())
    assert len(subs) == 1
    sub = subs[0]
    assert sub.trigger_id == "trg-ch-1"
    assert sub.event_matcher is not None
    assert sub.event_matcher.command_name == "deploy"
    assert sub.reply_target is not None


@pytest.mark.asyncio
async def test_create_channel_binding_unknown_trigger_returns_trigger_not_found(
    sp: _SP, system_toolset
):
    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "no-such-trigger",
            "config": {"kind": "start_chat", "agent_id": "deployer"},
        },
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "trigger_not_found"


# ===========================================================================
# list_channel_bindings
# ===========================================================================


@pytest.mark.asyncio
async def test_list_channel_bindings_returns_subscriptions(sp: _SP, system_toolset):
    await _seed_channel_trigger(sp)
    await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "start_chat", "agent_id": "deployer"},
        },
    )

    result = await system_toolset.call(
        tool_name="list_channel_bindings",
        arguments={"trigger_id": "trg-ch-1"},
    )
    assert not result.is_error, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1


@pytest.mark.asyncio
async def test_list_channel_bindings_unknown_trigger_404(sp: _SP, system_toolset):
    result = await system_toolset.call(
        tool_name="list_channel_bindings",
        arguments={"trigger_id": "no-such-trigger"},
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "trigger_not_found"


# ===========================================================================
# delete_channel_binding
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_channel_binding_removes_subscription(sp: _SP, system_toolset):
    await _seed_channel_trigger(sp)
    create_result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "start_chat", "agent_id": "deployer"},
        },
    )
    sub_id = json.loads(create_result.output)["id"]

    result = await system_toolset.call(
        tool_name="delete_channel_binding",
        arguments={"trigger_id": "trg-ch-1", "subscription_id": sub_id},
    )
    assert not result.is_error, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert sp.get_storage(Subscription)._data == {}


@pytest.mark.asyncio
async def test_delete_channel_binding_unknown_returns_subscription_not_found(
    sp: _SP, system_toolset
):
    await _seed_channel_trigger(sp)
    result = await system_toolset.call(
        tool_name="delete_channel_binding",
        arguments={"trigger_id": "trg-ch-1", "subscription_id": "no-such-sub"},
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "subscription_not_found"


# ===========================================================================
# Renamed reply-binding tools
# ===========================================================================


@pytest.mark.asyncio
async def test_reply_binding_tools_are_exposed(system_toolset):
    tools = {t.id async for t in system_toolset.list_tools()}
    assert "set_reply_binding" in tools
    assert "clear_reply_binding" in tools


@pytest.mark.asyncio
async def test_set_reply_binding_writes_reply_binding_field(sp: _SP, system_toolset):
    await sp.get_storage(Workspace).create(_make_workspace())
    await sp.get_storage(Channel).create(_make_channel())

    result = await system_toolset.call(
        tool_name="set_reply_binding",
        arguments={
            "workspace_id": "ws-1",
            "channel_id": "chan-1",
            "anchor": "thread-99",
        },
    )
    assert not result.is_error, result.output

    stored = await sp.get_storage(Workspace).get("ws-1")
    assert stored.reply_binding is not None
    assert stored.reply_binding.channel_id == "chan-1"
    assert stored.reply_binding.anchor == "thread-99"


@pytest.mark.asyncio
async def test_clear_reply_binding_nulls_it(sp: _SP, system_toolset):
    ws = _make_workspace("ws-3")
    ws = ws.model_copy(
        update={"reply_binding": WorkspaceChannelLink(channel_id="chan-x")}
    )
    await sp.get_storage(Workspace).create(ws)

    result = await system_toolset.call(
        tool_name="clear_reply_binding",
        arguments={"workspace_id": "ws-3"},
    )
    assert not result.is_error, result.output

    stored = await sp.get_storage(Workspace).get("ws-3")
    assert stored.reply_binding is None
