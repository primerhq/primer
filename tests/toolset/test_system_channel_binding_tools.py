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

from primer.model.access import Access
from primer.api.registries import ProviderRegistry
from primer.model.channel import Channel
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.storage import CursorPageResponse, OffsetPageResponse
from primer.model.trigger import (
    ChannelTriggerConfig,
    Subscription,
    Trigger,
    WebhookTriggerConfig,
)
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)
from primer.model.yield_ import ToolContext
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

    async def update_unless(
        self,
        e,
        *,
        field,
        forbidden,
        conn=None,
    ):
        current = self._data.get(e.id)
        if current is None:
            raise NotFoundError(f"no entity with id {e.id!r}")
        if getattr(current, field, None) == forbidden:
            return None
        self._data[e.id] = e
        return e

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        """The storage contract's guarded field write, with the reference semantics (the reply-binding tools write the binding only)."""
        from tests.storage._patch_reference import patch_model

        current = self._data.get(id)
        if current is None:
            raise NotFoundError(f"no entity with id {id!r}")
        updated = patch_model(current, patch, where=where, set_paths=set_paths)
        if updated is not None:
            self._data[id] = updated
        return updated

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
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

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
    return Channel(access=Access(public=True), 
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
                "kind": "session_append",
                "session_id": "sess-1",
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
async def test_create_channel_binding_refuses_dm_sender(sp: _SP, system_toolset):
    """Ticket 01a16e37-b98e, ruling 2: no channel can post to a person's direct messages, so the target is refused by name."""
    await _seed_channel_trigger(sp)

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
            "reply_target": "dm_sender",
        },
    )

    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "validation-error"
    assert "reply_target" in payload["message"] and "direct messages" in payload["message"]
    assert not sp.get_storage(Subscription)._data


@pytest.mark.asyncio
async def test_create_channel_binding_explicit_target_validates_the_channel(sp: _SP, system_toolset):
    """Round 2 of #932, item 3: an explicit ``{channel_id, anchor}`` target names a real channel, and a Discord anchor is a numeric id."""
    from primer.model.channel import ChannelProviderType

    await _seed_channel_trigger(sp)
    await sp.get_storage(Channel).create(_make_channel("chan-x"))
    discord = Channel(access=Access(public=True), 
        id="chan-dc",
        provider_id="cp-dc",
        provider=ChannelProviderType.DISCORD,
        external_id="500",
    )
    await sp.get_storage(Channel).create(discord)

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
            "reply_target": {"channel_id": "chan-missing", "anchor": "1700000000.000100"},
        },
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "not-found"
    assert "reply_target" in payload["message"] and "chan-missing" in payload["message"]
    assert not sp.get_storage(Subscription)._data

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
            "reply_target": {"channel_id": "chan-dc", "anchor": "not-a-snowflake"},
        },
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "validation-error"
    assert "reply_target" in payload["message"] and "numeric" in payload["message"]
    assert not sp.get_storage(Subscription)._data

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
            "reply_target": {"channel_id": "chan-dc", "anchor": "1234567890123"},
        },
    )
    assert not result.is_error, result.output
    subs = list(sp.get_storage(Subscription)._data.values())
    assert len(subs) == 1
    assert subs[0].reply_target is not None and subs[0].reply_target.channel_id == "chan-dc"


@pytest.mark.asyncio
async def test_create_channel_binding_unknown_trigger_returns_trigger_not_found(
    sp: _SP, system_toolset
):
    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "no-such-trigger",
            "config": {"kind": "session_append", "session_id": "sess-1"},
        },
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "trigger_not_found"


@pytest.mark.asyncio
async def test_create_channel_binding_rejects_pathological_text_pattern(
    sp: _SP, system_toolset
):
    await _seed_channel_trigger(sp)

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "event_matcher": {
                "event_type": "message.posted",
                "text_pattern": r"(a+)+$",
            },
            "config": {"kind": "session_append", "session_id": "sess-1"},
        },
    )

    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "validation-error"
    assert "nested unbounded repetition" in payload["message"]
    assert not sp.get_storage(Subscription)._data


@pytest.mark.asyncio
async def test_create_channel_binding_refuses_a_non_channel_trigger(
    sp: _SP, system_toolset
):
    """The tool says the trigger must be a channel trigger; the handler enforces that
    before anything is stored."""
    await sp.get_storage(Trigger).create(
        Trigger(
            id="trg-wh-1",
            slug="webhook-trigger",
            name="Webhook trigger",
            config=WebhookTriggerConfig(),
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-wh-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
        },
    )

    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "validation-error"
    assert payload["message"].startswith("trigger_id:")
    assert "a channel binding needs a channel trigger" in payload["message"]
    assert not sp.get_storage(Subscription)._data


