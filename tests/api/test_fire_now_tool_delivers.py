"""``trigger__fire_now`` (the tool) delivers like ``POST /v1/triggers/{id}/fire_now`` (ticket 01a16ddb, lead ruling 1).

Both surfaces fire through :func:`primer.trigger.service.fire_now`; the tool used to build its ``ServiceDeps`` with no workspace registry and no
scheduler (its factory was given none), so every ``agent_fresh_session``, ``graph_fresh_session`` and ``session_append`` subscription answered
``dispatch_failed`` ("requires a workspace_registry") from the tool while the REST route, which threads ``app.state``'s, delivered. The reach and
rank rules the tool applies (#874) are unchanged and pinned where they live (``tests/api/test_session_control_rule_table.py``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from primer.model.storage import OffsetPage
from primer.model.trigger import DelayedTriggerConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.model.workspace_session import AgentSessionBinding, PendingSessionMessage, SessionStatus, WorkspaceSession

ROOT = Path(__file__).resolve().parents[2]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(sp) -> None:
    """A trigger with one ``session_append`` subscription on a session mid-turn, so a delivered fire QUEUES its message (no workspace slot needed)."""
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-busy", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING, turn_status="running", created_at=_now(),
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-tool", slug="tool-fire", name="t", config=DelayedTriggerConfig(fire_at=_now() + timedelta(hours=1)),
        enabled=True, next_fire_at=_now() + timedelta(hours=1), created_at=_now(),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-append", trigger_id="tr-tool", config=SessionAppendSubConfig(session_id="sess-busy"),
        payload_template="fired {{ trigger_slug }}", parallelism="queue", enabled=True, created_at=_now(),
    ))


async def _queued(sp) -> list[PendingSessionMessage]:
    return (await sp.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=50))).items


@pytest.mark.asyncio
async def test_the_tool_delivers_a_session_append_like_the_rest_route(app, client):
    sp = app.state.storage_provider
    await _seed(sp)
    tools = app.state.provider_registry._trigger_toolset_provider

    rest = await client.post("/v1/triggers/tr-tool/fire_now")
    assert rest.status_code == 200, rest.text
    [rest_result] = rest.json()["results"]
    assert rest_result["ok"] is True and rest_result["artefact_id"] == "sess-busy", rest_result

    tool = await tools.call(tool_name="fire_now", arguments={"id": "tr-tool"})
    assert not tool.is_error, tool.output
    [tool_result] = json.loads(tool.output)["results"]
    assert tool_result["ok"] is True, f"the tool's fire did not deliver: {tool_result}"
    assert tool_result["error_code"] is None
    assert tool_result["artefact_id"] == "sess-busy"

    queued = await _queued(sp)
    assert len(queued) == 2, "both fires queue their message on the busy session"
    assert all(m.session_id == "sess-busy" for m in queued)
    assert all(any(part.get("text") == "fired tool-fire" for part in m.parts) for m in queued)
    sub = await sp.get_storage(Subscription).get("sb-append")
    assert sub.last_fire_error is None, "the tool's fire left the subscription a dispatch error"


@pytest.mark.asyncio
async def test_the_tool_and_the_route_hand_the_fire_the_same_collaborators(app, client, monkeypatch):
    """One fire path: both call ``service.fire_now``, and both hand the dispatch the app's workspace registry and scheduler."""
    import primer.trigger.dispatch as dispatch_mod
    from primer.trigger.dispatch import FireResult

    seen: list = []

    async def _spy(*, trigger_id, scheduled_for, deps, extra_context=None):
        seen.append(deps)
        return FireResult(fire_id="fire-x", results=[])

    monkeypatch.setattr(dispatch_mod, "fire_trigger", _spy)
    sp = app.state.storage_provider
    await _seed(sp)

    assert (await client.post("/v1/triggers/tr-tool/fire_now")).status_code == 200
    tool = await app.state.provider_registry._trigger_toolset_provider.call(tool_name="fire_now", arguments={"id": "tr-tool"})
    assert not tool.is_error, tool.output

    rest_deps, tool_deps = seen
    assert rest_deps.workspace_registry is app.state.workspace_registry
    assert tool_deps.workspace_registry is app.state.workspace_registry, "the tool's fire has no workspace registry"
    assert tool_deps.scheduler is rest_deps.scheduler is app.state.scheduler
    assert tool_deps.storage_provider is rest_deps.storage_provider is sp


def _call(path: str, opener: str) -> str:
    """The text of the call that starts with ``opener``, up to its closing parenthesis (its arguments are plain ``name=value`` lines)."""
    src = (ROOT / path).read_text(encoding="utf-8")
    start = src.index(opener)
    return src[start: src.index(")\n", start)]


def test_the_lifespan_hands_the_trigger_toolset_what_the_rest_route_passes():
    """The production wiring: the REST route reads ``app.state``'s claim engine, event bus, scheduler and workspace registry; the toolset is built with the same."""
    call = _call("primer/api/_app_lifespan.py", "trigger_toolset = build_trigger_toolset_provider(")
    for arg in ("claim_engine=claim_engine", "event_bus=event_bus", "scheduler=scheduler", "workspace_registry=workspace_registry"):
        assert arg in call, f"the lifespan builds the trigger toolset without {arg}"


def test_the_test_app_hands_the_trigger_toolset_its_workspace_registry_and_scheduler():
    call = _call("primer/api/app.py", "trigger_toolset = build_trigger_toolset_provider(")
    assert "workspace_registry=workspace_registry" in call
    assert "scheduler=_test_scheduler" in call
