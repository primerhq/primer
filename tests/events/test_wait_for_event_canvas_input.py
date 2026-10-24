"""A non-admin ``wait_for_event`` gets a ``canvas.event`` without what the user ENTERED (audit G8-08, the lead's ruling D).

``wait_for_event`` is the other door of the event log a non-admin reaches: the event that wakes the wait becomes the run's tool result
(its transcript, a shared read) and, between the wake and the resume, sits on the waiting session's row (``parked_state``, served by
``GET /v1/sessions/{id}``). A non-admin canvas wait must name its own workspace or session (#856), and the default workspace is shared,
so such a wait can wake on another user's interaction. The rule is the feed's: a non-admin gets the event without the ``value`` and the
``data`` of its payload, whoever's session it is, and an admin gets the full event. The dispatcher drops them BEFORE it publishes the
wake, so neither the row nor the result ever holds them. It keeps them only for a park that asked to (``keep_input`` in the park's
``resume_metadata``: an admin's ``wait_for_event``, and ``canvas_wait``, which reads the calling session's own canvas); a park that does
not say so, from an older build for one, gets the event without them (fail closed).

A non-admin canvas wait may not match on the input either (``payload.value``, ``payload.data`` or the whole ``payload``, also through the
payload fallback, ``value``): the wait that wakes or not would tell the run what was entered.

Everything runs on the real SQLite storage: the real tool handler, the park the worker writes, ``CanvasService.record_event`` appending
the real ``canvas.event``, the real dispatcher and the registered resume hook.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.canvas.lang import parse_program
from primer.canvas.service import CanvasService
from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventFilter, EventSubscription, SessionWakeSink
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext, Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.events_wait import make_wait_for_event_handler
from primer.worker.yield_resume_registry import get_resume_hook

#: The bound on every test body.
BODY_TIMEOUT_S = 60

WORKSPACE = "w"
WAITER = "sess-1"          # the session that waits
OTHER = "s-admin"          # another session of the same (shared) workspace
ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")
INPUT = ("value", "data")

PROGRAM = """
root = Column([genre, viz])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
viz = Html("<div id=b>pick one</div>", 60)
"""
#: The interactions, each with what the user entered.
INTERACTIONS = {
    "a select change": {"element": "genre", "kind": "change", "value": "Lo-fi"},
    "an html emit": {"element": "viz", "kind": "emit", "name": "submit", "data": {"note": "what the admin typed"}},
}


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _ctx(who: PrincipalRef) -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id=WAITER, workspace_id=WORKSPACE, initiated_by=who)


async def _canvas(sp, session_id: str) -> CanvasService:
    # No V8 for a body without a script: the script check is not what is under test here.
    service = CanvasService(sp, script_check=lambda body: [])
    await service.apply(
        session_id=session_id, workspace_id=WORKSPACE, statements=parse_program(PROGRAM), unit="line", base_rev=None,
        source="agent", by="agent",
    )
    return service


async def _park(sp, yielded: Yielded, *, resume_metadata: dict[str, Any] | None = None) -> None:
    """The park row the worker writes after the tool returns (``parked_state.yielded`` is the stamped Yielded)."""
    now = datetime.now(UTC)
    yielded_blob = {**yielded.to_jsonable(), "tool_name": "wait_for_event"}
    if resume_metadata is not None:
        yielded_blob["resume_metadata"] = resume_metadata
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=WAITER, workspace_id=WORKSPACE, binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=now, initiated_by=USER, parked_status="parked", parked_event_key=yielded.event_key, parked_at=now,
        parked_state={"tool_call_id": "call-1", "yielded": yielded_blob},
    ))


async def _wake(sp, yielded: Yielded, *, session_id: str, interaction: dict[str, Any], service: CanvasService) -> dict[str, Any]:
    """Record ``interaction`` on ``session_id``'s canvas, drain the dispatcher once and return the envelope it published for the park."""
    bus = _Bus()
    dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
    await dispatcher.drain_once()                                     # the subscription's cursor is pinned at the head already
    await service.record_event(session_id=session_id, workspace_id=WORKSPACE, by="admin", **interaction)
    assert await dispatcher.drain_once() == 1
    [(key, envelope)] = bus.published
    assert key == yielded.event_key
    return envelope


def _result_event(yielded: Yielded, envelope: dict[str, Any]) -> dict[str, Any]:
    out = get_resume_hook("wait_for_event")(yielded.resume_metadata, envelope, None)
    assert not out.is_error, out.output
    return json.loads(out.output)["event"]


async def _wait(sp, who: PrincipalRef, scope: dict[str, str]) -> Yielded:
    result = await make_wait_for_event_handler(sp)({"event_types": ["canvas.event"], "fields": [scope]}, ctx=_ctx(who))
    assert isinstance(result, Yielded), result
    return result


WORKSPACE_SCOPE = {"path": "workspace_id", "op": "eq", "value": WORKSPACE}
SESSION_SCOPE = {"path": "session_id", "op": "eq", "value": WAITER}


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_a_non_admin_wait_on_its_workspace_gets_another_sessions_canvas_event_without_its_input(sp, interaction: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        service = await _canvas(sp, OTHER)
        yielded = await _wait(sp, USER, WORKSPACE_SCOPE)
        await _park(sp, yielded)

        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction], service=service)

        assert (envelope["event_type"], envelope["session_id"]) == ("canvas.event", OTHER)
        assert not set(INPUT) & set(envelope["payload"]), f"the wake carried the input: {envelope['payload']}"
        assert envelope["payload"]["element"] == INTERACTIONS[interaction]["element"] and envelope["payload"]["by"] == "admin"
        assert envelope["payload"].get("name") == INTERACTIONS[interaction].get("name")
        event = _result_event(yielded, envelope)
        assert not set(INPUT) & set(event["payload"]), "the tool result carried the input"


