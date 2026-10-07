"""Stop (interrupt) as a system tool: ONE code path with the REST route (task 01a10871).

``POST /v1/workspaces/{wid}/sessions/{sid}/interrupt`` and ``workspaces__interrupt_workspace_session`` both call
``primer.workspace.session_factory.interrupt_session`` (the route's body, moved there as Cancel's was). So this file runs the SAME table
of session states through BOTH surfaces and requires the same outcome from each: what the answer is, whether a Stop is recorded on the
row, and whether the bus key went out. The six route tests in ``test_session_interrupt.py`` are unchanged and stay the proof that the
move kept the route's behaviour.

The tool is built the way the app builds it, over the same storage and bus as the route, with NO scheduler and NO claim engine: a Stop
needs neither, so the tool must not answer "unavailable" the way the session-creating tools do.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

import primer.observability.metrics as metrics
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.toolset.workspaces import build_workspaces_toolset

WID = "ws-stop-parity"
TOOL = "interrupt_workspace_session"


def _now() -> datetime:
    return datetime(2026, 10, 7, 10, 0, 0, tzinfo=timezone.utc)


async def _seed(storage_provider, *, sid: str, status: SessionStatus, **fields) -> None:
    values = dict(
        id=sid,
        workspace_id=WID,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason="completed" if status == SessionStatus.ENDED else None,
        ended_at=_now() if status == SessionStatus.ENDED else None,
        last_seq=1,
        turn_status="running" if status == SessionStatus.RUNNING else "idle",
        created_at=_now(),
    )
    row = WorkspaceSession(**{**values, **fields})
    await storage_provider.get_storage(WorkspaceSession).create(row)


@pytest.fixture
def toolset(app, fake_storage_provider):
    return build_workspaces_toolset(
        storage_provider=fake_storage_provider,
        workspace_registry=app.state.workspace_registry,
        scheduler=None,
        claim_engine=None,
        event_bus=app.state.event_bus,
    )


@pytest.fixture
def published(app, monkeypatch) -> list[str]:
    """The keys published on the app's bus (the real publish still runs)."""
    keys: list[str] = []
    real = app.state.event_bus.publish

    async def record(key, payload, *args, **kwargs):
        keys.append(key)
        return await real(key, payload, *args, **kwargs)

    monkeypatch.setattr(app.state.event_bus, "publish", record)
    return keys


@dataclass
class _Outcome:
    kind: str                 # "ok" | "conflict" | "not-found"
    detail: str               # the refusal text ("" for ok)
    flag: bool | None         # interrupt_requested in the ANSWER (None for a refusal)


async def _stop(surface: str, client, toolset, sid: str, *, workspace_id: str = WID) -> _Outcome:
    if surface == "route":
        resp = await client.post(f"/v1/workspaces/{workspace_id}/sessions/{sid}/interrupt", json={})
        if resp.status_code == 200:
            return _Outcome("ok", "", resp.json()["interrupt_requested"])
        kind = {409: "conflict", 404: "not-found"}[resp.status_code]
        return _Outcome(kind, resp.json().get("detail", ""), None)
    result = await toolset.call(tool_name=TOOL, arguments={"workspace_id": workspace_id, "session_id": sid})
    body = json.loads(result.output)
    if result.is_error:
        return _Outcome(body["type"], body["message"], None)
    return _Outcome("ok", "", body["interrupt_requested"])


SURFACES = pytest.mark.parametrize("surface", ["route", "tool"])

# id -> (status, row fields, expected kind, a Stop is recorded, the bus key went out, fragments the refusal must carry)
SCENARIOS = {
    "running": (SessionStatus.RUNNING, {}, "ok", True, True, ()),
    "queued": (SessionStatus.RUNNING, {"turn_status": "claimable"}, "ok", True, True, ()),
    "waiting": (SessionStatus.WAITING, {}, "ok", False, False, ()),
    "created": (SessionStatus.CREATED, {}, "ok", False, False, ()),
    "paused": (SessionStatus.PAUSED, {}, "ok", False, False, ()),
    "ended": (SessionStatus.ENDED, {}, "conflict", False, False, ("has ended",)),
    "parked": (SessionStatus.RUNNING, {"parked_status": "parked"}, "conflict", False, False, ("no turn is running", "Cancel")),
    "resuming": (
        SessionStatus.RUNNING, {"parked_status": "resumable"}, "conflict", False, False,
        ("resuming", "Stop is not available during a resume", "Cancel"),
    ),
}


