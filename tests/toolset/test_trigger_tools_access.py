"""The trigger tools act with the person's access (access-control plan P3 Task 4, Decisions 3 and 4).

The same levels as the REST routes: read lists and gets a trigger and its subscriptions; full updates, fires now and manages
subscriptions; owner deletes and shares. A trigger the caller cannot read answers ``type=trigger_not_found`` (as an unknown
id); too low a level answers ``type=forbidden``. Parking on a trigger (``subscribe_to_trigger``, ``subscribe_to_channel_event``)
needs read on it. The person is the run's initiator, or the MCP request's actor when the call carries no context.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from primer.mcp.server import current_actor
from primer.model.access import Access
from primer.model.channel import Channel
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.trigger import AgentFreshSubConfig, ChannelTriggerConfig, Subscription, Trigger, WebhookTriggerConfig
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset._system_access import access_tools
from primer.toolset.trigger import build_trigger_toolset_provider

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
TID, SID, CHAN = "tr-1", "sb-1", "tr-chan"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ref(uid: str) -> PrincipalRef:
    return PrincipalRef(type="user", id=uid, display=uid, role="admin" if uid == ROOT else "user", source="local", user_id=uid)


def _ctx(uid: str, *, session: str | None = None) -> ToolContext:
    return ToolContext(tool_call_id="tc-1", session_id=session, workspace_id="ws-1", initiated_by=_ref(uid))


@pytest_asyncio.fixture
async def env(tmp_path) -> AsyncIterator[Any]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    for uid in (ALICE, BOB, CAROL, DAVE, ROOT):
        await sp.get_storage(User).create(User(id=uid, username=uid, created_at=NOW, role="admin" if uid == ROOT else "user"))
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-1", template_id="tpl", provider_id="p", created_at=NOW, phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://ws-1", token="t"),
        access=Access(owners=[ALICE], grants={BOB: "full", CAROL: "full", DAVE: "full"}),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s-1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.RUNNING, created_at=NOW,
    ))
    shared = Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"})
    # The channel the chat trigger fires from is readable by everyone: parking on a channel trigger needs READ on its channel (access-control P3 SEC, finding 6).
    await sp.get_storage(Channel).create(Channel(id="ch-pub", provider_id="cp-1", provider="slack", external_id="CPUB", access=Access(public=True)))
    await sp.get_storage(Trigger).create(Trigger(
        id=TID, slug="nightly", name="n", created_at=NOW, owner=_ref(ALICE), config=WebhookTriggerConfig(token="a" * 32), access=shared,
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id=CHAN, slug="chat", name="c", created_at=NOW, owner=_ref(ALICE), config=ChannelTriggerConfig(provider_id="cp-1", channel_id="ch-pub"), access=shared,
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id=SID, trigger_id=TID, created_at=NOW, owner=_ref(ALICE), config=AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag-1"),
    ))
    tools = build_trigger_toolset_provider(storage_provider=sp)
    access = access_tools(storage_provider=sp)

    async def call(tool: str, args: dict, uid: str | None, *, session: str | None = None) -> tuple[bool, dict | list]:
        if tool in access:
            _, handler = access[tool]
            r = await handler(args, ctx=_ctx(uid) if uid else None)
        else:
            r = await tools.call(tool_name=tool, arguments=args, ctx=_ctx(uid, session=session) if uid else None)
        return r.is_error, json.loads(r.output) if r.output else {}

    env = type("Env", (), {})()
    env.sp, env.call = sp, call
    yield env
    await sp.aclose()


_SUB = {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}
_READ = [
    ("get", {"id": TID}),
    ("list_subscriptions", {"trigger_id": TID}),
    ("get_subscription", {"trigger_id": TID, "subscription_id": SID}),
    ("get_entity_access", {"kind": "trigger", "id": TID}),
]
_FULL = [
    ("update", {"id": TID, "name": "renamed"}),
    ("fire_now", {"id": TID}),
    ("create_subscription", {"trigger_id": TID, "config": _SUB}),
    ("update_subscription", {"trigger_id": TID, "subscription_id": SID, "description": "d"}),
    ("delete_subscription", {"trigger_id": TID, "subscription_id": SID}),
]
_OWNER = [
    ("delete", {"id": TID}),
    ("set_entity_access", {"kind": "trigger", "id": TID, "owners": [ALICE], "public": True}),
]


def _ids(doors) -> list[str]:
    return [tool for tool, _ in doors]


def _refused_as_missing(is_err: bool, body: Any) -> bool:
    return is_err and body["type"] in ("trigger_not_found", "not-found")


@pytest.mark.parametrize(("tool", "args"), _READ + _FULL + _OWNER, ids=_ids(_READ + _FULL + _OWNER))
async def test_a_private_trigger_is_invisible_to_a_stranger(env, tool, args):
    is_err, body = await env.call(tool, args, DAVE)
    assert _refused_as_missing(is_err, body), body


@pytest.mark.parametrize(("tool", "args"), _READ, ids=_ids(_READ))
async def test_a_reader_reads(env, tool, args):
    is_err, body = await env.call(tool, args, BOB)
    assert not is_err, body


@pytest.mark.parametrize(("tool", "args"), _FULL + _OWNER, ids=_ids(_FULL + _OWNER))
async def test_a_reader_cannot_write(env, tool, args):
    is_err, body = await env.call(tool, args, BOB)
    assert is_err and body["type"] == "forbidden", body


@pytest.mark.parametrize(("tool", "args"), _READ + _FULL, ids=_ids(_READ + _FULL))
async def test_a_full_grantee_edits_fires_and_manages_subscriptions(env, tool, args):
    is_err, body = await env.call(tool, args, CAROL)
    assert not is_err, body


@pytest.mark.parametrize(("tool", "args"), _OWNER, ids=_ids(_OWNER))
async def test_a_full_grantee_cannot_delete_or_share(env, tool, args):
    is_err, body = await env.call(tool, args, CAROL)
    assert is_err and body["type"] == "forbidden", body


@pytest.mark.parametrize("uid", [ALICE, ROOT])
@pytest.mark.parametrize(("tool", "args"), _READ + _FULL + _OWNER, ids=_ids(_READ + _FULL + _OWNER))
async def test_the_owner_and_an_admin_pass_every_door(env, tool, args, uid):
    is_err, body = await env.call(tool, args, uid)
    assert not is_err, body


async def test_list_only_reaches_readable_triggers(env):
    _, body = await env.call("list", {}, DAVE)
    assert body == []
    _, body = await env.call("list", {}, BOB)
    assert sorted(t["id"] for t in body) == [TID, CHAN]


@pytest.mark.parametrize(("factory", "trigger_id"), [("trigger", TID), ("channel", CHAN)])
async def test_parking_on_a_trigger_needs_read_on_it(env, factory, trigger_id):
    from primer.model.yield_ import Yielded
    from primer.toolset.trigger import _make_subscribe_channel_handler, _make_subscribe_handler

    handler = (_make_subscribe_handler if factory == "trigger" else _make_subscribe_channel_handler)(env.sp)
    refused = await handler({"trigger_id": trigger_id}, ctx=_ctx(DAVE, session="s-1"))
    assert refused.is_error and json.loads(refused.output)["type"] == "trigger_not_found_or_disabled", refused.output
    parked = await handler({"trigger_id": trigger_id}, ctx=_ctx(BOB, session="s-1"))  # a reader parks
    assert isinstance(parked, Yielded), parked


async def test_an_mcp_call_acts_as_the_request_actor(env):
    for uid, readable in ((DAVE, False), (BOB, True)):
        token = current_actor.set(Principal(type="api_token", id="tok", display="t", role="user", source="local", user_id=uid))
        try:
            is_err, body = await env.call("get", {"id": TID}, None)
        finally:
            current_actor.reset(token)
        assert is_err is not readable, body


# --------------------------------------------------------------------------------------------------------------------
# The channel-binding tools of the system toolset: a binding is a subscription, so it inherits its trigger's access
# --------------------------------------------------------------------------------------------------------------------

BIND_TID, BIND_SID = "tr-bind", "sb-bind"


@pytest_asyncio.fixture
async def bindings(env):
    """A channel trigger on a public channel (Task 5's channel READ passes for everyone), shared like tr-1, with one binding."""
    from primer.model.channel import Channel
    from primer.toolset.system import build_system_toolset
    from tests.toolset.test_wiki_tools import _Registry

    sp = env.sp
    await sp.get_storage(Channel).create(Channel(
        id="ch-1", provider_id="cp-1", provider="slack", external_id="C1", access=Access(public=True),
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id=BIND_TID, slug="bound", name="b", created_at=NOW, owner=_ref(ALICE),
        config=ChannelTriggerConfig(provider_id="cp-1", channel_id="ch-1"),
        access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id=BIND_SID, trigger_id=BIND_TID, created_at=NOW, owner=_ref(ALICE),
        config=AgentFreshSubConfig(workspace_id="ws-1", agent_id="ag-1"),
    ))
    system = build_system_toolset(storage_provider=sp, provider_registry=_Registry(None))

    async def call(tool: str, args: dict, uid: str) -> tuple[bool, Any]:
        r = await system.call(tool_name=tool, arguments=args, ctx=_ctx(uid))
        return r.is_error, json.loads(r.output)

    return call


_BIND_READ = [("list_channel_bindings", {"trigger_id": BIND_TID})]
_BIND_FULL = [
    ("create_channel_binding", {"trigger_id": BIND_TID, "config": _SUB}),
    ("delete_channel_binding", {"trigger_id": BIND_TID, "subscription_id": BIND_SID}),
]


@pytest.mark.parametrize(("tool", "args"), _BIND_READ + _BIND_FULL, ids=_ids(_BIND_READ + _BIND_FULL))
async def test_a_binding_tool_on_an_unreadable_trigger_answers_as_a_missing_trigger(bindings, tool, args):
    is_err, body = await bindings(tool, args, DAVE)
    assert _refused_as_missing(is_err, body), body


@pytest.mark.parametrize(("tool", "args"), _BIND_READ, ids=_ids(_BIND_READ))
async def test_a_reader_lists_the_bindings(bindings, tool, args):
    is_err, body = await bindings(tool, args, BOB)
    assert not is_err and [s["id"] for s in body] == [BIND_SID], body


@pytest.mark.parametrize(("tool", "args"), _BIND_FULL, ids=_ids(_BIND_FULL))
async def test_a_reader_cannot_create_or_delete_a_binding(bindings, tool, args):
    is_err, body = await bindings(tool, args, BOB)
    assert is_err and body["type"] == "forbidden", body


@pytest.mark.parametrize(("tool", "args"), _BIND_READ + _BIND_FULL, ids=_ids(_BIND_READ + _BIND_FULL))
async def test_a_full_grantee_manages_the_bindings(bindings, tool, args):
    is_err, body = await bindings(tool, args, CAROL)
    assert not is_err, body
