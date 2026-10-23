"""One rule table governs every path that drives another session (lead rulings 1 to 3 of the G7 fixes; audit G7-01, G7-05, G7-06).

A path drives a session when it puts input into it; the session then runs a turn at its creator's rank. The table in
``docs/dev/subsystems/sessions.md`` (section 9, "One rule table") is pinned here row by row: every path runs every scenario, through the code a real
caller reaches it by:

* the three in-session tools (``message_session``, and ``steer_workspace_session`` and ``restart_workspace_session`` called from a session) through
  :func:`primer.agent.inner_dispatch.dispatch_inner_tool`, the way ``system__call_tool`` reaches a tool;
* the steer and restart tools with no calling session through the same dispatch with no context and the request's actor on ``current_actor``, as the
  MCP endpoint calls them;
* the REST steer and restart routes over HTTP, through the auth middleware, as a logged-in user-role account;
* the trigger's ``session_append`` dispatcher.

The actor ranks ``user`` on every path. Scenarios: the target was started by an admin (``outranked``); by a user (``same rank``); it is in another
workspace than the sending session's (``out of reach``; the sender is not in the default workspace); an operator cancelled it (``cancelled``); the
sending session's turn is already 8 hops from a person (``hop limit``). A refused drive writes nothing to the target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

import primer.trigger.subscribers.session_append as sa
from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.trigger import SessionAppendSubConfig, Subscription, Trigger
from primer.model.user import User
from primer.model.workspace_session import AgentSessionBinding, SessionStatus
from primer.model.yield_ import ToolContext
from primer.session.peer_messages import MAX_PEER_HOPS
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from primer.trigger.subscribers import DispatchDeps
from tests.session._peer_fakes import FakeBus, FakeEngine, FakeRegistry, FakeScheduler, World, make_world, seed_session

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")

D, F, N, C, H, S = "delivered", "forbidden", "not-found", "conflict", "hop-limit", "skipped"

#: path -> (kind of drive, what each scenario answers). The columns are the table's.
TABLE: dict[str, tuple[str, dict[str, str]]] = {
    "message_session":            ("steer", {"outranked": F, "same rank": D, "out of reach": N, "cancelled": C, "hop limit": H}),
    "steer tool, in a session":   ("steer", {"outranked": F, "same rank": D, "out of reach": N, "cancelled": C, "hop limit": H}),
    "restart tool, in a session": ("restart", {"outranked": F, "same rank": D, "out of reach": N, "cancelled": C, "hop limit": H}),
    "steer tool, no session":     ("steer", {"outranked": F, "same rank": D, "out of reach": D, "cancelled": C, "hop limit": D}),
    "restart tool, no session":   ("restart", {"outranked": F, "same rank": D, "out of reach": D, "cancelled": C, "hop limit": D}),
    "REST steer":                 ("steer", {"outranked": F, "same rank": D, "out of reach": D, "cancelled": D, "hop limit": D}),
    "REST restart":               ("restart", {"outranked": F, "same rank": D, "out of reach": D, "cancelled": D, "hop limit": D}),
    "trigger session_append":     ("steer", {"outranked": F, "same rank": D, "out of reach": D, "cancelled": S, "hop limit": D}),
}
SCENARIOS = ["outranked", "same rank", "out of reach", "cancelled", "hop limit"]
IN_SESSION = {"message_session", "steer tool, in a session", "restart tool, in a session"}


@dataclass
class Env:
    world: World
    rest: httpx.AsyncClient | None
    target_workspace: str


async def _seed(world: World, path: str, scenario: str) -> str:
    """The sending session ``ses-a`` (a user's agent session in ``ws-1``) and the target ``ses-t``; returns the target's workspace."""
    kind = TABLE[path][0]
    await seed_session(
        world, sid="ses-a", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER,
        peer_hops=MAX_PEER_HOPS if scenario == "hop limit" else 0, created_at=NOW,
    )
    workspace = "ws-2" if scenario == "out of reach" else "ws-1"
    creator = ADMIN if scenario == "outranked" else USER
    if scenario == "cancelled":
        state = {"status": SessionStatus.ENDED, "ended_reason": "cancelled"}
    elif kind == "restart":
        state = {"status": SessionStatus.ENDED, "ended_reason": "completed"}
    else:
        state = {"status": SessionStatus.WAITING}
    await seed_session(world, sid="ses-t", workspace_id=workspace, initiated_by=creator, turn_no=1, created_at=NOW, **state)
    return workspace


async def _rest_env(app, admin, sp, path: str, scenario: str) -> Env:
    """The app over the shared fake storage, its registry answering with the world's workspaces, and a user-role account logged in."""
    world = World(sp=sp, registry=FakeRegistry(), scheduler=FakeScheduler(), engine=FakeEngine(), bus=FakeBus())
    app.state.workspace_registry.get_workspace = world.registry.get_workspace  # type: ignore[method-assign]
    r = await admin.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    login = await client.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})
    assert login.status_code == 200, login.text
    return Env(world=world, rest=client, target_workspace=await _seed(world, path, scenario))


