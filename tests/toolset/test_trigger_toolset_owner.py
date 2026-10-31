"""The trigger tools record the caller's identity as the owner (security review A-20, ticket 01a16ee4).

A fired run is ranked by its owner, so a run that creates or re-saves a trigger or subscription through a tool stamps
``ctx.initiated_by`` as the owner, the same way the REST routes stamp the request's actor. The MCP endpoint's own call (no
``ToolContext`` and no delegated run) stamps the MCP person's actor (``current_actor``), so what it saves fires at most at the
person's rank, and a later save by a lower rank is refused by the rank rule. A call with no identity at all (a delegated run,
a subagent started over MCP) records no owner, which fires as an ordinary user (fail closed).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from primer.api.registries import ProviderRegistry
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext
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


async def _seed_ws1(provider) -> None:
    """The subscription's target workspace (access-control P2 T6): the save path refuses for an owner that holds no full there."""
    from primer.model.access import Access
    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

    await provider.get_storage(Workspace).create(Workspace(
        id="ws-1", name="ws-1", template_id="tpl", provider_id="local-1", created_at=datetime.now(timezone.utc),
        phase="running", runtime_meta=WorkspaceRuntimeMeta(url="ws://ws-1", token="t"),
        access=Access(owners=["user-1"]),
    ))


async def test_the_trigger_tools_record_and_refresh_the_owner(fake_storage_provider) -> None:
    # The save asks the subscription owner's full on the target workspace (access-control P2 T6): user-1 owns ws-1.
    await _seed_ws1(fake_storage_provider)
    await fake_storage_provider.set_default_workspace_id(caller().workspace_id)
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
    stored = await fake_storage_provider.get_storage(Trigger).get(tid)
    assert stored.owner is None
    # Nobody known owns nothing (access-control P3 Task 4): the row is admin-only, so the same caller cannot subscribe to it.
    assert stored.access == Access()
    sub = await tools.call(tool_name="create_subscription", arguments={"trigger_id": tid, "config": SUB_CONFIG}, ctx=None)
    assert sub.is_error and json.loads(sub.output)["type"] == "trigger_not_found", sub.output


async def test_the_mcp_door_records_the_mcp_principal_as_owner(fake_storage_provider) -> None:
    """Ticket 01a16ee4: the MCP endpoint's own call (no ``ToolContext``) records the MCP person's actor, so what it saves fires at
    most at the person's rank. A call inside a delegated run (a subagent started over MCP, ``_RUN_ID`` set) is not the MCP person
    and records no owner."""
    from primer.agent.invoke import _RUN_ID
    from primer.mcp.server import current_actor
    from primer.model.principal import Principal

    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    actor = Principal(type="user", id="u-mcp", display="mcp", role="user", source="local", user_id="u-mcp")
    token = current_actor.set(actor)
    try:
        created = await tools.call(tool_name="create", arguments=_create_args(), ctx=None)
        assert not created.is_error, created.output
        tid = json.loads(created.output)["id"]

        assert _owner(await fake_storage_provider.get_storage(Trigger).get(tid)) == ("user", "u-mcp", "user")

        # The delegated run acts with the MCP person's access (it owns the trigger) but records no run-as owner.
        _RUN_ID.set("run-1")
        try:
            upd = await tools.call(tool_name="update", arguments={"id": tid, "name": "by a delegated run"}, ctx=None)
        finally:
            _RUN_ID.set(None)
    finally:
        current_actor.reset(token)
    assert not upd.is_error, upd.output
    assert (await fake_storage_provider.get_storage(Trigger).get(tid)).owner is None, "a delegated run is not the MCP person"


async def test_create_channel_binding_records_the_owner(fake_storage_provider) -> None:
    # The save asks the caller's full on the target workspace (access-control P2 T6): user-1 owns ws-1.
    await _seed_ws1(fake_storage_provider)
    await fake_storage_provider.set_default_workspace_id(caller().workspace_id)
    # The trigger fires from chan-1, a channel the caller holds full on (access P3, decision 5 refuses the admin-only provider-wide form).
    from primer.model.access import Access
    from primer.model.channel import Channel

    await fake_storage_provider.get_storage(Channel).create(Channel(
        id="chan-1", provider_id="cp-1", provider="slack", external_id="C12345",
        access=Access(owners=["user-1"]),
    ))
    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="trg-ch-1", slug="ch-trigger", name="ch", created_at=datetime.now(timezone.utc),
        config=ChannelTriggerConfig(provider_id="cp-1", channel_id="chan-1"),
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


async def _admin_webhook(tools, sp) -> tuple[str, str]:
    created = await tools.call(
        tool_name="create", arguments={"slug": "tool-hook", "name": "hook", "config": {"kind": "webhook"}}, ctx=caller("admin"),
    )
    assert not created.is_error, created.output
    body = json.loads(created.output)
    # Shared publicly: these tests are about who sees the token and the owner's details, not who may read the row (P3 Task 4).
    triggers = sp.get_storage(Trigger)
    row = await triggers.get(body["id"])
    await triggers.update(row.model_copy(update={"access": Access(owners=row.access.owners, grants=row.access.grants, public=True)}))
    return body["id"], body["config"]["token"]