@SURFACES
@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_the_same_states_give_the_same_stop_through_the_route_and_the_tool(
    surface, scenario, client, toolset, fake_storage_provider, published,
):
    status, fields, kind, recorded, publishes, fragments = SCENARIOS[scenario]
    sid = f"s-{scenario}"
    await _seed(fake_storage_provider, sid=sid, status=status, **fields)

    outcome = await _stop(surface, client, toolset, sid)

    assert outcome.kind == kind, outcome
    for fragment in fragments:
        assert fragment in outcome.detail, (fragment, outcome.detail)
    stored = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert stored.interrupt_requested is recorded, "a Stop is recorded on the row exactly when a turn was running to stop"
    assert published == ([f"session:{sid}:cancel"] if publishes else [])
    if kind == "ok":
        assert outcome.flag is recorded, "the ANSWER says whether a Stop was recorded: interrupt_requested false = nothing recorded"
    if recorded:
        assert stored.cancel_requested_at is not None and stored.cancel_requested is False, "a Stop is not a Cancel"


@SURFACES
@pytest.mark.parametrize("case", ["unknown", "other-workspace"])
async def test_an_unknown_session_or_the_wrong_workspace_is_not_found(
    surface, case, client, toolset, fake_storage_provider, published,
):
    await _seed(fake_storage_provider, sid="s-here", status=SessionStatus.RUNNING)
    sid, wid = ("s-nope", WID) if case == "unknown" else ("s-here", "ws-elsewhere")

    outcome = await _stop(surface, client, toolset, sid, workspace_id=wid)

    assert outcome.kind == "not-found"
    assert sid in outcome.detail
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("s-here")).interrupt_requested is False
    assert published == []


@SURFACES
async def test_a_failed_publish_is_still_ok_counted_and_logged_through_both(
    surface, client, toolset, fake_storage_provider, app, monkeypatch, caplog,
):
    metrics.reset_for_test()
    await _seed(fake_storage_provider, sid="s-down", status=SessionStatus.RUNNING)

    async def broken_publish(*_a, **_k):
        raise RuntimeError("bus is down")

    monkeypatch.setattr(app.state.event_bus, "publish", broken_publish)

    with caplog.at_level(logging.WARNING):
        outcome = await _stop(surface, client, toolset, "s-down")

    assert outcome.kind == "ok" and outcome.flag is True
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("s-down")).interrupt_requested is True
    assert metrics.session_interrupt_publish_failures_total._value.get() == 1.0
    assert any("s-down" in r.getMessage() and "bus is down" in r.getMessage() for r in caplog.records)


async def test_the_tool_works_with_no_bus_at_all_the_flag_is_the_durable_record(app, fake_storage_provider):
    """A deployment without a bus still records the Stop on the row, which the worker polls."""
    await _seed(fake_storage_provider, sid="s-nobus", status=SessionStatus.RUNNING)
    bare = build_workspaces_toolset(
        storage_provider=fake_storage_provider, workspace_registry=app.state.workspace_registry,
        scheduler=None, claim_engine=None, event_bus=None,
    )

    result = await bare.call(tool_name=TOOL, arguments={"workspace_id": WID, "session_id": "s-nobus"})

    assert not result.is_error, result.output
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("s-nobus")).interrupt_requested is True


async def test_a_session_can_stop_itself_the_tool_does_not_special_case_the_caller(toolset, fake_storage_provider, published):
    """D4 of the design: a self-stop is allowed. The call comes from inside the running turn (a ToolContext whose session is the
    target) and records a Stop like any other; what the turn does about it is the loop's business (see the loop-level test)."""
    from primer.model.yield_ import ToolContext

    await _seed(fake_storage_provider, sid="s-self", status=SessionStatus.RUNNING)
    ctx = ToolContext(tool_call_id="call-1", session_id="s-self", workspace_id=WID)

    result = await toolset.call(
        tool_name=TOOL, arguments={"workspace_id": WID, "session_id": "s-self"}, ctx=ctx,
    )

    assert not result.is_error, result.output
    assert json.loads(result.output)["interrupt_requested"] is True
    assert published == ["session:s-self:cancel"]


async def test_the_route_and_the_tool_call_the_one_shared_helper(client, toolset, fake_storage_provider, monkeypatch):
    """One implementation, not two: both surfaces reach ``session_factory.interrupt_session`` (imported at call time, as Cancel's)."""
    import primer.workspace.session_factory as session_factory

    await _seed(fake_storage_provider, sid="s-one", status=SessionStatus.RUNNING)
    real = session_factory.interrupt_session
    calls: list[tuple[str, str]] = []

    async def spy(*, workspace_id, session_id, deps):
        calls.append((workspace_id, session_id))
        return await real(workspace_id=workspace_id, session_id=session_id, deps=deps)

    monkeypatch.setattr(session_factory, "interrupt_session", spy)

    await _stop("route", client, toolset, "s-one")
    await _stop("tool", client, toolset, "s-one")

    assert calls == [(WID, "s-one"), (WID, "s-one")]