async def test_a_non_admin_wait_on_its_own_session_is_stripped_too(sp) -> None:
    """One rule, no ownership check: the run's own session's canvas is read through the canvas tools, not through the event log."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        service = await _canvas(sp, WAITER)
        yielded = await _wait(sp, USER, SESSION_SCOPE)
        await _park(sp, yielded)

        envelope = await _wake(sp, yielded, session_id=WAITER, interaction=INTERACTIONS["an html emit"], service=service)

        assert envelope["session_id"] == WAITER and not set(INPUT) & set(envelope["payload"])
        assert envelope["payload"]["name"] == "submit"


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_an_admin_wait_gets_the_value_and_the_data(sp, interaction: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        service = await _canvas(sp, OTHER)
        yielded = await _wait(sp, ADMIN, WORKSPACE_SCOPE)
        await _park(sp, yielded)

        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction], service=service)

        entered = {k: v for k, v in INTERACTIONS[interaction].items() if k in INPUT}
        assert {k: envelope["payload"][k] for k in entered} == entered
        assert {k: _result_event(yielded, envelope)["payload"][k] for k in entered} == entered


async def test_a_park_that_does_not_ask_for_the_input_does_not_get_it(sp) -> None:
    """Fail closed: a park whose metadata does not say ``keep_input`` (a wait from an older build, a session_wake an admin made over
    REST) gets the event without the input, even when an admin's run made it."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        service = await _canvas(sp, OTHER)
        yielded = await _wait(sp, ADMIN, WORKSPACE_SCOPE)
        await _park(sp, yielded, resume_metadata={"subscription_id": yielded.resume_metadata["subscription_id"]})

        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS["a select change"], service=service)

        assert "value" not in envelope["payload"]


async def test_a_wake_of_any_other_kind_is_delivered_whole(sp) -> None:
    """Only the input keys of a ``canvas.event`` are dropped: a session event keeps its payload for a non-admin's wait."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        result = await make_wait_for_event_handler(sp)(
            {"event_types": ["session.messaged"], "fields": [WORKSPACE_SCOPE]}, ctx=_ctx(USER),
        )
        assert isinstance(result, Yielded)
        await _park(sp, result)
        bus = _Bus()
        dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
        await dispatcher.drain_once()
        payload = {"value": "v", "data": {"k": 1}, "from_session_id": OTHER}
        await sp.get_event_store().append(event_type="session.messaged", workspace_id=WORKSPACE, session_id=OTHER, payload=payload)
        assert await dispatcher.drain_once() == 1

        [(_key, envelope)] = bus.published
        assert envelope["payload"] == payload


# Every way a matcher path reaches what a user entered: the input keys through the payload, through the payload fallback (an envelope
# path that is not found is looked up in the payload, primer.events.filters._lookup_path), a key under them, and the whole payload
# (a prefix of its JSON reads the input too).
INPUT_PATHS = ["payload.value", "payload.data", "payload.data.note", "value", "data", "data.note", "payload"]
# Paths that read what the non-admin is shown anyway.
SHOWN_PATHS = ["payload.element", "payload.kind", "payload.name", "payload.by", "element", "kind"]


@pytest.mark.parametrize("op", ["eq", "prefix"])
@pytest.mark.parametrize("path", INPUT_PATHS)
async def test_a_non_admin_canvas_wait_may_not_match_on_the_input(sp, path: str, op: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        result = await make_wait_for_event_handler(sp)(
            {"event_types": ["canvas.*"], "fields": [SESSION_SCOPE, {"path": path, "op": op, "value": "Lo"}]}, ctx=_ctx(USER),
        )

        assert not isinstance(result, Yielded) and result.is_error
        body = json.loads(result.output)
        assert body["type"] == "forbidden" and repr(path) in body["message"], body
        assert (await sp.get_storage(EventSubscription).list(OffsetPage(length=10))).items == [], "nothing was stored"


@pytest.mark.parametrize("path", SHOWN_PATHS)
async def test_a_non_admin_canvas_wait_may_match_on_what_it_is_shown(sp, path: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        result = await make_wait_for_event_handler(sp)(
            {"event_types": ["canvas.event"], "fields": [SESSION_SCOPE, {"path": path, "op": "eq", "value": "x"}]}, ctx=_ctx(USER),
        )

        assert isinstance(result, Yielded), result


@pytest.mark.parametrize("path", INPUT_PATHS)
async def test_an_admin_and_a_non_canvas_wait_may_match_on_those_paths(sp, path: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        handler = make_wait_for_event_handler(sp)
        matcher = {"path": path, "op": "prefix", "value": "Lo"}

        admin = await handler({"event_types": ["canvas.event"], "fields": [matcher]}, ctx=_ctx(ADMIN))
        other_kind = await handler({"event_types": ["session.messaged"], "fields": [matcher]}, ctx=_ctx(USER))

        assert isinstance(admin, Yielded), admin
        assert isinstance(other_kind, Yielded), other_kind


async def test_a_session_wake_sink_still_needs_no_new_field(sp) -> None:
    """The keep-input decision rides the park, not the subscription: a stored ``SessionWakeSink`` is the shape every build reads, so a
    rolling deploy (an older dispatcher reading a newer row, or the reverse) never meets a row it cannot decode."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        yielded = await _wait(sp, USER, SESSION_SCOPE)
        [sub] = (await sp.get_storage(EventSubscription).list(OffsetPage(length=10))).items
        assert sub.sink == SessionWakeSink(event_key=yielded.event_key, session_id=WAITER)
        assert sub.filter == EventFilter(event_types=["canvas.event"], fields=[SESSION_SCOPE])
