"""``GET /v1/events`` gives a non-admin a ``canvas.event`` without what the user ENTERED (audit G8-08, the lead's ruling D).

Sessions and their transcripts are a shared read in this install by design (the user's G7-02 decision): every user may open every
session. What the event feed carried that nothing else does is the input of a canvas interaction, a Select's ``value`` and an Html
element's emit ``data``. So a non-admin reader gets every ``canvas.event`` with those two payload keys REMOVED (absent, not masked), and
everything else as an admin sees it: the envelope, the element, the kind, the emit ``name``, who did it, the ids, seq, rev and times.
The rule is one rule: it does not look at whose session the event is from, so a non-admin's own session is stripped too. An admin reads
the full event, as before. ``canvas.updated`` carries no input and is the same for both.

The app runs on the real SQLite storage and the events are the real ones: ``CanvasService.record_event`` validates each interaction,
writes it to the canvas and appends the ``canvas.event`` to the SQLite event log, as production does. Every query shape the console uses
is read: the workspace feed, an exact type, a glob, a ``since`` window, and the log polled one event at a time (each ``after_id`` is the
last id read, the way a tail follows the log; there is no SSE stream of the event log).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider

#: The bound on every test body: one SQLite file, a handful of requests.
BODY_TIMEOUT_S = 60

WORKSPACE = "ws-prime"
#: An admin's session and a user's own session, both in the shared default workspace.
ADMIN_SESSION, USER_SESSION = "s-admin", "s-user"
ADMIN_REF = PrincipalRef(type="user", id="u-admin", display="evadmin", role="admin", source="local")
USER_REF = PrincipalRef(type="user", id="u-user", display="evuser", role="user", source="local")

PROGRAM = """
root = Column([genre, viz])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
viz = Html("<div id=b>pick one</div>", 60)
"""
#: What each session's user entered: the Select's value and the Html element's emit data.
ENTERED = {
    ADMIN_SESSION: {"value": "Lo-fi", "data": {"note": "what the admin typed", "card": "4111"}},
    USER_SESSION: {"value": "Classical", "data": {"note": "what the user typed"}},
}
#: The payload keys a non-admin never reads.
INPUT = ("value", "data")

#: The query shapes of the feed (the console's Activity panel and the events sidebar). ``{base}`` is the log head before the events and
#: ``{since}`` a moment before them.
QUERIES = {
    "workspace feed": "/v1/events?after_id={base}&workspace_id=" + WORKSPACE,
    "exact type": "/v1/events?after_id={base}&workspace_id=" + WORKSPACE + "&event_type=canvas.event",
    "glob": "/v1/events?after_id={base}&workspace_id=" + WORKSPACE + "&event_type=canvas.*",
    "since": "/v1/events?since={since}&workspace_id=" + WORKSPACE,
    "polled one at a time": "/v1/events?after_id={base}&workspace_id=" + WORKSPACE + "&limit=1",
}
POLLED = "polled one at a time"


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "events.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _app(sp: SqliteStorageProvider):
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    return create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def clients(sp: SqliteStorageProvider) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """``(admin, user)``: two cookie sessions on one app. The admin registers (the first account is the admin); the user logs in."""
    app = _app(sp)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin, \
            httpx.AsyncClient(transport=transport, base_url="http://test") as user:
        reg = await admin.post("/v1/auth/register", json={"username": "evadmin", "password": "evadminpass1"})
        assert reg.status_code == 200, reg.text
        await sp.get_storage(User).create(User(
            id="u-user", username="evuser", password_hash=await hash_password("evuserpass1"),
            created_at=datetime.now(UTC), role="user",
        ))
        login = await user.post("/v1/auth/login", json={"username": "evuser", "password": "evuserpass1"})
        assert login.status_code == 200, login.text
        yield admin, user


async def _seed(sp: SqliteStorageProvider) -> dict[str, Any]:
    """Two sessions of the shared workspace, a canvas on each, and on each a Select change and an Html emit. Returns the marks the query
    shapes read after: ``base``, the log head before them, and ``since``, a moment before them (UTC, as a query value).

    An ``agent.created`` row is appended too: it is the proof that the user client is read as a non-admin (the feed hides entity CRUD
    from a non-admin only)."""
    store = sp.get_event_store()
    marks = {"base": await store.max_id(), "since": (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    sessions = sp.get_storage(WorkspaceSession)
    for sid, creator in ((ADMIN_SESSION, ADMIN_REF), (USER_SESSION, USER_REF)):
        await sessions.create(WorkspaceSession(
            id=sid, workspace_id=WORKSPACE, binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.WAITING,
            created_at=datetime.now(UTC), initiated_by=creator,
        ))
    # No V8 for a body without a script: the script check is not what is under test here.
    service = CanvasService(sp, script_check=lambda body: [])
    for sid, creator in ((ADMIN_SESSION, ADMIN_REF), (USER_SESSION, USER_REF)):
        await service.apply(
            session_id=sid, workspace_id=WORKSPACE, statements=parse_program(PROGRAM), unit="line", base_rev=None,
            source="agent", by="agent",
        )
        await service.record_event(
            session_id=sid, workspace_id=WORKSPACE, element="genre", kind="change", value=ENTERED[sid]["value"], by=creator.display,
        )
        await service.record_event(
            session_id=sid, workspace_id=WORKSPACE, element="viz", kind="emit", name="submit", data=ENTERED[sid]["data"],
            by=creator.display,
        )
    await store.append(event_type="agent.created", entity_kind="agent", entity_id="a-1", workspace_id=WORKSPACE, payload={"id": "a-1"})
    return marks


async def _read(client: httpx.AsyncClient, query: str, marks: dict[str, Any]) -> list[dict[str, Any]]:
    """The items of ``query`` after the ``marks`` of :func:`_seed`. The polled shape follows the log one page of one event at a time, each
    ``after_id`` the last id read, until a page comes back empty (the non-admin's last page is the ``agent.created`` row it is not shown)."""
    if query != POLLED:
        resp = await client.get(QUERIES[query].format(**marks))
        assert resp.status_code == 200, resp.text
        return resp.json()["items"]
    items: list[dict[str, Any]] = []
    cursor = marks["base"]
    while True:
        resp = await client.get(QUERIES[query].format(**{**marks, "base": cursor}))
        assert resp.status_code == 200, resp.text
        page = resp.json()["items"]
        if not page:
            return items
        assert len(page) == 1
        items += page
        cursor = page[-1]["id"]


def _canvas_events(items: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    return [e for e in items if e["event_type"] == "canvas.event" and e["session_id"] == session_id]


def _without_input(event: dict[str, Any]) -> dict[str, Any]:
    return {**event, "payload": {k: v for k, v in event["payload"].items() if k not in INPUT}}


@pytest.mark.parametrize("session_id", [ADMIN_SESSION, USER_SESSION], ids=["another users session", "the users own session"])
@pytest.mark.parametrize("query", list(QUERIES), ids=list(QUERIES))
async def test_a_non_admin_reads_a_canvas_event_without_its_input(sp, clients, session_id: str, query: str) -> None:
    """Another user's session and the reader's own: one rule, no ownership check. The rest of the event is the admin's, key for key."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        admin, user = clients
        marks = await _seed(sp)

        user_items = await _read(user, query, marks)
        admin_items = await _read(admin, query, marks)

        if query in ("workspace feed", "since"):
            assert "agent.created" in [e["event_type"] for e in admin_items]
            assert "agent.created" not in [e["event_type"] for e in user_items], "the user client must be read as a non-admin"
        seen = _canvas_events(user_items, session_id)
        full = _canvas_events(admin_items, session_id)
        assert [e["payload"]["kind"] for e in seen] == ["change", "emit"]
        for event in seen:
            assert not set(INPUT) & set(event["payload"]), f"a non-admin read the input of {event['payload']['id']}: {event['payload']}"
        assert seen == [_without_input(e) for e in full], "everything but the input is what the admin reads"
        change, emit = seen
        assert (change["payload"]["element"], change["payload"]["type"], change["payload"]["rev"]) == ("genre", "Select", 2)
        assert (emit["payload"]["element"], emit["payload"]["name"], emit["payload"]["seq"]) == ("viz", "submit", 2)
        assert {e["payload"]["by"] for e in seen} == {(ADMIN_REF if session_id == ADMIN_SESSION else USER_REF).display}
        assert {e["actor"] for e in seen} == {e["actor"] for e in full}


@pytest.mark.parametrize("session_id", [ADMIN_SESSION, USER_SESSION], ids=["the admins session", "a users session"])
async def test_an_admin_reads_the_value_and_the_data(sp, clients, session_id: str) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        admin, _user = clients
        marks = await _seed(sp)

        change, emit = _canvas_events(await _read(admin, "workspace feed", marks), session_id)

        assert change["payload"]["value"] == ENTERED[session_id]["value"]
        assert emit["payload"]["data"]["note"] == ENTERED[session_id]["data"]["note"]
        assert emit["payload"]["name"] == "submit"


async def test_canvas_updated_reads_the_same_for_both(sp, clients) -> None:
    """``canvas.updated`` says which element ids changed and who changed them: no input, nothing to drop."""
    async with asyncio.timeout(BODY_TIMEOUT_S):
        admin, user = clients
        marks = await _seed(sp)
        query = "/v1/events?after_id={base}&workspace_id=" + WORKSPACE + "&event_type=canvas.updated"

        seen = (await user.get(query.format(**marks))).json()["items"]

        assert len(seen) == 2 and seen == (await admin.get(query.format(**marks))).json()["items"]
        assert seen[0]["payload"]["changed"]["added"]
