"""The trigger tools record the calling run's identity as the owner (security review A-20).

A fired run is ranked by its owner, so a run that creates or re-saves a trigger or subscription through a tool stamps
``ctx.initiated_by`` as the owner, the same way the REST routes stamp the request's actor. A call with no identity (the MCP
endpoint dispatches handlers without a ``ToolContext``) records no owner, which fires as an ordinary user (fail closed).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from primer.api.registries import ProviderRegistry
from primer.model.trigger import ChannelTriggerConfig, Subscription, Trigger
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from tests._support.caller import caller

SUB_CONFIG = {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}


def _create_args() -> dict:
    fire_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return {"slug": "tool-owned", "name": "Tool owned", "config": {"kind": "delayed", "fire_at": fire_at}}


def _owner(row) -> tuple:
    return (row.owner.type, row.owner.id, row.owner.role) if row.owner is not None else None


async def test_the_trigger_tools_record_and_refresh_the_owner(fake_storage_provider) -> None:
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    as_user, as_admin = caller("user"), caller("admin")

    created = await tools.call(tool_name="create", arguments=_create_args(), ctx=as_user)
    assert not created.is_error, created.output
    tid = json.loads(created.output)["id"]
    sub = await tools.call(
        tool_name="create_subscription", arguments={"trigger_id": tid, "config": SUB_CONFIG}, ctx=as_user,
    )
    assert not sub.is_error, sub.output
    sid = json.loads(sub.output)["id"]

    triggers, subs = fake_storage_provider.get_storage(Trigger), fake_storage_provider.get_storage(Subscription)
    assert _owner(await triggers.get(tid)) == ("user", "user-1", "user")
    assert _owner(await subs.get(sid)) == ("user", "user-1", "user")

    upd = await tools.call(tool_name="update", arguments={"id": tid, "name": "re-saved"}, ctx=as_admin)
    assert not upd.is_error, upd.output
    upd_sub = await tools.call(
        tool_name="update_subscription",
        arguments={"trigger_id": tid, "subscription_id": sid, "description": "re-saved"},
        ctx=as_admin,
    )
    assert not upd_sub.is_error, upd_sub.output

    assert _owner(await triggers.get(tid)) == ("user", "user-1", "admin")
    assert _owner(await subs.get(sid)) == ("user", "user-1", "admin")


async def test_a_call_without_an_identity_records_no_owner(fake_storage_provider) -> None:
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)

    created = await tools.call(tool_name="create", arguments=_create_args(), ctx=None)
    assert not created.is_error, created.output
    tid = json.loads(created.output)["id"]
    sub = await tools.call(tool_name="create_subscription", arguments={"trigger_id": tid, "config": SUB_CONFIG}, ctx=None)
    assert not sub.is_error, sub.output

    assert (await fake_storage_provider.get_storage(Trigger).get(tid)).owner is None
    assert (await fake_storage_provider.get_storage(Subscription).get(json.loads(sub.output)["id"])).owner is None


async def test_create_channel_binding_records_the_owner(fake_storage_provider) -> None:
    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="trg-ch-1", slug="ch-trigger", name="ch", created_at=datetime.now(timezone.utc),
        config=ChannelTriggerConfig(provider_id="cp-1"),
    ))
    registry = ProviderRegistry(
        fake_storage_provider, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )
    system = build_system_toolset(storage_provider=fake_storage_provider, provider_registry=registry)

    result = await system.call(
        tool_name="create_channel_binding",
        arguments={"trigger_id": "trg-ch-1", "config": SUB_CONFIG},
        ctx=caller("user"),
    )
    assert not result.is_error, result.output

    sub = await fake_storage_provider.get_storage(Subscription).get(json.loads(result.output)["id"])
    assert _owner(sub) == ("user", "user-1", "user")


# ---------------------------------------------------------------------------
# The webhook token through the tools (lead review of #491, round 2)
# ---------------------------------------------------------------------------

MASK = "•••redacted•••"


async def _admin_webhook(tools) -> tuple[str, str]:
    created = await tools.call(
        tool_name="create", arguments={"slug": "tool-hook", "name": "hook", "config": {"kind": "webhook"}}, ctx=caller("admin"),
    )
    assert not created.is_error, created.output
    body = json.loads(created.output)
    return body["id"], body["config"]["token"]


async def test_the_get_and_list_tools_mask_the_token_for_anyone_but_its_owner_or_an_admin(fake_storage_provider) -> None:
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    tid, token = await _admin_webhook(tools)

    def _token_of(result) -> str:
        body = json.loads(result.output)
        if isinstance(body, dict) and "items" in body:
            body = body["items"]
        rows = body if isinstance(body, list) else [body]
        return next(r["config"]["token"] for r in rows if r["id"] == tid)

    # caller("user") shares the admin caller's id: the same identity at a lower rank (a capped run) is not the owner.
    for ctx, expected in ((caller("admin"), token), (caller("user"), MASK), (caller("user", kind="api_token"), MASK), (None, MASK)):
        got = await tools.call(tool_name="get", arguments={"id": tid}, ctx=ctx)
        listed = await tools.call(tool_name="list", arguments={}, ctx=ctx)
        assert not got.is_error and not listed.is_error, (got.output, listed.output)
        assert _token_of(got) == expected, ctx
        assert _token_of(listed) == expected, ctx


async def test_a_user_run_cannot_update_an_admin_webhook_trigger(fake_storage_provider) -> None:
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    tid, token = await _admin_webhook(tools)
    before = await fake_storage_provider.get_storage(Trigger).get(tid)

    result = await tools.call(tool_name="update", arguments={"id": tid, "name": "mine"}, ctx=caller("user"))

    assert result.is_error and json.loads(result.output).get("type") == "forbidden", result.output
    assert token not in result.output
    after = await fake_storage_provider.get_storage(Trigger).get(tid)
    assert (after.name, after.config.token, after.owner) == (before.name, token, before.owner)
