"""A session wake never carries what a user ENTERED on a canvas (audit G8-08, the lead's rulings on #872).

A ``canvas.event``'s ``value`` and ``data`` never leave the session they belong to through the event log. A wake is such a door twice:
the flip stamps the published envelope on the waiting row (``parked_state.resume_event_payload``, which ``GET /v1/sessions/{id}``
serves to any signed-in user: sessions are a shared read by the user's G7-02 decision), and the resume makes it the run's tool result,
its transcript. So the dispatcher removes the two keys from EVERY wake before it publishes, whoever waits:

* a non-admin ``wait_for_event`` (scoped to its own workspace by #856, and the default workspace is shared, so it can wake on another
  user's click), on another session's canvas or its own;
* an admin's ``wait_for_event``: an admin who needs another session's input reads ``GET /v1/events``, where an admin gets the full event;
* ``canvas_wait`` on the calling session's own canvas: its resume restores the input from the stored event on that canvas, so the
  agent still gets what its user entered. An event the canvas ring evicted before the resume comes back without it, beside the canvas's
  current ``values``.

``wait_for_event``'s resume hook removes the keys again (defence in depth). A non-admin canvas wait may not match on the input either
(``payload.value``, ``payload.data`` or the whole ``payload``, also through the payload fallback, ``value``): whether the wait wakes would
tell the run what was entered. An admin's matchers are not limited.

Everything runs on the real SQLite storage: the real tool handlers, the park the worker writes, ``CanvasService.record_event`` appending
the real ``canvas.event``, the real dispatcher, the real flip (``flip_sessions_parked_on``, what the bus listener runs), the registered
resume hooks and the real router read by another non-admin user.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.passwords import hash_password
from primer.canvas.lang import parse_program
from primer.canvas.service import CanvasService
from primer.canvas.settings import write_canvas_settings
from primer.events.dispatcher import EventDispatcher
from primer.events.redaction import MASK
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from primer.model.event import EventSubscription
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.session.yields import flip_sessions_parked_on
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.canvas import build_canvas_toolset
from primer.toolset.events_wait import make_wait_for_event_handler, wait_for_event_resume
from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook

#: The bound on every test body.
BODY_TIMEOUT_S = 60

WORKSPACE = "w"
WAITER = "sess-1"          # the session that waits
OTHER = "s-owner"          # another session of the same (shared) workspace
ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")
INPUT = ("value", "data")

PROGRAM = """
root = Column([genre, viz])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
viz = Html("<div id=b>pick one</div>", 60)
"""
#: The interactions, each with what the user entered (distinctive, so a leak shows anywhere in a served row).
INTERACTIONS = {
    "a select change": {"element": "genre", "kind": "change", "value": "Lo-fi"},
    "an html emit": {"element": "viz", "kind": "emit", "name": "submit", "data": {"note": "the owner's secret note"}},
}
#: The text of each interaction's input, searched for in a served row.
ENTERED_TEXT = {"a select change": "Lo-fi", "an html emit": "the owner's secret note"}


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


@pytest_asyncio.fixture
async def other_user(sp: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in non-admin who is neither the waiter nor the owner of the canvas: the reader of the shared session rows."""
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as boot, \
            httpx.AsyncClient(transport=transport, base_url="http://test") as other:
        reg = await boot.post("/v1/auth/register", json={"username": "bootadmin", "password": "bootadminpass1"})
        assert reg.status_code == 200, reg.text
        await sp.get_storage(User).create(User(
            id="u-other", username="other", password_hash=await hash_password("otherpass1"), created_at=datetime.now(UTC), role="user",
        ))
        login = await other.post("/v1/auth/login", json={"username": "other", "password": "otherpass1"})
        assert login.status_code == 200, login.text
        yield other


def _ctx(who: PrincipalRef, session_id: str = WAITER) -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id=session_id, workspace_id=WORKSPACE, initiated_by=who)


async def _canvas(sp, session_id: str) -> CanvasService:
    # No V8 for a body without a script: the script check is not what is under test here.
    service = CanvasService(sp, script_check=lambda body: [])
    await service.apply(
        session_id=session_id, workspace_id=WORKSPACE, statements=parse_program(PROGRAM), unit="line", base_rev=None,
        source="agent", by="agent",
    )
    return service


async def _park(sp, yielded: Yielded, *, session_id: str = WAITER, who: PrincipalRef = USER, tool_name: str = "wait_for_event") -> None:
    """The park row the worker writes after the tool returns (``parked_state.yielded`` is the stamped Yielded)."""
    now = datetime.now(UTC)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id=WORKSPACE, binding=AgentSessionBinding(agent_id="agent-a"), status=SessionStatus.WAITING,
        created_at=now, initiated_by=who, parked_status="parked", parked_event_key=yielded.event_key, parked_at=now,
        parked_state={"tool_call_id": "call-1", "yielded": {**yielded.to_jsonable(), "tool_name": tool_name}},
    ))


