"""PERF-T10 (M9) perf: the events route never starves a page behind filtered rows, and ``max_id`` costs a query only for admins.

The answer per caller (same items, same ``max_id``) is pinned by the characterization halves of this file, and they stay green
through the change wherever the first ``limit`` rows all survive the Python-side filter. The starvation regression (a page whose
first rows are filtered out still returns up to ``limit``) is RED before the fix; the ``max_id`` count guards are green pins.
"""

from __future__ import annotations

from tests.api.conftest import raw_client as client, app  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user, _seed_workspace
from tests.perf.counting import CountingStorageProvider


async def _seed_events(app, rows: list[tuple[str, str | None]]):
    store = app.state.storage_provider.get_event_store()
    for event_type, ws in rows:
        await store.append(event_type=event_type, workspace_id=ws)


async def test_an_admin_reads_the_full_log_with_a_max_id(client, app):
    await _register_admin(client)
    await _seed_events(
        app,
        [
            ("session.started", "ws-x"),      # 1
            ("session.wake", "ws-x"),         # 2: unsafe for non-admins, still the admin's
            ("agent.turn", None),             # 3: not user-safe at all, still the admin's
        ],
    )
    response = await client.get("/v1/events", params={"limit": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["event_type"] for e in body["items"]] == ["session.started", "session.wake", "agent.turn"]
    assert body["max_id"] == 3

    # The glob pushes its literal head into the query and the page keeps its content.
    response = await client.get("/v1/events", params={"limit": 10, "event_type": "session.*"})
    body = response.json()
    assert [e["event_type"] for e in body["items"]] == ["session.started", "session.wake"]
    assert body["max_id"] == 3


async def test_a_non_admin_reads_only_the_user_safe_feed_of_their_workspace(client, app):
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    await _seed_workspace(app, "ws-alice", alice)
    await _seed_events(
        app,
        [
            ("session.started", "ws-alice"),   # 1: user-safe
            ("session.finished", "ws-alice"),  # 2: user-safe
            ("session.wake", "ws-other"),      # 3: another workspace's unsafe event, never in the feed
        ],
    )
    await _login(client, "alice")
    response = await client.get("/v1/events", params={"limit": 10, "workspace_id": "ws-alice"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["event_type"] for e in body["items"]] == ["session.started", "session.finished"]
    # The head is the newest event of the readable workspace they named, not the global log's.
    assert body["max_id"] == 2


async def test_a_page_whose_first_rows_are_filtered_out_is_not_starved(client, app):
    """Regression: with 3 unsafe rows ahead of the safe ones, a limit=3 page comes back empty before the fix
    (the reader polls ``after_id=0`` forever and never reaches the safe rows behind)."""
    await _register_admin(client)
    alice = await _seed_user(app, "alice-starve")
    await _seed_workspace(app, "ws-starve", alice)
    await _seed_events(
        app,
        [
            ("session.wake", "ws-starve"),       # 1-3: filtered out for a non-admin
            ("session.wake", "ws-starve"),
            ("session.wake", "ws-starve"),
            ("session.started", "ws-starve"),    # 4-5: the rows the page must actually return
            ("session.started", "ws-starve"),
        ],
    )
    await _login(client, "alice-starve")
    response = await client.get("/v1/events", params={"limit": 3, "workspace_id": "ws-starve"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["event_type"] for e in body["items"]] == ["session.started", "session.started"]
    assert body["max_id"] == 5


async def test_max_id_costs_a_query_only_for_admins(client, app):
    await _register_admin(client)
    alice = await _seed_user(app, "alice-max")
    await _seed_workspace(app, "ws-max", alice)
    await _seed_events(app, [("session.started", "ws-max")])

    original = app.state.storage_provider
    csp = CountingStorageProvider(original)
    app.state.storage_provider = csp
    try:
        with csp.counted() as c:
            admin = await client.get("/v1/events")
            await _login(client, "alice-max")
            alice_feed = await client.get("/v1/events", params={"workspace_id": "ws-max"})
    finally:
        app.state.storage_provider = original

    assert admin.status_code == 200, admin.text
    assert alice_feed.status_code == 200, alice_feed.text
    assert c.calls("max_id") == 1  # the admin's global head; the non-admin's head comes from the page