def _tool_outcome(result) -> tuple[str, dict]:
    body = json.loads(result.output)
    return (D if not result.is_error else body["type"]), body


async def _drive(env: Env, path: str, session_id: str = "ses-t") -> tuple[str, dict]:
    world, ws = env.world, env.target_workspace
    if path == "REST steer" or path == "REST restart":
        verb, payload = ("steer", {"instruction": "hello"}) if path == "REST steer" else ("restart", {"input": "hello"})
        r = await env.rest.post(f"/v1/workspaces/{ws}/sessions/{session_id}/{verb}", json=payload)
        return {200: D, 403: F, 404: N, 409: C}[r.status_code], r.json()
    if path == "trigger session_append":
        await world.sp.get_storage(User).create(User(id="u-owner", username="owner", created_at=NOW, role="user"))
        owner = PrincipalRef(type="user", id="u-owner", display="owner", role="user", source="local").model_dump(mode="json")
        await world.sp.get_storage(Trigger).create(Trigger.model_validate({
            "id": "tr-1", "slug": "drive-table", "name": "t", "created_at": NOW.isoformat(), "config": {"kind": "delayed", "fire_at": NOW.isoformat()},
            "owner": owner,
        }))
        sub = Subscription.model_validate({
            "id": "sb-1", "trigger_id": "tr-1", "config": SessionAppendSubConfig(session_id=session_id).model_dump(mode="json"),
            "parallelism": "queue", "created_at": NOW.isoformat(), "owner": owner,
        })
        res = await sa.SessionAppendDispatcher().dispatch(
            sub, rendered_payload="hello", fire_context={}, fire_id="fire-1",
            deps=DispatchDeps(storage_provider=world.sp, claim_engine=world.engine, scheduler=world.scheduler,
                              workspace_registry=world.registry, event_bus=world.bus),
        )
        if res.ok and not res.skipped:
            return D, {}
        return {"steer_outranks_fire": F, "skipped_session_ended": S}.get(res.error_code, res.error_code), {}
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    if path == "message_session":
        tool, arguments = "message_session", {"session_id": session_id, "text": "hello"}
    elif path.startswith("steer"):
        tool, arguments = "steer_workspace_session", {"workspace_id": ws, "session_id": session_id, "instruction": "hello"}
    else:
        tool, arguments = "restart_workspace_session", {"workspace_id": ws, "session_id": session_id, "input": "hello"}
    if path in IN_SESSION:
        ctx = ToolContext(tool_call_id="call-1", session_id="ses-a", workspace_id="ws-1", initiated_by=USER)
        return _tool_outcome(await dispatch_inner_tool(
            provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=ctx,
        ))
    token = current_actor.set(Principal(type="user", id="u-mcp", display="mcp", role="user", source="local"))
    try:
        return _tool_outcome(await dispatch_inner_tool(
            provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=None,
        ))
    finally:
        current_actor.reset(token)


async def _check(env: Env, path: str, scenario: str) -> None:
    try:
        before = (await env.world.sessions.get("ses-t")).model_dump(mode="json")

        outcome, body = await _drive(env, path)

        assert outcome == TABLE[path][1][scenario], (path, scenario, body)
        after = await env.world.sessions.get("ses-t")
        if outcome == D:
            assert after.status == SessionStatus.RUNNING and after.turn_status == "claimable", (path, scenario)
        else:
            assert after.model_dump(mode="json") == before, f"{path} wrote to the target it refused ({scenario})"
        if outcome == N:
            # Out of reach answers exactly as an id that does not exist, so the id is never confirmed.
            _, unknown = await _drive(env, path, session_id="ses-nobody")
            assert json.dumps(body).replace("ses-t", "<id>") == json.dumps(unknown).replace("ses-nobody", "<id>"), (body, unknown)
    finally:
        if env.rest is not None:
            await env.rest.aclose()


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("path", [p for p in TABLE if not p.startswith("REST")])
async def test_every_tool_and_trigger_path_obeys_the_rule_table(path, scenario):
    world = make_world(with_bus=True)
    await _check(Env(world=world, rest=None, target_workspace=await _seed(world, path, scenario)), path, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("path", [p for p in TABLE if p.startswith("REST")])
async def test_every_rest_path_obeys_the_rule_table(app, client, fake_storage_provider, path, scenario):
    await _check(await _rest_env(app, client, fake_storage_provider, path, scenario), path, scenario)