async def _wake(sp, yielded: Yielded, *, session_id: str, interaction: dict[str, Any], flip: bool = False) -> dict[str, Any]:
    """Record ``interaction`` on ``session_id``'s canvas, drain the dispatcher once and return the envelope it published for the park.

    With ``flip`` the envelope is also delivered as the bus listener delivers it: the real flip stamps it on the waiting row."""
    bus = _Bus()
    dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
    await dispatcher.drain_once()                                     # the subscription's cursor is pinned at the head already
    await CanvasService(sp).record_event(session_id=session_id, workspace_id=WORKSPACE, by="owner", **interaction)
    assert await dispatcher.drain_once() == 1
    [(key, envelope)] = bus.published
    assert key == yielded.event_key
    if flip:
        assert await flip_sessions_parked_on(key, envelope, session_storage=sp.get_storage(WorkspaceSession), engine=None) == 1
    return envelope


def _result_event(yielded: Yielded, envelope: dict[str, Any]) -> dict[str, Any]:
    out = get_resume_hook("wait_for_event")(yielded.resume_metadata, envelope, None)
    assert not out.is_error, out.output
    return json.loads(out.output)["event"]


async def _wait(sp, who: PrincipalRef, scope: dict[str, str], session_id: str = WAITER) -> Yielded:
    result = await make_wait_for_event_handler(sp)({"event_types": ["canvas.event"], "fields": [scope]}, ctx=_ctx(who, session_id))
    assert isinstance(result, Yielded), result
    return result


async def _canvas_wait(sp, interaction: dict[str, Any]):
    """The real ``canvas_wait`` of the canvas owner's own session (a non-admin run), through the toolset provider."""
    provider = build_canvas_toolset(storage_provider=sp)
    with pytest.raises(YieldToWorker) as exc:
        await provider.call(
            tool_name="canvas_wait", arguments={"ids": [interaction["element"]], "kinds": [interaction["kind"]]}, ctx=_ctx(USER, OTHER),
        )
    return provider, exc.value.yielded


def _resume_context(provider) -> ResumeContext:
    async def resolve(toolset_id: str):
        assert toolset_id == "canvas"
        return provider
    return ResumeContext(tool_name="canvas_wait", tool_call_id="call-1", session_id=OTHER, resolve_provider=resolve)


async def _served_row(client: httpx.AsyncClient, session_id: str) -> tuple[dict[str, Any], str]:
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    return resp.json(), resp.text


WORKSPACE_SCOPE = {"path": "workspace_id", "op": "eq", "value": WORKSPACE}
SESSION_SCOPE = {"path": "session_id", "op": "eq", "value": WAITER}
OWNER_SCOPE = {"path": "session_id", "op": "eq", "value": OTHER}


# ---- what the wake and the wait_for_event result carry ------------------------------------------------------------------------------


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_a_non_admin_wait_on_its_workspace_gets_another_sessions_canvas_event_without_its_input(sp, interaction: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await _canvas(sp, OTHER)
        yielded = await _wait(sp, USER, WORKSPACE_SCOPE)
        await _park(sp, yielded)

        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction])

        assert (envelope["event_type"], envelope["session_id"]) == ("canvas.event", OTHER)
        assert not set(INPUT) & set(envelope["payload"]), f"the wake carried the input: {envelope['payload']}"
        assert envelope["payload"]["element"] == INTERACTIONS[interaction]["element"] and envelope["payload"]["by"] == "owner"
        assert envelope["payload"].get("name") == INTERACTIONS[interaction].get("name"), "the Html emit name is kept"
        assert not set(INPUT) & set(_result_event(yielded, envelope)["payload"]), "the tool result carried the input"


async def test_a_non_admin_wait_on_its_own_session_is_stripped_too(sp) -> None:
    """One rule, no ownership check: the run's own session's canvas is read through the canvas tools, not through the event log."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await _canvas(sp, WAITER)
        yielded = await _wait(sp, USER, SESSION_SCOPE)
        await _park(sp, yielded)

        envelope = await _wake(sp, yielded, session_id=WAITER, interaction=INTERACTIONS["an html emit"])

        assert envelope["session_id"] == WAITER and not set(INPUT) & set(envelope["payload"])
        assert envelope["payload"]["name"] == "submit"


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_an_admin_wait_gets_no_input_either(sp, interaction: str) -> None:
    """An admin who needs another session's input reads GET /v1/events; a wake would land it on the admin's shared row and transcript."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await _canvas(sp, OTHER)
        yielded = await _wait(sp, ADMIN, OWNER_SCOPE)
        await _park(sp, yielded, who=ADMIN)

        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction])

        assert not set(INPUT) & set(envelope["payload"]), f"the admin's wake carried the input: {envelope['payload']}"
        event = _result_event(yielded, envelope)
        assert not set(INPUT) & set(event["payload"]) and ENTERED_TEXT[interaction] not in json.dumps(event)
        assert event["payload"]["element"] == INTERACTIONS[interaction]["element"]


async def test_a_wake_of_any_other_kind_is_delivered_whole(sp) -> None:
    """Only the input keys of a ``canvas.event`` are removed: a session event keeps its payload."""
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


