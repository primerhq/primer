"""A trigger-fired run is ranked as the people who set it up, never as an internal admin (security review A-20).

Before the fix every fired run carried ``PrincipalRef(type="trigger", role=None)`` and :func:`primer.authz._role_allows` waved
the ``trigger`` type through every role floor. The triggers, agents and toolsets routers are open to ``role=user``, so an
ordinary account could build an agent whose tools include ``system__create_toolset``, subscribe it to a trigger, fire it, and
have the run create a stdio MCP toolset (a command on the host).

These tests drive the real fresh-session subscribers, read the session row they create, and hand that row's ``initiated_by``
to a :class:`ToolExecutionManager` holding the real system toolset, the way the worker's executor builder does
(``primer/worker/executor_builders.py``). The run calls ``system__create_toolset`` with a stdio toolset.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.model.api_token import ApiToken
from primer.model.chat import ToolCallPart
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset
from primer.model.trigger import Subscription, Trigger
from primer.model.user import User
from primer.model.workspace_session import WorkspaceSession
from primer.toolset.system import build_system_toolset
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.agent_fresh_session import AgentFreshSessionDispatcher
from primer.trigger.subscribers.graph_fresh_session import GraphFreshSessionDispatcher

STDIO = {"id": "ts-pwn", "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["/bin/sh", "-c", "id"]}}}


def _ref(uid: str, role: str | None, *, kind: str = "user") -> PrincipalRef:
    return PrincipalRef(type=kind, id=uid, display=uid, role=role, source="local")  # type: ignore[arg-type]


async def _seed_user(sp, uid: str, role: str, *, disabled: bool = False) -> PrincipalRef:
    await sp.get_storage(User).create(
        User(id=uid, username=uid, created_at=datetime.now(timezone.utc), role=role, disabled=disabled),
    )
    return _ref(uid, role)


async def _seed(sp, *, kind: str, target_id: str, trigger_owner, sub_owner) -> Subscription:
    """Write the trigger and subscription rows as stored documents; ``None`` owner = a row saved before owners were recorded."""
    now = datetime.now(timezone.utc).isoformat()
    trigger = Trigger.model_validate({
        "id": "tr-own-1", "slug": "own-trigger", "name": "own", "created_at": now,
        "config": {"kind": "delayed", "fire_at": now},
        "owner": trigger_owner.model_dump(mode="json") if trigger_owner else None,
    })
    await sp.get_storage(Trigger).create(trigger)
    config = (
        {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": target_id}
        if kind == "agent"
        else {"kind": "graph_fresh_session", "workspace_id": "ws-1", "graph_id": target_id}
    )
    sub = Subscription.model_validate({
        "id": "sb-own-1", "trigger_id": trigger.id, "config": config, "parallelism": "queue", "created_at": now,
        "owner": sub_owner.model_dump(mode="json") if sub_owner else None,
    })
    await sp.get_storage(Subscription).create(sub)
    return sub


async def _fire(sp, sub: Subscription, deps: DispatchDeps, *, kind: str) -> WorkspaceSession:
    dispatcher = AgentFreshSessionDispatcher() if kind == "agent" else GraphFreshSessionDispatcher()
    res = await dispatcher.dispatch(
        sub,
        rendered_payload="go" if kind == "agent" else json.dumps({"x": 1}),
        fire_context={"trigger_id": sub.trigger_id, "fired_at": "2026-10-08T09:00:00+00:00"},
        fire_id="fire-own-1",
        deps=deps,
    )
    assert res.ok, res
    session = await sp.get_storage(WorkspaceSession).get(res.artefact_id)
    assert session is not None
    return session


async def _run_creates_stdio_toolset(sp, session: WorkspaceSession):
    """The fired run's tool call, through the manager the worker builds from the session row."""
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )
    manager = ToolExecutionManager(
        toolset_providers={"system": build_system_toolset(storage_provider=sp, provider_registry=registry)},
        workspace_session=SimpleNamespace(session_id=session.id, workspace_id=session.workspace_id),  # type: ignore[arg-type]
        tools=["system__create_toolset"],
        initiated_by=session.initiated_by,
    )
    await manager.list_tools()
    return await manager.execute(ToolCallPart(id="call-1", name="system__create_toolset", arguments={"entity": STDIO}))


