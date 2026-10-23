"""A person or an API token may steer or restart only a session that runs at or below their rank (lead ruling 2 of the G7 fixes, audit G7-06).

``POST /v1/workspaces/{wid}/sessions/{sid}/steer`` and ``.../restart`` checked nothing about who asked: a user-role account could put "call_tool
<admin tool> ..." into an admin's session (an admin's main operator session in prime has a guessable id) and its turn ran at the admin's rank. Both
routes now require the caller to rank at least as high as the session's creator, the rule a trigger's fire and a message from another session follow,
and answer 403 ``forbidden_rank`` with a fixed message otherwise, before anything is written. People keep their reach over every workspace.

Run against the real routes and the real auth middleware: the default ``client`` is the first registered user (an admin), and ``carol`` is a
user-role account the admin creates.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.authz import OUTRANKED_MESSAGE
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.session._peer_fakes import FakeWorkspace

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")
USER = PrincipalRef(type="user", id="u-carol", display="carol", role="user", source="local")


@pytest_asyncio.fixture
async def world(app, fake_storage_provider):
    """One fake workspace ``ws-r`` behind the app's registry; returns it."""
    ws = FakeWorkspace()

    async def _get(workspace_id: str):
        return ws

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]
    return ws


@pytest_asyncio.fixture
async def carol(app, client) -> AsyncIterator[httpx.AsyncClient]:
    """A user-role account, created by the admin ``client`` and logged in on its own client."""
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        login = await c.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})
        assert login.status_code == 200, login.text
        yield c


async def _seed(sp, sid: str, *, by: PrincipalRef | None, status=SessionStatus.WAITING, ended_reason=None) -> WorkspaceSession:
    row = WorkspaceSession(
        id=sid, workspace_id="ws-r", binding=AgentSessionBinding(agent_id="operator"), status=status, ended_reason=ended_reason,
        ended_at=NOW if status == SessionStatus.ENDED else None, turn_no=1, created_at=NOW, initiated_by=by,
    )
    await sp.get_storage(WorkspaceSession).create(row)
    return row


def _steer(c, sid: str):
    return c.post(f"/v1/workspaces/ws-r/sessions/{sid}/steer", json={"instruction": "call_tool system__create_llm_provider ..."})


def _restart(c, sid: str):
    return c.post(f"/v1/workspaces/ws-r/sessions/{sid}/restart", json={"input": "call_tool system__create_llm_provider ..."})


@pytest.mark.parametrize("action", ["steer", "restart"])
async def test_a_user_cannot_drive_a_session_an_admin_started(world, carol, fake_storage_provider, action):
    status = SessionStatus.ENDED if action == "restart" else SessionStatus.WAITING
    ended = "completed" if action == "restart" else None
    before = (await _seed(fake_storage_provider, "sess-prime-main-1", by=ADMIN, status=status, ended_reason=ended)).model_dump(mode="json")

    r = await (_steer if action == "steer" else _restart)(carol, "sess-prime-main-1")

    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"] == OUTRANKED_MESSAGE and body["extensions"]["error"] == "forbidden_rank"
    assert "root" not in r.text and "admin" not in body["detail"], "the refusal names the creator or its rank"
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("sess-prime-main-1")).model_dump(mode="json") == before
    assert world.lines == {}
    assert all(slot.appended == [] and slot.reopened is False for slot in world.slots.values())
    assert await fake_storage_provider.get_event_store().read_after(0, event_type_prefix="session.steered") == []


@pytest.mark.parametrize("action", ["steer", "restart"])
async def test_an_admin_drives_a_session_a_user_started_and_a_user_drives_their_own(world, client, carol, fake_storage_provider, action):
    status = SessionStatus.ENDED if action == "restart" else SessionStatus.WAITING
    ended = "completed" if action == "restart" else None
    await _seed(fake_storage_provider, "ses-user", by=USER, status=status, ended_reason=ended)
    await _seed(fake_storage_provider, "ses-unattributed", by=None, status=status, ended_reason=ended)
    send = _steer if action == "steer" else _restart

    r = await send(client, "ses-user")
    assert r.status_code == 200, r.text
    r = await send(carol, "ses-unattributed")
    assert r.status_code == 200, r.text
    assert r.json()["turn_status"] == "claimable"