# ---- the waiting row, a shared read --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_the_row_of_a_canvas_wait_holds_no_input_after_the_wake(sp, other_user, interaction: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await _canvas(sp, OTHER)
        _provider, yielded = await _canvas_wait(sp, INTERACTIONS[interaction])
        await _park(sp, yielded, session_id=OTHER, tool_name="canvas_wait")

        await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction], flip=True)

        row, text = await _served_row(other_user, OTHER)
        held = row["parked_state"]["resume_event_payload"]["payload"]
        assert held["element"] == INTERACTIONS[interaction]["element"], "the flip stamped the wake on the row"
        assert not set(INPUT) & set(held), f"another user reads the input on the waiting row: {held}"
        assert ENTERED_TEXT[interaction] not in text


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_the_row_of_an_admin_wait_holds_no_input_after_the_wake(sp, other_user, interaction: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await _canvas(sp, OTHER)
        yielded = await _wait(sp, ADMIN, OWNER_SCOPE)
        await _park(sp, yielded, who=ADMIN)

        await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction], flip=True)

        row, text = await _served_row(other_user, WAITER)
        held = row["parked_state"]["resume_event_payload"]["payload"]
        assert held["element"] == INTERACTIONS[interaction]["element"], "the flip stamped the wake on the row"
        assert not set(INPUT) & set(held), f"another user reads the input on the admin's waiting row: {held}"
        assert ENTERED_TEXT[interaction] not in text


# ---- canvas_wait: the session's own input, restored from its own canvas ------------------------------------------------------------


@pytest.mark.parametrize("interaction", list(INTERACTIONS), ids=list(INTERACTIONS))
async def test_the_canvas_wait_result_still_carries_the_input(sp, interaction: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await _canvas(sp, OTHER)
        provider, yielded = await _canvas_wait(sp, INTERACTIONS[interaction])
        await _park(sp, yielded, session_id=OTHER, tool_name="canvas_wait")
        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS[interaction])
        assert not set(INPUT) & set(envelope["payload"])

        out = await get_resume_hook("canvas_wait")(yielded.resume_metadata, envelope, _resume_context(provider))

        assert not out.is_error, out.output
        [event] = json.loads(out.output)["events"]
        entered = {k: v for k, v in INTERACTIONS[interaction].items() if k in INPUT}
        assert {k: event[k] for k in entered} == entered, "the resume restores the input from the session's own canvas"


async def test_an_event_the_ring_evicted_before_the_resume_comes_back_without_its_input(sp) -> None:
    """The documented edge: the canvas keeps ``max_events``; an emit pushed out before the resume is returned from the stripped wake,
    with the canvas's current ``values`` beside it."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        await write_canvas_settings(sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_events=10))
        await _canvas(sp, OTHER)
        provider, yielded = await _canvas_wait(sp, INTERACTIONS["an html emit"])
        await _park(sp, yielded, session_id=OTHER, tool_name="canvas_wait")
        envelope = await _wake(sp, yielded, session_id=OTHER, interaction=INTERACTIONS["an html emit"])
        service = CanvasService(sp)
        for n in range(10):                                           # ten non-waking changes push the emit out of a ring of ten
            await service.record_event(
                session_id=OTHER, workspace_id=WORKSPACE, element="genre", kind="change", value=["Lo-fi", "Classical"][n % 2], by="owner",
            )

        out = await get_resume_hook("canvas_wait")(yielded.resume_metadata, envelope, _resume_context(provider))

        body = json.loads(out.output)
        [event] = body["events"]
        assert event["id"] == envelope["payload"]["id"] and event["name"] == "submit"
        assert "data" not in event, "an evicted event comes back without its input"
        assert body["values"] == {"genre": "Classical"}, "the canvas's current values are beside it"


# ---- the resume hook removes the input again (defence in depth) ---------------------------------------------------------------------


def test_the_wait_for_event_resume_hook_removes_the_input_again() -> None:
    """A wake that reached the hook by another path, with the input still in it: the hook removes it (and masks secrets, as before)."""
    envelope = {
        "id": 7, "event_type": "canvas.event", "session_id": OTHER, "workspace_id": WORKSPACE,
        "payload": {"id": "e1", "element": "viz", "kind": "emit", "name": "submit", "data": {"note": "typed"}, "value": "v", "by": "owner"},
    }
    other_kind = {"id": 8, "event_type": "session.messaged", "payload": {"value": "v", "data": {"k": 1}, "api_key": "sk-1"}}

    event = json.loads(wait_for_event_resume({}, envelope, None).output)["event"]
    kept = json.loads(wait_for_event_resume({}, other_kind, None).output)["event"]

    assert event["payload"] == {"id": "e1", "element": "viz", "kind": "emit", "name": "submit", "by": "owner"}
    assert kept["payload"] == {"value": "v", "data": {"k": 1}, "api_key": MASK}


# ---- a non-admin canvas wait may not match on the input --------------------------------------------------------------------------------

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