async def test_the_get_and_list_tools_mask_the_token_for_anyone_but_its_owner_or_an_admin(fake_storage_provider) -> None:
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    tid, token = await _admin_webhook(tools, fake_storage_provider)

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
    tid, token = await _admin_webhook(tools, fake_storage_provider)
    before = await fake_storage_provider.get_storage(Trigger).get(tid)

    # Another user (it only reads the publicly shared trigger): an update needs full on it.
    other_user = ToolContext(
        tool_call_id="tc", session_id="s", workspace_id="w",
        initiated_by=PrincipalRef(type="user", id="user-2", display="user-2", role="user", source="local"),
    )
    result = await tools.call(tool_name="update", arguments={"id": tid, "name": "mine"}, ctx=other_user)

    assert result.is_error and json.loads(result.output).get("type") == "forbidden", result.output
    assert token not in result.output
    after = await fake_storage_provider.get_storage(Trigger).get(tid)
    assert (after.name, after.config.token, after.owner) == (before.name, token, before.owner)


# ---------------------------------------------------------------------------
# Lead review of #491, round 3
# ---------------------------------------------------------------------------


async def test_an_admin_update_that_echoes_the_token_it_read_still_gets_a_new_token(fake_storage_provider) -> None:
    from primer.model.principal import PrincipalRef
    from primer.model.yield_ import ToolContext

    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    created = await tools.call(
        tool_name="create", arguments={"slug": "user-hook", "name": "hook", "config": {"kind": "webhook"}}, ctx=caller("user"),
    )
    tid, token = json.loads(created.output)["id"], json.loads(created.output)["config"]["token"]
    other_admin = ToolContext(
        tool_call_id="tc", session_id="s", workspace_id="w",
        initiated_by=PrincipalRef(type="user", id="admin-2", display="admin-2", role="admin", source="local"),
    )

    read = json.loads((await tools.call(tool_name="get", arguments={"id": tid}, ctx=other_admin)).output)
    assert read["config"]["token"] == token
    updated = await tools.call(tool_name="update", arguments={"id": tid, "config": read["config"]}, ctx=other_admin)

    assert not updated.is_error, updated.output
    stored = await fake_storage_provider.get_storage(Trigger).get(tid)
    assert stored.owner.id == "admin-2"
    assert stored.config.token != token and len(stored.config.token) == 32


async def test_two_unattributed_callers_are_not_the_same_owner(fake_storage_provider) -> None:
    """``PrincipalRef.unattributed()`` is a stand-in for "nobody known", not an identity: one unattributed run may not see or
    change what another saved."""
    from primer.model.principal import PrincipalRef
    from primer.model.yield_ import ToolContext
    from primer.trigger.owner import may_manage_trigger, same_owner

    nobody = ToolContext(tool_call_id="tc", session_id="s", workspace_id="w", initiated_by=PrincipalRef.unattributed())
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    created = await tools.call(
        tool_name="create", arguments={"slug": "nobody-hook", "name": "hook", "config": {"kind": "webhook"}}, ctx=nobody,
    )
    assert not created.is_error, created.output
    tid = json.loads(created.output)["id"]
    # Shared publicly: nobody known owns nothing, so the row is readable only once it is public (access-control P3 Task 4).
    triggers = fake_storage_provider.get_storage(Trigger)
    await triggers.update((await triggers.get(tid)).model_copy(update={"access": Access(public=True)}))
    stored = await triggers.get(tid)

    assert not await same_owner(PrincipalRef.unattributed(), PrincipalRef.unattributed(), fake_storage_provider)
    assert not await may_manage_trigger(stored, PrincipalRef.unattributed(), fake_storage_provider)
    got = json.loads((await tools.call(tool_name="get", arguments={"id": tid}, ctx=nobody)).output)
    assert got["config"]["token"] == MASK
    updated = await tools.call(tool_name="update", arguments={"id": tid, "name": "x"}, ctx=nobody)
    assert updated.is_error and json.loads(updated.output).get("type") == "forbidden", updated.output


async def test_the_get_tool_shows_a_non_manager_only_the_owner_display_and_role(fake_storage_provider) -> None:
    tools = build_trigger_toolset_provider(storage_provider=fake_storage_provider)
    tid, _ = await _admin_webhook(tools, fake_storage_provider)

    got = json.loads((await tools.call(tool_name="get", arguments={"id": tid}, ctx=caller("user", kind="api_token"))).output)
    listed = json.loads((await tools.call(tool_name="list", arguments={}, ctx=None)).output)

    assert got["owner"] == {"display": "user", "role": "admin"}
    assert [r["owner"] for r in listed if r["id"] == tid] == [{"display": "user", "role": "admin"}]