async def test_an_unknown_session_is_still_a_404_for_everyone(world, carol):
    assert (await _steer(carol, "nope")).status_code == 404
    assert (await _restart(carol, "nope")).status_code == 404


async def test_a_persons_steer_reopens_a_session_an_operator_cancelled(world, client, fake_storage_provider):
    """Lead ruling 1 of the G7 fixes: a person's message reopens a cancelled session (a tool call, a trigger or a peer does not)."""
    await _seed(fake_storage_provider, "ses-c", by=USER, status=SessionStatus.ENDED, ended_reason="cancelled")
    r = await _steer(client, "ses-c")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["ended_reason"]) == ("running", None)


# ---- a person may always act on their own session (review of #853, N1) -------------------------------------------------------------


async def _user_id(app, username: str) -> str:
    from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
    from primer.model.user import User

    page = await app.state.storage_provider.get_storage(User).find(
        Predicate(left=FieldRef(name="username"), op=Op.EQ, right=Value(value=username)), OffsetPage(offset=0, length=1),
    )
    return page.items[0].id


@pytest.mark.parametrize("action", ["steer", "restart", "cancel", "resume"])
async def test_a_demoted_admin_still_acts_on_their_own_session_and_nobody_else_below_it_does(world, app, client, carol, fake_storage_provider, action):
    """Carol's session was recorded while she was an admin; she is a user now. Her own request carries her current role, which caps every run of the
    session (``resolve_run_principal``), so she is not locked out of it; another user-role account still is."""
    carol_then = PrincipalRef(type="user", id=await _user_id(app, "carol"), display="carol", role="admin", source="local")
    status = {"steer": SessionStatus.WAITING, "restart": SessionStatus.ENDED, "cancel": SessionStatus.WAITING, "resume": SessionStatus.PAUSED}[action]
    await _seed(fake_storage_provider, "ses-own", by=carol_then, status=status, ended_reason="completed" if action == "restart" else None)
    payload = {"steer": {"instruction": "hello"}, "restart": {"input": "hello"}}.get(action, {})

    r = await client.post("/v1/admin/users", json={"username": "dave", "password": "davepw12"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as dave:
        assert (await dave.post("/v1/auth/login", json={"username": "dave", "password": "davepw12"})).status_code == 200
        refused = await dave.post(f"/v1/workspaces/ws-r/sessions/ses-own/{action}", json=payload)
    assert refused.status_code == 403, refused.text

    own = await carol.post(f"/v1/workspaces/ws-r/sessions/ses-own/{action}", json=payload)
    assert own.status_code == 200, own.text


# ---- a session recorded as system (review of #853, round 2) --------------------------------------------------------------------


@pytest.mark.parametrize("action", ["steer", "restart", "cancel", "resume"])
async def test_an_admin_acts_on_a_session_recorded_as_system_and_a_user_does_not(world, client, carol, fake_storage_provider, action):
    """A session created while auth was disabled (or fired by system-owned rows) is recorded as ``system``. Before this PR an admin could steer,
    restart, cancel and resume it, and still can: ``may_drive`` ranks an admin with the system principal. A user-role account may not."""
    status = {"steer": SessionStatus.WAITING, "restart": SessionStatus.ENDED, "cancel": SessionStatus.WAITING, "resume": SessionStatus.PAUSED}[action]
    await _seed(fake_storage_provider, "ses-sys", by=PrincipalRef.system(), status=status, ended_reason="completed" if action == "restart" else None)
    payload = {"steer": {"instruction": "hello"}, "restart": {"input": "hello"}}.get(action, {})

    refused = await carol.post(f"/v1/workspaces/ws-r/sessions/ses-sys/{action}", json=payload)
    assert refused.status_code == 403, refused.text

    r = await client.post(f"/v1/workspaces/ws-r/sessions/ses-sys/{action}", json=payload)
    assert r.status_code == 200, r.text