@pytest.mark.asyncio
async def test_create_channel_binding_refuses_a_parked_session_config(
    sp: _SP, system_toolset
):
    """A parked_session row is only created by the subscribe_to_trigger yielding tool;
    the handler answers the service's refusal by name instead of letting it escape."""
    await _seed_channel_trigger(sp)

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {
                "kind": "parked_session",
                "session_id": "sess-1",
                "tool_call_id": "tcl-1",
                "parked_at": "2026-01-01T00:00:00Z",
            },
        },
    )

    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "validation-error"
    assert payload["message"].startswith("config:")
    assert (
        "subscriptions of kind 'parked_session' are only created via "
        "the subscribe_to_trigger yielding tool" in payload["message"]
    )
    assert not sp.get_storage(Subscription)._data


@pytest.mark.asyncio
async def test_create_channel_binding_refuses_a_bad_parallelism(
    sp: _SP, system_toolset
):
    """#915 types every create path's parallelism argument as the skip/queue
    literal; this pins the refusal on this tool: a bad value answers
    validation-error and nothing is stored."""
    await _seed_channel_trigger(sp)

    result = await system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
            "parallelism": "bogus",
        },
    )

    assert result.is_error
    payload = json.loads(result.output)
    assert payload["type"] == "validation-error"
    assert "parallelism" in payload["message"]
    assert not sp.get_storage(Subscription)._data


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
            "config": {"kind": "session_append", "session_id": "sess-1"},
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


def _principal(uid: str, role: str = "user") -> PrincipalRef:
    return PrincipalRef(
        type="user", id=uid, display=uid, role=role, source="local"
    )  # type: ignore[arg-type]


def _ctx(initiated_by: PrincipalRef) -> ToolContext:
    return ToolContext(
        tool_call_id="tc-owner-view",
        session_id=None,
        workspace_id=None,
        initiated_by=initiated_by,
    )


def _create_binding(system_toolset, owner: PrincipalRef):
    return system_toolset.call(
        tool_name="create_channel_binding",
        arguments={
            "trigger_id": "trg-ch-1",
            "config": {"kind": "session_append", "session_id": "sess-1"},
        },
        ctx=_ctx(owner),
    )


@pytest.mark.asyncio
async def test_list_channel_bindings_hides_owner_details_from_a_non_manager(
    sp: _SP, system_toolset
):
    """A-20: a caller who does not manage the binding sees only the
    owner's display name and role, like trigger__list_subscriptions."""
    await _seed_channel_trigger(sp)
    await _create_binding(system_toolset, _principal("u-owner"))

    result = await system_toolset.call(
        tool_name="list_channel_bindings",
        arguments={"trigger_id": "trg-ch-1"},
        ctx=_ctx(_principal("u-other")),
    )
    assert not result.is_error, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["owner"] == {"display": "u-owner", "role": "user"}
    assert "id" not in rows[0]["owner"]
    assert "type" not in rows[0]["owner"]


@pytest.mark.asyncio
async def test_list_channel_bindings_owner_sees_the_full_owner(
    sp: _SP, system_toolset
):
    await _seed_channel_trigger(sp)
    owner = _principal("u-owner")
    await _create_binding(system_toolset, owner)

    result = await system_toolset.call(
        tool_name="list_channel_bindings",
        arguments={"trigger_id": "trg-ch-1"},
        ctx=_ctx(owner),
    )
    assert not result.is_error, result.output
    rows = json.loads(result.output)
    assert rows[0]["owner"]["id"] == "u-owner"
    assert rows[0]["owner"]["type"] == "user"


@pytest.mark.asyncio
async def test_list_channel_bindings_admin_sees_the_full_owner(
    sp: _SP, system_toolset
):
    await _seed_channel_trigger(sp)
    await _create_binding(system_toolset, _principal("u-owner"))

    result = await system_toolset.call(
        tool_name="list_channel_bindings",
        arguments={"trigger_id": "trg-ch-1"},
        ctx=_ctx(_principal("u-admin", role="admin")),
    )
    assert not result.is_error, result.output
    rows = json.loads(result.output)
    assert rows[0]["owner"]["id"] == "u-owner"


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
            "config": {"kind": "session_append", "session_id": "sess-1"},
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