def _refused(result) -> bool:
    """Refused by the handler (``type=forbidden``) or by the tool manager's role floor: either way in-band and nothing ran."""
    if not result.error:
        return False
    try:
        return json.loads(result.output).get("type") == "forbidden"
    except (TypeError, ValueError):
        return "access denied" in result.output


@pytest.fixture
def deps(fake_storage_provider, fake_claim_engine, fake_scheduler, fake_workspace_registry) -> DispatchDeps:
    return DispatchDeps(
        storage_provider=fake_storage_provider, claim_engine=fake_claim_engine,
        scheduler=fake_scheduler, workspace_registry=fake_workspace_registry,
    )


@pytest.fixture
def target(request, seeded_agent, seeded_graph):
    return ("agent", seeded_agent.id) if request.param == "agent" else ("graph", seeded_graph.id)


both_kinds = pytest.mark.parametrize("target", ["agent", "graph"], indirect=True)


@both_kinds
async def test_a_user_owned_trigger_run_cannot_create_a_stdio_toolset(fake_storage_provider, deps, seeded_workspace, target):
    kind, target_id = target
    owner = await _seed_user(fake_storage_provider, "u-plain", "user")
    sub = await _seed(fake_storage_provider, kind=kind, target_id=target_id, trigger_owner=owner, sub_owner=owner)

    session = await _fire(fake_storage_provider, sub, deps, kind=kind)
    result = await _run_creates_stdio_toolset(fake_storage_provider, session)

    assert _refused(result), result.output
    assert await fake_storage_provider.get_storage(Toolset).get("ts-pwn") is None, "the refused toolset was written"


@both_kinds
async def test_the_run_is_attributed_to_the_owner_and_keeps_the_trigger_provenance(
    fake_storage_provider, deps, seeded_workspace, target,
):
    kind, target_id = target
    owner = await _seed_user(fake_storage_provider, "u-plain", "user")
    sub = await _seed(fake_storage_provider, kind=kind, target_id=target_id, trigger_owner=owner, sub_owner=owner)

    session = await _fire(fake_storage_provider, sub, deps, kind=kind)

    assert session.initiated_by is not None
    assert (session.initiated_by.type, session.initiated_by.id, session.initiated_by.role) == ("user", "u-plain", "user")
    assert session.metadata["trigger_id"] == "tr-own-1"
    assert session.metadata["subscription_id"] == "sb-own-1"
    assert session.metadata["fire_id"] == "fire-own-1"


@both_kinds
async def test_an_admin_owned_trigger_run_still_creates_a_stdio_toolset(fake_storage_provider, deps, seeded_workspace, target):
    kind, target_id = target
    owner = await _seed_user(fake_storage_provider, "u-admin", "admin")
    sub = await _seed(fake_storage_provider, kind=kind, target_id=target_id, trigger_owner=owner, sub_owner=owner)

    session = await _fire(fake_storage_provider, sub, deps, kind=kind)
    result = await _run_creates_stdio_toolset(fake_storage_provider, session)

    assert not result.error, result.output
    assert await fake_storage_provider.get_storage(Toolset).get("ts-pwn") is not None


@both_kinds
async def test_a_legacy_ownerless_trigger_run_is_refused(fake_storage_provider, deps, seeded_workspace, target):
    kind, target_id = target
    sub = await _seed(fake_storage_provider, kind=kind, target_id=target_id, trigger_owner=None, sub_owner=None)

    session = await _fire(fake_storage_provider, sub, deps, kind=kind)
    result = await _run_creates_stdio_toolset(fake_storage_provider, session)

    assert session.initiated_by is not None and session.initiated_by.role == "user", session.initiated_by
    assert _refused(result), result.output
    assert await fake_storage_provider.get_storage(Toolset).get("ts-pwn") is None


async def test_a_user_subscription_on_an_admin_trigger_is_refused(fake_storage_provider, deps, seeded_workspace, seeded_agent):
    admin = await _seed_user(fake_storage_provider, "u-admin", "admin")
    user = await _seed_user(fake_storage_provider, "u-plain", "user")
    sub = await _seed(fake_storage_provider, kind="agent", target_id=seeded_agent.id, trigger_owner=admin, sub_owner=user)

    result = await _run_creates_stdio_toolset(fake_storage_provider, await _fire(fake_storage_provider, sub, deps, kind="agent"))

    assert _refused(result), result.output


