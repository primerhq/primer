"""The console's canvas routes (pivot P5, Task 15): read, apply, clear and click, through the real routers and auth middleware.

``client`` is the first registered account (an admin, ``testuser``); ``carol`` is a user-role account the admin creates. The workspace is
the canvas suite's recording fake, so the transcript records and the instructions a wake hands the session's slot are read back.

The lead's rulings on S6 pinned here:

1. ``POST .../canvas/events`` drives a session like a steer: full on the session's workspace (P2 Task 3) before anything is read or written,
   an admin never refused, a token acting as its owner, the workspace's access deciding who acts (the WHO table). The operator's writes
   (``POST .../canvas/ops``, ``DELETE .../canvas``) put input into the session too and follow the same gate. It hands off to
   ``deliver_canvas_event`` and never writes the input into a structured copy.
2. ``GET .../canvas`` serves the values and the newest events WITH their input to a holder of read on the session's workspace (the shared
   session read; a stranger gets the session's missing-row 404), while the event feed keeps removing it for a non-admin (#872).
3. The cap on events that do not wake the agent answers 429 ``silent_rate_limited`` and records nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.canvas.files import referenced_files
from primer.model.access import Access
from primer.model.canvas import MAX_EVENT_BODY_BYTES, MAX_SILENT_EVENTS_PER_MINUTE, SessionCanvas
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.api.test_session_steer_restart_rank import WHO, _token_client, _user_id, _workspace as _seed_workspace
from tests.canvas._fakes import FakeWorkspace

WS = "ws-1"
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")
SECRET_NOTE = "the owner's private note 7f3a"

PLAYLIST = """
root = Column([title, cover, controls, player, more, viz])
title = Text("Pick a playlist", "h2")
cover = Image("file:assets/c.png", "Album cover")
controls = Row([genre, play])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
play = Button("Play", "primary")
player = Audio("https://cdn.example.com/audio/so-what.mp3", "So What")
more = Link("Open on Bandcamp", "https://milesdavis.bandcamp.com/album/kind-of-blue")
viz = Html("<div id=b>pick one</div>", 60)
"""


@pytest_asyncio.fixture
async def ws(app) -> FakeWorkspace:
    """The one workspace behind the app's registry, a recorder."""
    workspace = FakeWorkspace()

    async def get_workspace(workspace_id: str) -> FakeWorkspace:
        return workspace

    app.state.workspace_registry.get_workspace = get_workspace  # type: ignore[method-assign]
    return workspace


@pytest_asyncio.fixture
async def carol(app, client) -> AsyncIterator[httpx.AsyncClient]:
    """A user-role account, created by the admin ``client`` and logged in on its own client."""
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
        yield c


async def _seed(sp, sid: str = "s1", *, workspace_id: str = WS, by: PrincipalRef | None = None, **fields) -> WorkspaceSession:
    row = WorkspaceSession(
        id=sid, workspace_id=workspace_id, binding=AgentSessionBinding(agent_id="operator"), created_at=datetime.now(UTC),
        initiated_by=by, **({"status": SessionStatus.WAITING, "turn_status": "idle", "turn_no": 1} | fields),
    )
    await sp.get_storage(WorkspaceSession).create(row)
    return row


def _url(sid: str = "s1", tail: str = "", workspace_id: str = WS) -> str:
    return f"/v1/workspaces/{workspace_id}/sessions/{sid}/canvas{tail}"


async def _draw(c: httpx.AsyncClient, sid: str = "s1", source: str = PLAYLIST) -> dict:
    r = await c.post(_url(sid, "/ops"), json={"source": source})
    assert r.status_code == 200, r.text
    return r.json()


async def _canvas(sp, sid: str = "s1") -> SessionCanvas | None:
    return await sp.get_storage(SessionCanvas).get(sid)


async def _platform(sp, event_type: str) -> list:
    return [e for e in await sp.get_event_store().read_after(0, limit=10_000) if e.event_type == event_type]


# ---- reading -----------------------------------------------------------------------------------------------------------------------


async def test_a_new_session_has_an_empty_canvas(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    r = await client.get(_url())
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["rev"], body["event_seq"], body["element_count"], body["settings"]["media_policy"]) == (0, 0, 0, "any-https")
    assert (body["session_id"], body["workspace_id"], body["events"], body["file_urls"]) == ("s1", WS, [], {})
    assert body["frame_url"].startswith("/canvas/frame?v=")
    assert await _canvas(fake_storage_provider) is None, "reading a canvas never creates one"


