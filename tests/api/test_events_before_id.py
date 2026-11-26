"""GET /v1/events?before_id=: the log read backwards, so a filtered window can reach events older than the newest ids (review 8.2).

The store only reads forward; the route walks the id space downward in bounded windows and hands back ``next_before_id`` when a
page is short because the scan budget ran out.
"""

from __future__ import annotations

import pytest

from primer.api.routers import events as events_module
from tests.api.conftest import raw_client as client, app  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user, _seed_workspace


async def _seed(app, rows: list[tuple[str, str | None]]) -> list[int]:
    store = app.state.storage_provider.get_event_store()
    return [await store.append(event_type=t, workspace_id=ws) for t, ws in rows]


async def test_pages_back_through_a_filtered_log_past_the_newest_ids(client, app):
    await _register_admin(client)
    ids = await _seed(app, [("agent.created", None)] + [("session.started", "ws-a"), ("session.finished", "ws-a")] * 2 + [("agent.created", None)] * 30)
    sessions = ids[1:5]
    first = (await client.get("/v1/events", params={"limit": 2, "event_type": "session.*", "before_id": ids[-1] + 1})).json()
    # The newest two session events live under 30 newer agent events: the old fixed window of the newest 100 ids would not have shown them.
    assert [e["id"] for e in first["items"]] == sessions[2:]
    assert first["next_before_id"] == sessions[2]
    second = (await client.get("/v1/events", params={"limit": 2, "event_type": "session.*", "before_id": first["next_before_id"]})).json()
    assert [e["id"] for e in second["items"]] == sessions[:2]
    third = (await client.get("/v1/events", params={"limit": 2, "event_type": "session.*", "before_id": second["next_before_id"]})).json()
    assert third["items"] == [] and third["next_before_id"] is None


async def test_a_sparse_filter_is_bounded_per_request_and_resumes_by_cursor(client, app, monkeypatch):
    monkeypatch.setattr(events_module, "_BACK_WINDOW", 5)
    monkeypatch.setattr(events_module, "_BACK_MAX_WINDOWS", 2)
    await _register_admin(client)
    ids = await _seed(app, [("session.started", "ws-a")] + [("agent.created", None)] * 40)
    first = (await client.get("/v1/events", params={"limit": 10, "workspace_id": "ws-a", "before_id": ids[-1] + 1})).json()
    # Two windows of five ids cover ids 32..41, no match: a short (empty) page that says where to resume.
    assert first["items"] == []
    assert first["next_before_id"] == ids[-1] + 1 - 10
    cursor, found = first["next_before_id"], []
    for _ in range(10):
        page = (await client.get("/v1/events", params={"limit": 10, "workspace_id": "ws-a", "before_id": cursor})).json()
        found += [e["id"] for e in page["items"]]
        cursor = page["next_before_id"]
        if cursor is None:
            break
    assert found == [ids[0]]


async def test_a_page_that_spans_windows_returns_the_newest_matches_not_the_oldest(client, app, monkeypatch):
    # The walk reads the windows newest first; a page whose matches sit in more than one window must still answer with the NEWEST
    # ``limit`` matches (in id order), and its cursor must sit at the page's oldest id, so the next read is strictly below it.
    monkeypatch.setattr(events_module, "_BACK_WINDOW", 10)
    rows = [("agent.created", None)] * 41
    for i in (4, 7, 19, 29):  # matches at ids 5, 8, 20, 30: three different windows of ten ids
        rows[i] = ("session.started", "ws-a")
    await _register_admin(client)
    ids = await _seed(app, rows)
    first = (await client.get("/v1/events", params={"limit": 3, "event_type": "session.*", "before_id": ids[-1] + 1})).json()
    assert [e["id"] for e in first["items"]] == [ids[7], ids[19], ids[29]], "the newest three matches, not the older window's rows"
    assert first["next_before_id"] == ids[7], "the cursor sits at the page's oldest id"
    second = (await client.get("/v1/events", params={"limit": 3, "event_type": "session.*", "before_id": first["next_before_id"]})).json()
    assert [e["id"] for e in second["items"]] == [ids[4]]
    assert second["next_before_id"] is None, "nothing older is left"


async def test_before_id_is_clamped_to_the_head_and_refuses_after_id(client, app):
    await _register_admin(client)
    ids = await _seed(app, [("session.started", "ws-a")] * 3)
    huge = (await client.get("/v1/events", params={"before_id": 10**15})).json()
    assert [e["id"] for e in huge["items"]] == ids and huge["max_id"] == ids[-1]
    both = await client.get("/v1/events", params={"before_id": 5, "after_id": 1})
    assert both.status_code == 422, both.text


async def test_a_non_admin_reads_back_only_their_workspaces_user_safe_events(client, app):
    await _register_admin(client)
    alice = await _seed_user(app, "alice-back")
    await _seed_workspace(app, "ws-back", alice)
    ids = await _seed(
        app,
        [("session.started", "ws-back"), ("session.wake", "ws-back"), ("session.finished", "ws-back"), ("session.started", "ws-other")],
    )
    await _login(client, "alice-back")
    body = (await client.get("/v1/events", params={"workspace_id": "ws-back", "before_id": ids[-1] + 1})).json()
    assert [e["id"] for e in body["items"]] == [ids[0], ids[2]]
    # Refused without a workspace, exactly as the forward read is.
    assert (await client.get("/v1/events", params={"before_id": 5})).status_code == 422