async def test_an_admin_subscription_on_a_user_trigger_is_ranked_by_the_lower_of_the_two(
    fake_storage_provider, deps, seeded_workspace, seeded_agent,
):
    admin = await _seed_user(fake_storage_provider, "u-admin", "admin")
    user = await _seed_user(fake_storage_provider, "u-plain", "user")
    sub = await _seed(fake_storage_provider, kind="agent", target_id=seeded_agent.id, trigger_owner=user, sub_owner=admin)

    session = await _fire(fake_storage_provider, sub, deps, kind="agent")
    result = await _run_creates_stdio_toolset(fake_storage_provider, session)

    assert session.initiated_by is not None and session.initiated_by.id == "u-admin"
    assert _refused(result), result.output


async def test_a_demoted_admin_is_ranked_by_the_role_they_hold_at_fire_time(
    fake_storage_provider, deps, seeded_workspace, seeded_agent,
):
    """The owner's role is re-read from the User row at fire time, not trusted from the snapshot taken at save time."""
    await _seed_user(fake_storage_provider, "u-was-admin", "user")
    stale = _ref("u-was-admin", "admin")
    sub = await _seed(fake_storage_provider, kind="agent", target_id=seeded_agent.id, trigger_owner=stale, sub_owner=stale)

    result = await _run_creates_stdio_toolset(fake_storage_provider, await _fire(fake_storage_provider, sub, deps, kind="agent"))

    assert _refused(result), result.output


@pytest.mark.parametrize("disabled", [False, True], ids=["deleted", "disabled"])
async def test_an_owner_that_cannot_be_resolved_fails_closed(
    fake_storage_provider, deps, seeded_workspace, seeded_agent, disabled,
):
    if disabled:
        await _seed_user(fake_storage_provider, "u-gone", "admin", disabled=True)
    stale = _ref("u-gone", "admin")
    sub = await _seed(fake_storage_provider, kind="agent", target_id=seeded_agent.id, trigger_owner=stale, sub_owner=stale)

    session = await _fire(fake_storage_provider, sub, deps, kind="agent")
    result = await _run_creates_stdio_toolset(fake_storage_provider, session)

    assert session.initiated_by is not None and session.initiated_by.role == "user"
    assert _refused(result), result.output


async def test_an_api_token_owner_is_ranked_by_the_token_owner_role(fake_storage_provider, deps, seeded_workspace, seeded_agent):
    await _seed_user(fake_storage_provider, "u-admin", "admin")
    await fake_storage_provider.get_storage(ApiToken).create(ApiToken(
        id="tok-1", user_id="u-admin", name="ci", token_hash="a" * 64, prefix="abcdefgh",
        created_at=datetime.now(timezone.utc),
    ))
    owner = PrincipalRef(type="api_token", id="tok-1", display="ci", role="admin", source="internal")
    sub = await _seed(fake_storage_provider, kind="agent", target_id=seeded_agent.id, trigger_owner=owner, sub_owner=owner)

    session = await _fire(fake_storage_provider, sub, deps, kind="agent")
    result = await _run_creates_stdio_toolset(fake_storage_provider, session)

    assert (session.initiated_by.type, session.initiated_by.id) == ("api_token", "tok-1")
    assert not result.error, result.output


async def test_a_revoked_api_token_owner_fails_closed(fake_storage_provider, deps, seeded_workspace, seeded_agent):
    await _seed_user(fake_storage_provider, "u-admin", "admin")
    await fake_storage_provider.get_storage(ApiToken).create(ApiToken(
        id="tok-1", user_id="u-admin", name="ci", token_hash="a" * 64, prefix="abcdefgh",
        created_at=datetime.now(timezone.utc), revoked_at=datetime.now(timezone.utc),
    ))
    owner = PrincipalRef(type="api_token", id="tok-1", display="ci", role="admin", source="internal")
    sub = await _seed(fake_storage_provider, kind="agent", target_id=seeded_agent.id, trigger_owner=owner, sub_owner=owner)

    result = await _run_creates_stdio_toolset(fake_storage_provider, await _fire(fake_storage_provider, sub, deps, kind="agent"))

    assert _refused(result), result.output