async def test_ops_apply_and_read_back_as_a_program(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    out = await _draw(client)
    assert out["rev"] == 1 and "genre" in out["changed"]["added"] and out["warnings"] == []
    lang = (await client.get(_url(), params={"format": "lang"})).json()
    assert lang["rev"] == 1 and lang["format"] == "lang"
    assert 'genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")' in lang["source"]
    assert 'viz = Html(..., 60)' in lang["source"]
    full = (await client.get(_url(), params={"format": "lang", "full": "true"})).json()
    assert 'viz = Html("<div id=b>pick one</div>", 60)' in full["source"]
    [record] = ws.lines("s1")
    assert (record["kind"], record["payload"]["source"], record["payload"]["by"]) == ("canvas_op", "operator", "testuser")
    [updated] = await _platform(fake_storage_provider, "canvas.updated")
    assert updated.actor == "testuser"


async def test_json_ops_apply_and_a_malformed_op_is_named(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    ops = [{"op": "set", "id": "hello", "type": "Text", "props": {"text": "hi"}}, {"op": "append", "id": "root", "children": ["hello"]}]
    r = await client.post(_url(tail="/ops"), json={"ops": ops})
    assert r.status_code == 200, r.text
    assert r.json()["changed"]["added"] == ["hello"]
    r = await client.post(_url(tail="/ops"), json={"ops": [{"op": "paint", "id": "x"}]})
    assert r.status_code == 422, r.text
    ext = r.json()["extensions"]
    assert (ext["code"], ext["message"], ext["rev"]) == ("canvas_invalid", "1 problem(s); nothing was applied", 1)
    assert ext["errors"][0]["op"] == 1 and "op must be one of" in ext["errors"][0]["message"]


async def test_a_refused_batch_is_422_with_line_errors(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    r = await client.post(_url(tail="/ops"), json={"source": 'a = Text("ok")\nb = \nroot += [a]\nTitle = Text("x")'})
    assert r.status_code == 422, r.text
    ext = r.json()["extensions"]
    assert ext["code"] == "canvas_invalid" and ext["message"] == "2 problem(s); nothing was applied" and ext["rev"] == 0
    assert [e["line"] for e in ext["errors"]] == [2, 4]
    r = await client.post(_url(tail="/ops"), json={"source": 'a = Button("Go")\nb = Text("orphan")\nroot += [a, a]'})
    assert r.status_code == 422 and r.json()["extensions"]["code"] == "canvas_invalid"
    assert await _canvas(fake_storage_provider) is None, "nothing was applied"
    assert ws.lines("s1") == []


@pytest.mark.parametrize("body", [{}, {"source": 'a = Text("x")', "ops": []}])
async def test_source_or_ops_one_of_the_two(client, fake_storage_provider, ws, body):
    await _seed(fake_storage_provider)
    r = await client.post(_url(tail="/ops"), json=body)
    assert r.status_code == 422 and r.json()["extensions"]["code"] == "canvas_invalid"
    assert r.json()["detail"] == "give source or ops, not both and not neither"


async def test_a_stale_base_rev_is_409(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    assert (await client.post(_url(tail="/events"), json={"element": "genre", "kind": "change", "value": "Lo-fi"})).status_code == 202
    r = await client.post(_url(tail="/ops"), json={"source": 'title.text = "Again"', "base_rev": 1})
    assert r.status_code == 409, r.text
    ext = r.json()["extensions"]
    assert (ext["code"], ext["rev"]) == ("canvas_rev_conflict", 2)
    assert r.json()["detail"] == "the canvas is at rev 2, not 1; nothing was applied"
    assert len(ext["changes"]) == 1 and "genre" in ext["changes"][0]


async def test_delete_clears(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    r = await client.delete(_url())
    assert r.status_code == 200, r.text
    assert r.json()["rev"] == 2 and "genre" in r.json()["changed"]["removed"]
    body = (await client.get(_url())).json()
    assert (body["rev"], body["element_count"]) == (2, 0)


# ---- clicks ------------------------------------------------------------------------------------------------------------------------


async def test_a_click_on_an_idle_session_wakes_it(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    r = await client.post(_url(tail="/events"), json={"element": "play", "kind": "click"})
    assert r.status_code == 202, r.text
    assert r.json() == {"event_id": "e1", "rev": 1, "event_seq": 1, "delivery": "woken"}
    [(instruction, _)] = ws.slots["s1"].appended
    assert instruction.startswith("<canvas_event") and 'by="testuser"' in instruction and 'label="Play"' in instruction
    [user_input] = [r for r in ws.lines("s1") if r["kind"] == "user_input"]
    assert user_input["payload"]["canvas_event"]["element"] == "play"
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("s1")).turn_status == "claimable"
    [logged] = await _platform(fake_storage_provider, "canvas.event")
    assert logged.payload["by"] == "testuser" and logged.actor == "testuser"


async def test_a_silent_event_is_recorded(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    r = await client.post(_url(tail="/events"), json={"element": "genre", "kind": "change", "value": "Lo-fi"})
    assert r.status_code == 202 and r.json() == {"event_id": "e1", "rev": 2, "event_seq": 1, "delivery": "recorded"}
    assert "s1" not in ws.slots, "a silent event wakes no one"
    [record] = [r for r in ws.lines("s1") if r["kind"] == "canvas_event"]
    assert record["payload"]["element"] == "genre" and "value" not in record["payload"], "the record carries no input"


async def test_a_click_while_a_turn_runs_is_queued(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider, status=SessionStatus.RUNNING, turn_status="running")
    await _draw(client)
    r = await client.post(_url(tail="/events"), json={"element": "viz", "kind": "emit", "name": "submit", "data": {"note": SECRET_NOTE}})
    assert r.status_code == 202 and r.json()["delivery"] == "queued"
    assert "s1" not in ws.slots, "the turn is not interrupted: the click waits behind it"


async def test_a_retried_post_records_and_delivers_once(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    body = {"element": "play", "kind": "click", "client_event_id": "c-1"}
    first = await client.post(_url(tail="/events"), json=body)
    second = await client.post(_url(tail="/events"), json=body)
    assert first.json()["delivery"] == "woken"
    assert second.status_code == 202 and second.json() == {**first.json(), "delivery": "recorded"}
    assert len(ws.slots["s1"].appended) == 1, "the retried click was not delivered again"
    row = await _canvas(fake_storage_provider)
    assert (row.event_seq, len(row.events)) == (1, 1)


async def test_a_retry_whose_first_delivery_raised_delivers_the_click(client, fake_storage_provider, ws, monkeypatch):
    """Review of #908, round 2: the click was recorded but its delivery raised, so the client retries with the same key. The event is
    still unread and nothing queued it: the retry delivers it, once, and records nothing more."""
    import primer.canvas.delivery as delivery

    await _seed(fake_storage_provider)
    await _draw(client)
    real, calls = delivery.deliver_canvas_event, []

    async def flaky(**kwargs):
        calls.append(kwargs["event"].id)
        if len(calls) == 1:
            raise RuntimeError("the wake could not be written")
        return await real(**kwargs)

    monkeypatch.setattr(delivery, "deliver_canvas_event", flaky)
    body = {"element": "play", "kind": "click", "client_event_id": "c-1"}
    with pytest.raises(RuntimeError):
        await client.post(_url(tail="/events"), json=body)
    assert "s1" not in ws.slots and (await _canvas(fake_storage_provider)).event_seq == 1

    r = await client.post(_url(tail="/events"), json=body)
    assert r.status_code == 202 and r.json() == {"event_id": "e1", "rev": 1, "event_seq": 1, "delivery": "woken"}
    assert calls == ["e1", "e1"] and len(ws.slots["s1"].appended) == 1
    third = await client.post(_url(tail="/events"), json=body)
    assert third.json()["delivery"] == "recorded" and len(ws.slots["s1"].appended) == 1, "a delivered (read) click is not delivered twice"
    assert len(await _platform(fake_storage_provider, "canvas.event")) == 1


async def test_a_retry_of_a_queued_click_is_not_queued_again(client, fake_storage_provider, ws):
    from primer.model.workspace_session import PendingSessionMessage
    from primer.model.storage import OffsetPage

    await _seed(fake_storage_provider, status=SessionStatus.RUNNING, turn_status="running")
    await _draw(client)
    body = {"element": "play", "kind": "click", "client_event_id": "c-1"}
    assert (await client.post(_url(tail="/events"), json=body)).json()["delivery"] == "queued"
    again = await client.post(_url(tail="/events"), json=body)
    assert again.status_code == 202 and again.json()["delivery"] == "queued"
    queued = (await fake_storage_provider.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=10))).items
    assert len(queued) == 1


async def test_a_retry_of_a_silent_event_records_nothing_more(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    body = {"element": "genre", "kind": "change", "value": "Lo-fi", "client_event_id": "c-2"}
    first = await client.post(_url(tail="/events"), json=body)
    again = await client.post(_url(tail="/events"), json=body)
    assert first.json()["delivery"] == again.json()["delivery"] == "recorded"
    assert len([r for r in ws.lines("s1") if r["kind"] == "canvas_event"]) == 1, "it waits in the canvas for the digest, once"


async def test_a_retry_after_the_click_left_the_ring_records_nothing(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    assert (await client.put("/v1/canvas_settings", json={"max_events": 10})).status_code == 200
    body = {"element": "play", "kind": "click", "client_event_id": "c-1"}
    assert (await client.post(_url(tail="/events"), json=body)).json()["delivery"] == "woken"
    for _ in range(12):
        await client.post(_url(tail="/events"), json={"element": "more", "kind": "open"})
    again = await client.post(_url(tail="/events"), json=body)
    assert again.status_code == 202, again.text
    assert again.json() == {"event_id": "e1", "rev": 1, "event_seq": 13, "delivery": "recorded"}
    assert (await _canvas(fake_storage_provider)).event_seq == 13 and len(ws.slots["s1"].appended) == 1


async def test_an_ended_session_refuses_events_with_409(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    await fake_storage_provider.get_storage(WorkspaceSession).patch_if(
        "s1", {"status": "ended", "ended_reason": "cancelled", "ended_at": datetime.now(UTC).isoformat()}, where={"status": ["waiting"]},
    )
    r = await client.post(_url(tail="/events"), json={"element": "play", "kind": "click"})
    assert r.status_code == 409 and r.json()["extensions"]["code"] == "session_ended"
    assert r.json()["detail"] == "This session has ended, so the canvas no longer takes clicks."
    assert (await client.get(_url())).json()["event_seq"] == 0
    assert await _platform(fake_storage_provider, "canvas.event") == []


#: (body, status, code). Every refusal records nothing.
REFUSALS = {
    "unknown element": ({"element": "nope", "kind": "click"}, 422, "unknown_element"),
    "a kind the element does not send": ({"element": "play", "kind": "change", "value": "x"}, 422, "kind_not_allowed"),
    "a value that is not an option": ({"element": "genre", "kind": "change", "value": "Polka"}, 422, "invalid_value"),
    "an emit without a name": ({"element": "viz", "kind": "emit"}, 422, "invalid_value"),
    "a position out of range": ({"element": "player", "kind": "play", "position": -1}, 422, "invalid_value"),
}


@pytest.mark.parametrize("case", list(REFUSALS))
async def test_event_refusals_map_to_422(client, fake_storage_provider, ws, case):
    await _seed(fake_storage_provider)
    await _draw(client)
    body, status, code = REFUSALS[case]
    r = await client.post(_url(tail="/events"), json=body)
    assert r.status_code == status and r.json()["extensions"]["code"] == code, r.text
    assert (await _canvas(fake_storage_provider)).event_seq == 0


async def test_a_disabled_element_refuses_its_events(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    await _draw(client, source="play.disabled = true")
    r = await client.post(_url(tail="/events"), json={"element": "play", "kind": "click"})
    assert r.status_code == 422 and r.json()["extensions"]["code"] == "element_disabled"


async def test_the_eleventh_click_of_a_minute_is_429(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider, status=SessionStatus.RUNNING, turn_status="running")
    await _draw(client)
    for _ in range(10):
        assert (await client.post(_url(tail="/events"), json={"element": "play", "kind": "click"})).status_code == 202
    r = await client.post(_url(tail="/events"), json={"element": "play", "kind": "click"})
    assert r.status_code == 429 and r.json()["extensions"]["code"] == "rate_limited"
    assert (await _canvas(fake_storage_provider)).event_seq == 10


async def test_the_silent_events_of_a_minute_are_capped_with_429(client, fake_storage_provider, ws):
    """The lead's ruling 3 on S6: past MAX_SILENT_EVENTS_PER_MINUTE the route answers 429 ``silent_rate_limited`` and records nothing."""
    await _seed(fake_storage_provider)
    await _draw(client)
    for _ in range(MAX_SILENT_EVENTS_PER_MINUTE):
        r = await client.post(_url(tail="/events"), json={"element": "more", "kind": "open"})
        assert r.status_code == 202 and r.json()["delivery"] == "recorded"
    before = (await _canvas(fake_storage_provider)).model_dump()
    r = await client.post(_url(tail="/events"), json={"element": "more", "kind": "open"})
    assert r.status_code == 429, r.text
    assert r.json()["extensions"]["code"] == "silent_rate_limited"
    assert r.json()["detail"] == (
        f"This canvas takes at most {MAX_SILENT_EVENTS_PER_MINUTE} events a minute that do not wake the agent. "
        "Wait a moment, then try again."
    )
    assert (await _canvas(fake_storage_provider)).model_dump() == before
    assert len(await _platform(fake_storage_provider, "canvas.event")) == MAX_SILENT_EVENTS_PER_MINUTE


async def test_the_event_body_is_bounded_before_it_is_read(client, fake_storage_provider, ws):
    """The #790 review, binding for S6: a body over MAX_EVENT_BODY_BYTES is 413 before the service walks ``value`` or ``data``."""
    await _seed(fake_storage_provider)
    await _draw(client)
    big = {"element": "viz", "kind": "emit", "name": "x", "data": {"blob": "y" * MAX_EVENT_BODY_BYTES}}
    r = await client.post(_url(tail="/events"), json=big)
    assert r.status_code == 413 and r.json()["extensions"]["code"] == "payload_too_large", r.text
    assert r.json()["extensions"]["limit_bytes"] == MAX_EVENT_BODY_BYTES
    assert (await _canvas(fake_storage_provider)).event_seq == 0
    r = await client.post(_url(tail="/events"), json={"element": "play", "kind": "click", "extra": 1})
    assert r.status_code == 422, "an unknown field is refused"


@pytest.mark.parametrize("method, tail, body", [
    ("get", "", None), ("post", "/ops", {"source": 'a = Text("x")'}), ("delete", "", None), ("post", "/events", {"element": "play", "kind": "click"}),
])
async def test_another_workspaces_session_is_404(client, fake_storage_provider, ws, method, tail, body):
    await _seed(fake_storage_provider, workspace_id="ws-2")
    kwargs = {"json": body} if body is not None else {}
    r = await getattr(client, method)(_url(tail=tail), **kwargs)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Session 's1' does not exist on workspace 'ws-1'"
    r = await getattr(client, method)(_url("nobody", tail), **kwargs)
    assert r.status_code == 404


async def test_the_read_does_not_fail_on_an_install_without_the_frame(client, fake_storage_provider, ws, tmp_path, monkeypatch):
    """Review of #908, round 2: no ``ui/`` gives ``frame_url: null``, never a 500; the canvas reads as it is."""
    from primer.api import _app_middleware

    await _seed(fake_storage_provider)
    await _draw(client)
    monkeypatch.setattr(_app_middleware, "_UI_DIR", tmp_path / "no-ui")
    r = await client.get(_url())
    assert r.status_code == 200, r.text
    assert r.json()["frame_url"] is None and r.json()["element_count"] == 8


async def test_file_urls_are_minted_for_referenced_files(client, fake_storage_provider, ws, raw_client):
    await _seed(fake_storage_provider)
    await _draw(client)
    body = (await client.get(_url())).json()
    assert set(body["file_urls"]) == {"file:assets/c.png"}
    assert body["file_urls"]["file:assets/c.png"].startswith("/canvas/sessions/s1/files/")
    assert referenced_files(body["elements"]) == {"assets/c.png"}


# ---- the shared read (ruling 2) ------------------------------------------------------------------------------------------------------


async def test_any_user_reads_the_values_and_events_with_their_input_while_the_feed_hides_it(client, carol, fake_storage_provider, ws, app):
    """The lead's ruling 2 on S6: GET .../canvas is the shared session read, values and the newest events WITH what was entered; the
    REST event feed keeps removing the input for a non-admin (#872)."""
    await _seed(fake_storage_provider, status=SessionStatus.RUNNING, turn_status="running", by=ADMIN)
    # The workspace as a row the access layer can judge (Phase 2, Task 7):
    # carol reads its event feed through a read grant.
    carol_id = await _user_id(app, "carol")
    await fake_storage_provider.get_storage(Workspace).create(Workspace(
        id=WS, template_id="t", provider_id="p",
        created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        access=Access(owners=[ADMIN.id], grants={carol_id: "read"}),
    ))
    await _draw(client)
    assert (await client.post(_url(tail="/events"), json={"element": "genre", "kind": "change", "value": "Classical"})).status_code == 202
    emit = {"element": "viz", "kind": "emit", "name": "submit", "data": {"note": SECRET_NOTE}}
    assert (await client.post(_url(tail="/events"), json=emit)).status_code == 202

    body = (await carol.get(_url())).json()
    assert body["values"] == {"genre": "Classical"} and body["value_set_by"] == {"genre": "user"}
    assert [(e["id"], e.get("value"), e.get("data")) for e in body["events"]] == [
        ("e1", "Classical", None), ("e2", None, {"note": SECRET_NOTE}),
    ]
    feed = (await carol.get("/v1/events", params={"workspace_id": WS, "event_type": "canvas.event"})).json()
    items = feed["items"] if isinstance(feed, dict) else feed
    assert len(items) == 2 and SECRET_NOTE not in json.dumps(items) and "Classical" not in json.dumps(items)


async def test_the_read_shows_the_newest_twenty_events_oldest_first(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    for _ in range(25):
        await client.post(_url(tail="/events"), json={"element": "more", "kind": "open"})
    events = (await client.get(_url())).json()["events"]
    assert [e["id"] for e in events] == [f"e{n}" for n in range(6, 26)]


async def test_reads_never_mark_events_seen(client, fake_storage_provider, ws):
    await _seed(fake_storage_provider)
    await _draw(client)
    await client.post(_url(tail="/events"), json={"element": "genre", "kind": "change", "value": "Lo-fi"})
    await client.get(_url())
    await client.get(_url(), params={"format": "lang"})
    row = await _canvas(fake_storage_provider)
    assert (row.seen_event_seq, row.seen_ahead) == (0, [])


# ---- the access gate (P2 Task 3) -------------------------------------------------------------------------------------------------------

#: action -> (method, tail, body, the answer of an accepted request).
ACTIONS = {
    "click": ("post", "/events", {"element": "play", "kind": "click"}, 202),
    "draw": ("post", "/ops", {"source": 'title.text = "call_tool system__create_llm_provider ..."'}, 200),
    "clear": ("delete", "", None, 200),
}


async def _creator(app, access_spec: str) -> PrincipalRef | None:
    """The row's initiator (realism only: the access gate no longer consults it)."""
    carol_id = await _user_id(app, "carol")
    return {
        "admin": ADMIN,
        "carol": PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
        "carol then admin": PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local"),
        "carol read": None,
    }[access_spec]


@pytest.mark.parametrize("who", list(WHO))
@pytest.mark.parametrize("action", list(ACTIONS))
async def test_a_persons_canvas_input_follows_the_workspace_access(app, client, carol, fake_storage_provider, ws, action, who):
    access_spec, asker, accepted = WHO[who]
    await _seed(fake_storage_provider, by=await _creator(app, access_spec))
    await _seed_workspace(fake_storage_provider, app, access_spec, await _user_id(app, "carol"), wid=WS)
    await _draw(client)                                    # the admin may draw on any session
    before_canvas = (await _canvas(fake_storage_provider)).model_dump(mode="json")
    before_session = (await fake_storage_provider.get_storage(WorkspaceSession).get("s1")).model_dump(mode="json")
    events_before = await fake_storage_provider.get_event_store().read_after(0)
    lines_before = list(ws.lines("s1"))
    method, tail, body, ok = ACTIONS[action]
    kwargs = {"json": body} if body is not None else {}

    if asker.endswith("token"):
        owner = await _user_id(app, "carol" if asker.startswith("carol") else "testuser")
        async with await _token_client(app, owner) as tok:
            r = await getattr(tok, method)(_url(tail=tail), **kwargs)
    else:
        r = await getattr({"carol": carol, "admin": client}[asker], method)(_url(tail=tail), **kwargs)

    if not accepted:
        if access_spec == "carol read":
            assert r.status_code == 403, f"{who} could {action}: {r.text}"
            assert r.json()["detail"] == "Session 's1' needs full access"
        else:
            assert r.status_code == 404, f"{who} could {action}: {r.text}"
            assert r.json()["detail"] == "Session 's1' does not exist"
        assert (await _canvas(fake_storage_provider)).model_dump(mode="json") == before_canvas, f"a refused {action} wrote the canvas"
        assert (await fake_storage_provider.get_storage(WorkspaceSession).get("s1")).model_dump(mode="json") == before_session
        assert await fake_storage_provider.get_event_store().read_after(0) == events_before, f"a refused {action} was announced"
        assert ws.lines("s1") == lines_before and "s1" not in ws.slots
        return
    assert r.status_code == ok, f"{who} could not {action}: {r.text}"
    after = await _canvas(fake_storage_provider)
    assert after.event_seq == 1 if action == "click" else after.rev == 2


@pytest.mark.parametrize("action", list(ACTIONS))
async def test_an_admin_acts_on_a_system_recorded_session_and_a_stranger_gets_the_missing_row(app, client, carol, fake_storage_provider, ws, action):
    await _seed(fake_storage_provider, by=PrincipalRef.system())
    await _seed_workspace(fake_storage_provider, app, "admin", await _user_id(app, "carol"), wid=WS)
    await _draw(client)
    method, tail, body, ok = ACTIONS[action]
    kwargs = {"json": body} if body is not None else {}
    refused = await getattr(carol, method)(_url(tail=tail), **kwargs)
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "Session 's1' does not exist"
    assert (await getattr(client, method)(_url(tail=tail), **kwargs)).status_code == ok


async def test_a_reader_reads_a_session_they_cannot_drive(app, client, carol, fake_storage_provider, ws):
    """Reading needs read on the session's workspace: carol's read grant admits the read and refuses the full acts (above)."""
    await _seed(fake_storage_provider, by=ADMIN)
    await _draw(client)
    await _seed_workspace(fake_storage_provider, app, "carol read", await _user_id(app, "carol"), wid=WS)
    r = await carol.get(_url())
    assert r.status_code == 200 and r.json()["element_count"] == 8


# ---- the session's delete (ruling 7) -----------------------------------------------------------------------------------------------


async def test_deleting_a_session_deletes_its_canvas(client, fake_storage_provider, ws, caplog):
    await _seed(fake_storage_provider)
    await _seed(fake_storage_provider, "s2")
    await _draw(client)
    await _draw(client, "s2")
    with caplog.at_level("WARNING"):
        r = await client.delete(f"/v1/workspaces/{WS}/sessions/s1")
    assert r.status_code == 204, r.text
    assert await _canvas(fake_storage_provider) is None
    assert await _canvas(fake_storage_provider, "s2") is not None, "only its own canvas"
    assert not [rec for rec in caplog.records if "canvas cleanup" in rec.getMessage()]


async def test_a_session_that_never_drew_deletes_quietly(client, fake_storage_provider, ws, caplog):
    await _seed(fake_storage_provider)
    with caplog.at_level("WARNING"):
        assert (await client.delete(f"/v1/workspaces/{WS}/sessions/s1")).status_code == 204
    assert not [rec for rec in caplog.records if "canvas cleanup" in rec.getMessage()], "no canvas is not a failure"


async def test_a_canvas_cleanup_that_fails_never_blocks_the_delete(client, fake_storage_provider, ws, caplog):
    await _seed(fake_storage_provider)
    await _draw(client)
    canvases = fake_storage_provider.get_storage(SessionCanvas)

    async def broken(*args, **kwargs):
        raise RuntimeError("the canvas table is unreachable")

    canvases.delete = broken
    with caplog.at_level("WARNING"):
        r = await client.delete(f"/v1/workspaces/{WS}/sessions/s1")
    assert r.status_code == 204, r.text
    assert await fake_storage_provider.get_storage(WorkspaceSession).get("s1") is None, "the session row is still removed"
    assert [rec.getMessage() for rec in caplog.records if "canvas cleanup" in rec.getMessage()] == [
        "delete_session: canvas cleanup failed (row still removed)",
    ]
