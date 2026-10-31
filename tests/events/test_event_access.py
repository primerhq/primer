"""Event reads and waits only see workspaces the person can read (access-control Phase 2, Task 7).

``GET /v1/events`` answers 404 when a non-admin names a workspace they cannot read. A
non-admin ``wait_for_event`` is pinned to workspaces the run's person can read: an
existing ``eq`` matcher on ``workspace_id`` must name one, otherwise the run's own
workspace is pinned (workspace-less events then never match: a missing path never
matches). ``session.wake`` is no longer user-safe: it carries no workspace and holds
the wake payload. And the dispatcher re-checks at delivery that the park's session's
turn person can still read the event's workspace, so a revoked grant stops delivery.
Admins are unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import json

import httpx
import pytest_asyncio
from httpx import ASGITransport

from tests.events._users import seed_account_of
from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.passwords import hash_password
from primer.events import visibility
from primer.events.dispatcher import EventDispatcher
from primer.model.access import Access
from primer.model.event import (
    EventFilter,
    EventSubscription,
    SessionWakeSink,
)
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.events_wait import make_wait_for_event_handler

USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


async def _seed_workspace(sp: SqliteStorageProvider, ws_id: str, *, owners: list[str], grants: dict[str, str] | None = None) -> None:
    await sp.get_storage(Workspace).create(Workspace(
        id=ws_id, template_id="t", provider_id="p",
        created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        access=Access(owners=owners, grants=grants or {}),
    ))


async def _park(sp: SqliteStorageProvider, session_id: str, workspace_id: str, person: PrincipalRef, event_key: str) -> None:
    await seed_account_of(sp, person)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=session_id, workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(UTC),
        parked_status="parked",
        parked_event_key=event_key,
        turn_person=person,
    ))


def _ctx(who: PrincipalRef, workspace_id: str = "w-own") -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id=workspace_id, initiated_by=who)


async def _subs(sp: SqliteStorageProvider) -> list[EventSubscription]:
    return (await sp.get_storage(EventSubscription).list(OffsetPage(length=50))).items


# ---------------------------------------------------------------------------
# (c) session.wake is not user-safe
# ---------------------------------------------------------------------------


def test_session_wake_is_not_user_safe():
    assert visibility.user_safe("session.wake") is False
    assert visibility.user_safe("session.ended") is True
    # A prefix wait on session.* stays ALLOWED: the workspace pin and the
    # delivery re-check are what keep the wake envelope out of a non-admin.
    assert visibility.user_safe_glob("session.*") is True


async def test_a_non_admin_may_not_wait_on_session_wake(sp):  # noqa: F811
    result = await make_wait_for_event_handler(sp)({"event_types": ["session.wake"]}, ctx=_ctx(USER))

    assert result.is_error and json.loads(result.output)["type"] == "forbidden", result
    assert await _subs(sp) == []


# ---------------------------------------------------------------------------
# (a) GET /v1/events: the named workspace must be readable
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def rest(sp):  # noqa: F811
    """The real app with a registered admin and one other user (u-other)."""
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    admin = httpx.AsyncClient(transport=transport, base_url="http://test")
    reg = await admin.post("/v1/auth/register", json={"username": "evadmin", "password": "evadminpass1"})
    assert reg.status_code == 200, reg.text
    admin_uid = next(
        u.id for u in (await sp.get_storage(User).list(OffsetPage(length=10))).items
        if u.username == "evadmin"
    )
    await sp.get_storage(User).create(User(
        id="u-other", username="other", password_hash=await hash_password("otherpass1"),
        created_at=datetime.now(UTC), role="user",
    ))
    other = httpx.AsyncClient(transport=transport, base_url="http://test")
    login = await other.post("/v1/auth/login", json={"username": "other", "password": "otherpass1"})
    assert login.status_code == 200, login.text
    try:
        yield sp, admin, other, admin_uid, "u-other"
    finally:
        await admin.aclose()
        await other.aclose()


async def test_events_feed_is_404_for_a_workspace_the_reader_cannot_read(rest):
    sp, admin, other, admin_uid, other_uid = rest
    await _seed_workspace(sp, "w-secret", owners=[admin_uid])
    r = await other.get("/v1/events?workspace_id=w-secret")
    assert r.status_code == 404, r.text
    # A workspace that does not exist answers the same 404 (spec 9).
    assert (await other.get("/v1/events?workspace_id=no-such-workspace")).status_code == 404

    # Sharing read un-hides the feed for the reader.
    row = await sp.get_storage(Workspace).get("w-secret")
    await sp.get_storage(Workspace).update(row.model_copy(update={
        "access": Access(owners=[admin_uid], grants={other_uid: "read"}),
    }))
    r = await other.get("/v1/events?workspace_id=w-secret")
    assert r.status_code == 200, r.text

    # The admin reads the same feed, without the share.
    row = await sp.get_storage(Workspace).get("w-secret")
    await sp.get_storage(Workspace).update(row.model_copy(update={"access": Access(owners=[admin_uid])}))
    assert (await admin.get("/v1/events?workspace_id=w-secret")).status_code == 200


async def test_events_feed_hides_session_wake_from_a_non_admin(rest):
    sp, admin, other, admin_uid, other_uid = rest
    await _seed_workspace(sp, "w-read", owners=[admin_uid], grants={other_uid: "read"})
    store = sp.get_event_store()
    await store.append(event_type="session.ended", workspace_id="w-read")
    await store.append(event_type="session.wake", workspace_id="w-read")
    await store.append(event_type="agent.updated", workspace_id="w-read")

    r = await other.get("/v1/events?workspace_id=w-read")
    assert r.status_code == 200, r.text
    types = [e["event_type"] for e in r.json()["items"]]
    assert "session.ended" in types
    assert "session.wake" not in types, "session.wake is no longer user-safe"
    assert "agent.updated" not in types

    # The admin reads the full log without naming a workspace: session.wake included.
    r = await admin.get("/v1/events")
    assert r.status_code == 200, r.text
    assert "session.wake" in [e["event_type"] for e in r.json()["items"]]


# ---------------------------------------------------------------------------
# (b) wait_for_event is pinned to workspaces the person can read
# ---------------------------------------------------------------------------


async def test_wait_for_event_is_scoped_to_readable_workspaces(sp):  # noqa: F811
    await _seed_workspace(sp, "w-own", owners=["u-admin"], grants={"u-1": "read"})
    await _seed_workspace(sp, "w-other", owners=["u-admin"])
    handler = make_wait_for_event_handler(sp)

    # A named workspace the run cannot read is refused, before anything is stored.
    result = await handler(
        {"event_types": ["session.ended"], "fields": [
            {"path": "workspace_id", "op": "eq", "value": "w-other"}]},
        ctx=_ctx(USER),
    )
    assert result.is_error and json.loads(result.output)["type"] == "forbidden", result
    assert await _subs(sp) == [], "a refused wait stores no subscription"

    # A named workspace the run can read is kept, unpinned-and-unaltered.
    result = await handler(
        {"event_types": ["session.ended"], "fields": [
            {"path": "workspace_id", "op": "eq", "value": "w-own"}]},
        ctx=_ctx(USER),
    )
    assert isinstance(result, Yielded), result
    [stored] = await _subs(sp)
    assert [(m.path, m.op, m.value) for m in stored.filter.fields] == [("workspace_id", "eq", "w-own")]
    await _clear_subs(sp)

    # Without a matcher the run's own workspace is pinned.
    result = await handler({"event_types": ["session.ended"]}, ctx=_ctx(USER))
    assert isinstance(result, Yielded), result
    [stored] = await _subs(sp)
    assert [(m.path, m.op, m.value) for m in stored.filter.fields] == [("workspace_id", "eq", "w-own")]
    await _clear_subs(sp)

    # An admin's wait stores no pin and may name any kind.
    result = await handler({"event_types": ["agent.*"]}, ctx=_ctx(ADMIN))
    assert isinstance(result, Yielded), result
    [stored] = await _subs(sp)
    assert stored.filter.fields == []


async def _clear_subs(sp: SqliteStorageProvider) -> None:
    subs = sp.get_storage(EventSubscription)
    for row in (await subs.list(OffsetPage(length=50))).items:
        await subs.delete(row.id)


# ---------------------------------------------------------------------------
# (d) the delivery re-check: a revoked grant stops delivery
# ---------------------------------------------------------------------------


async def test_a_revoked_grant_stops_wake_delivery(sp):  # noqa: F811
    await _seed_workspace(sp, "w", owners=["u-admin"], grants={"u-1": "read"})
    await _park(sp, "s-1", "w", USER, "evwait:s-1:c1")
    await sp.get_storage(EventSubscription).create(EventSubscription(
        id="sub-1", description="t",
        filter=EventFilter(event_types=["workspace.updated"]),
        sink=SessionWakeSink(event_key="evwait:s-1:c1", session_id="s-1"),
    ))
    bus = _RecordingBus()
    d = EventDispatcher(storage_provider=sp, event_bus=bus)

    # Pin the cursor at the head (a fresh subscription consumes from now),
    # then revoke the grant before the matching event lands.
    await d.drain_once()
    row = await sp.get_storage(Workspace).get("w")
    await sp.get_storage(Workspace).update(row.model_copy(update={"access": Access(owners=["u-admin"])}))
    event_id = await sp.get_event_store().append(event_type="workspace.updated", workspace_id="w")
    await d.drain_once()
    assert bus.published == [], "the revoked grant stops delivery"
    assert await sp.get_storage(EventSubscription).get("sub-1") is not None, (
        "the wait is left to its timeout, not garbage-collected"
    )
    assert await sp.get_event_store().get_cursor("sub-1") == event_id, (
        "the event is consumed, not held for retry"
    )

    # A later event under a RESTORED grant still wakes the same park.
    row = await sp.get_storage(Workspace).get("w")
    await sp.get_storage(Workspace).update(row.model_copy(update={
        "access": Access(owners=["u-admin"], grants={"u-1": "read"}),
    }))
    await sp.get_event_store().append(event_type="workspace.updated", workspace_id="w")
    await d.drain_once()
    assert [key for key, _ in bus.published] == ["evwait:s-1:c1"]


async def test_a_workspaceless_event_never_wakes_a_non_admin_park(sp):  # noqa: F811
    await _seed_workspace(sp, "w", owners=["u-admin"], grants={"u-1": "read"})
    await _park(sp, "s-1", "w", USER, "evwait:s-1:c1")
    subs = sp.get_storage(EventSubscription)
    await subs.create(EventSubscription(
        id="sub-1", description="t",
        filter=EventFilter(event_types=["trigger.fired"]),
        sink=SessionWakeSink(event_key="evwait:s-1:c1", session_id="s-1"),
    ))
    bus = _RecordingBus()
    d = EventDispatcher(storage_provider=sp, event_bus=bus)
    await d.drain_once()  # pin the cursor at the head

    await sp.get_event_store().append(event_type="trigger.fired")  # no workspace_id
    await d.drain_once()
    assert bus.published == [], "an event without a workspace never wakes a non-admin park"
    assert await subs.get("sub-1") is not None

    # The same event wakes an ADMIN's park.
    await _park(sp, "s-2", "w", ADMIN, "evwait:s-2:c2")
    await subs.create(EventSubscription(
        id="sub-2", description="t",
        filter=EventFilter(event_types=["trigger.fired"]),
        sink=SessionWakeSink(event_key="evwait:s-2:c2", session_id="s-2"),
    ))
    await d.drain_once()  # pin sub-2's cursor at the head
    await sp.get_event_store().append(event_type="trigger.fired")
    await d.drain_once()
    assert [key for key, _ in bus.published] == ["evwait:s-2:c2"]


async def test_an_admin_park_wakes_from_a_workspace_only_the_admin_reads(sp):  # noqa: F811
    await _seed_workspace(sp, "w", owners=["u-admin"])  # u-1 holds nothing on w
    await _park(sp, "s-1", "w", ADMIN, "evwait:s-1:c1")
    await sp.get_storage(EventSubscription).create(EventSubscription(
        id="sub-1", description="t",
        filter=EventFilter(event_types=["workspace.updated"]),
        sink=SessionWakeSink(event_key="evwait:s-1:c1", session_id="s-1"),
    ))
    bus = _RecordingBus()
    d = EventDispatcher(storage_provider=sp, event_bus=bus)
    await d.drain_once()  # pin the cursor at the head
    await sp.get_event_store().append(event_type="workspace.updated", workspace_id="w")
    await d.drain_once()
    assert [key for key, _ in bus.published] == ["evwait:s-1:c1"]
