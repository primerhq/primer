"""A session's access is the access of the workspace it lives in (access-control spec 9; plan P2 Task 3): the rank rule is gone.

``POST /v1/workspaces/{wid}/sessions/{sid}/steer`` and ``.../restart`` used to check the caller's rank against the session's creator's rank
(lead ruling 2 of the G7 fixes, audit G7-06): a user-role account could put "call_tool <admin tool> ..." into an admin's session and its turn
ran at the admin's rank. The gate now is the workspace access the session inherits: the caller must hold full on the workspace the session
lives in, before anything is routed, written or announced. A stranger to that workspace answers the session's missing-row 404 (spec 9,
indistinguishable from a missing id); a holder of a lower level answers 403 with the level named. There is no own-session bypass: a creator
whose grant is revoked loses access (the access probes pin it). Admins and the system principal clear every gate.

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
from pydantic import SecretStr

from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
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


async def _user_id(app, username: str) -> str:
    from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
    from primer.model.user import User

    page = await app.state.storage_provider.get_storage(User).find(
        Predicate(left=FieldRef(name="username"), op=Op.EQ, right=Value(value=username)), OffsetPage(offset=0, length=1),
    )
    return page.items[0].id


def _access_for(spec: str, carol_id: str, admin_id: str) -> Access:
    """The workspace access an access test seeds, by spec (the session inherits it)."""
    return {
        "admin": Access(owners=[admin_id]),
        "carol": Access(owners=[carol_id]),
        "carol then admin": Access(owners=[carol_id]),  # the grant follows the identity, not the recorded rank
        "carol read": Access(owners=[admin_id], grants={carol_id: "read"}),
    }[spec]


async def _workspace(sp, app, spec: str, carol_id: str, wid: str = "ws-r") -> None:
    """The workspace row the test's session lives in (the session's access is its workspace's)."""
    await sp.get_storage(Workspace).create(Workspace(
        id=wid, template_id="tpl-1", provider_id="local-1", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub-token")),
        access=_access_for(spec, carol_id, await _user_id(app, "testuser")),
    ))


def _steer(c, sid: str):
    return c.post(f"/v1/workspaces/ws-r/sessions/{sid}/steer", json={"instruction": "call_tool system__create_llm_provider ..."})


def _restart(c, sid: str):
    return c.post(f"/v1/workspaces/ws-r/sessions/{sid}/restart", json={"input": "call_tool system__create_llm_provider ..."})


@pytest.mark.parametrize("action", ["steer", "restart"])
async def test_a_user_cannot_drive_a_session_in_an_admins_workspace(world, app, carol, fake_storage_provider, action):
    """A stranger to the workspace answers the session's missing-row 404 (spec 9): the row is indistinguishable from one that is not there."""
    status = SessionStatus.ENDED if action == "restart" else SessionStatus.WAITING
    ended = "completed" if action == "restart" else None
    before = (await _seed(fake_storage_provider, "sess-prime-main-1", by=ADMIN, status=status, ended_reason=ended)).model_dump(mode="json")
    carol_id = await _user_id(app, "carol")
    await _workspace(fake_storage_provider, app, "admin", carol_id)

    r = await (_steer if action == "steer" else _restart)(carol, "sess-prime-main-1")

    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Session 'sess-prime-main-1' does not exist"
    assert "root" not in r.text and "admin" not in r.json()["detail"], "the refusal names the creator or its rank"
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("sess-prime-main-1")).model_dump(mode="json") == before
    assert world.lines == {}
    assert all(slot.appended == [] and slot.reopened is False for slot in world.slots.values())
    assert await fake_storage_provider.get_event_store().read_after(0, event_type_prefix="session.steered") == []


@pytest.mark.parametrize("action", ["steer", "restart"])
async def test_an_admin_drives_any_session_and_a_workspace_owner_drives_their_own(world, app, client, carol, fake_storage_provider, action):
    """The admin clears every gate; for carol it is the workspace access, not the row's initiator, that admits her: she drives the
    unattributed row as well as her own."""
    status = SessionStatus.ENDED if action == "restart" else SessionStatus.WAITING
    ended = "completed" if action == "restart" else None
    await _seed(fake_storage_provider, "ses-user", by=USER, status=status, ended_reason=ended)
    await _seed(fake_storage_provider, "ses-unattributed", by=None, status=status, ended_reason=ended)
    carol_id = await _user_id(app, "carol")
    await _workspace(fake_storage_provider, app, "carol", carol_id)
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


# ---- the rank no longer decides: a demoted admin keeps her grant, a stranger gets the missing row ----------------------------------------


@pytest.mark.parametrize("action", ["steer", "restart", "cancel", "resume"])
async def test_a_demoted_admin_acts_on_their_own_workspace_and_a_stranger_gets_the_missing_row(world, app, client, carol, fake_storage_provider, action):
    """Carol's session was recorded while she was an admin; she is a user now. What decides is the workspace access, not the recorded rank
    or the row's initiator: she owns the workspace, so her acts land; dave holds nothing on it and gets the session's missing-row 404."""
    carol_id = await _user_id(app, "carol")
    carol_then = PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local")
    status = {"steer": SessionStatus.WAITING, "restart": SessionStatus.ENDED, "cancel": SessionStatus.WAITING, "resume": SessionStatus.PAUSED}[action]
    await _seed(fake_storage_provider, "ses-own", by=carol_then, status=status, ended_reason="completed" if action == "restart" else None)
    await _workspace(fake_storage_provider, app, "carol", carol_id)
    payload = {"steer": {"instruction": "hello"}, "restart": {"input": "hello"}}.get(action, {})

    r = await client.post("/v1/admin/users", json={"username": "dave", "password": "davepw12"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as dave:
        assert (await dave.post("/v1/auth/login", json={"username": "dave", "password": "davepw12"})).status_code == 200
        refused = await dave.post(f"/v1/workspaces/ws-r/sessions/ses-own/{action}", json=payload)
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "Session 'ses-own' does not exist"

    own = await carol.post(f"/v1/workspaces/ws-r/sessions/ses-own/{action}", json=payload)
    assert own.status_code == 200, own.text


# ---- a session recorded as system ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["steer", "restart", "cancel", "resume"])
async def test_an_admin_acts_on_a_system_recorded_session_and_a_stranger_gets_the_missing_row(world, app, client, carol, fake_storage_provider, action):
    """A session created while auth was disabled (or fired by system-owned rows) is recorded as ``system``. The gate is the workspace
    access, not the recorded identity: the admin (who clears every gate) acts, and a user without a grant on the workspace gets the
    missing-row 404."""
    status = {"steer": SessionStatus.WAITING, "restart": SessionStatus.ENDED, "cancel": SessionStatus.WAITING, "resume": SessionStatus.PAUSED}[action]
    await _seed(fake_storage_provider, "ses-sys", by=PrincipalRef.system(), status=status, ended_reason="completed" if action == "restart" else None)
    carol_id = await _user_id(app, "carol")
    await _workspace(fake_storage_provider, app, "admin", carol_id)
    payload = {"steer": {"instruction": "hello"}, "restart": {"input": "hello"}}.get(action, {})

    refused = await carol.post(f"/v1/workspaces/ws-r/sessions/ses-sys/{action}", json=payload)
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "Session 'ses-sys' does not exist"

    r = await client.post(f"/v1/workspaces/ws-r/sessions/ses-sys/{action}", json=payload)
    assert r.status_code == 200, r.text


# ---- the human-input paths (ticket 01a16a35, from the review of #853) -------------------------------------------------------------------
#
# A person's answer to a session's ask_user and a person's resume of it put that person's input into a run, so they follow the same gate as
# the steer: full on the session's workspace (lead ruling 1). The gate is also a floor on every decision of an approval gate, the respond
# route and the cancel of the gate's yield (a cancel is a rejection), in addition to the gate's approver spec: the gates here have no spec,
# which admits anyone (lead ruling 2). Skipping any other yield puts input into the run too (the tool's result carries the person's
# free-text reason, and the turn goes on), so every cancel of a yield follows the gate: a Skip of the question would otherwise answer it
# past the gate. Every row goes over HTTP through the auth middleware; a refused row leaves the session row, the bus, the event log and the
# approval records as they were.

HUMAN_TC = "tc-h"
#: action -> (the route it posts to, its body, the answer of an accepted request).
HUMAN: dict[str, tuple[str, dict, int]] = {
    "answer the question": ("/v1/sessions/{sid}/ask_user/respond", {"tool_call_id": HUMAN_TC, "response": "go ahead"}, 202),
    "resume": ("/v1/workspaces/ws-r/sessions/{sid}/resume", {}, 200),
    "approve the call": ("/v1/sessions/{sid}/tool_approval/respond", {"tool_call_id": HUMAN_TC, "decision": "approved"}, 202),
    "cancel the approval": ("/v1/sessions/{sid}/yields/" + HUMAN_TC + "/cancel", {"reason": "not now"}, 202),
    "skip the question": ("/v1/sessions/{sid}/yields/" + HUMAN_TC + "/cancel", {"reason": "call_tool system__create_llm_provider ..."}, 202),
    "skip the sleep": ("/v1/sessions/{sid}/yields/" + HUMAN_TC + "/cancel", {"reason": "call_tool system__create_llm_provider ..."}, 202),
}
#: An approval gate whose policy names no approvers: anyone may decide it, as far as the spec goes.
_ANYONE_GATE = {"original_call": {"id": HUMAN_TC, "name": "delete_workspace", "arguments": {"id": "ws-x"}}}


def _parked_row(sid: str, *, by: PrincipalRef | None, tool: str, metadata: dict) -> WorkspaceSession:
    """``sid`` parked on one yield of ``tool`` under :data:`HUMAN_TC`, shaped as the agent loop writes it."""
    key = {"ask_user": "ask_user", "_approval": "tool_approval", "sleep": "timer"}[tool] + f":{sid}:{HUMAN_TC}"
    return WorkspaceSession(
        id=sid, workspace_id="ws-r", binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.RUNNING, turn_no=1, created_at=NOW,
        initiated_by=by, parked_status="parked", parked_at=NOW, parked_event_key=key,
        parked_state={"tool_call_id": HUMAN_TC, "yielded": {"tool_name": tool, "event_key": key, "resume_metadata": metadata}},
    )


async def _seed_human(sp, action: str, sid: str, *, by: PrincipalRef | None) -> WorkspaceSession:
    if action == "resume":
        row = WorkspaceSession(
            id=sid, workspace_id="ws-r", binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.PAUSED, turn_no=1, created_at=NOW,
            initiated_by=by,
        )
    elif action in ("answer the question", "skip the question"):
        row = _parked_row(sid, by=by, tool="ask_user", metadata={"prompt": "May I delete the workspace?"})
    elif action == "skip the sleep":
        row = _parked_row(sid, by=by, tool="sleep", metadata={"requested_seconds": 600.0})
    else:
        row = _parked_row(sid, by=by, tool="_approval", metadata=_ANYONE_GATE)
    await sp.get_storage(WorkspaceSession).create(row)
    return row


class _Spy:
    """Records every publish on the app's event bus."""

    def __init__(self, bus) -> None:
        self.keys: list[str] = []
        real = bus.publish

        async def publish(key, payload):
            self.keys.append(key)
            return await real(key, payload)

        bus.publish = publish


async def _token_client(app, owner_id: str) -> httpx.AsyncClient:
    """A client that authenticates only with a bearer API token of ``owner_id`` (no cookie)."""
    from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
    from primer.model.api_token import ApiToken

    plaintext = mint_plaintext()
    await app.state.storage_provider.get_storage(ApiToken).create(ApiToken(
        id=f"at-{owner_id}", user_id=owner_id, name="automation", token_hash=hash_token(plaintext), prefix=extract_prefix(plaintext), scopes=[],
        created_at=NOW,
    ))
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {plaintext}"})


#: who -> (the workspace access the test seeds, who asks, whether it is admitted). The row's ``initiated_by`` decides nothing any more (the
#: rank rule is gone): the session's access is its workspace's. ``carol`` holds no level on an ``admin`` workspace; a token asks as its
#: owner.
WHO: dict[str, tuple[str, str, bool]] = {
    "a user on an admin's workspace": ("admin", "carol", False),
    "an admin on a user's workspace": ("carol", "admin", True),
    "a user on their own workspace": ("carol", "carol", True),
    "a demoted admin on their own workspace": ("carol then admin", "carol", True),
    "a reader on a workspace the full act needs": ("carol read", "carol", False),
    "a user's token on an admin's workspace": ("admin", "carol's token", False),
    "an admin's token on a user's workspace": ("carol", "admin's token", True),
}


@pytest.mark.parametrize("who", list(WHO))
@pytest.mark.parametrize("action", list(HUMAN))
async def test_a_persons_input_reaches_a_session_only_with_the_workspace_access(world, app, client, carol, fake_storage_provider, action, who):
    """Human input into a parked session (an answer, a resume, an approval decision, a cancel of a yield) is a full act on the session's
    workspace: full admits, a lower level is refused with the level named and a stranger with the missing-row 404."""
    access_spec, asker, accepted = WHO[who]
    carol_id = await _user_id(app, "carol")
    creator = {
        "admin": ADMIN,
        "carol": PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
        "carol then admin": PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local"),
    }.get(access_spec, USER)
    sid = "ses-human"
    await _seed_human(fake_storage_provider, action, sid, by=creator)
    await _workspace(fake_storage_provider, app, access_spec, carol_id)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    before = (await sessions.get(sid)).model_dump(mode="json")
    events_before = await fake_storage_provider.get_event_store().read_after(0)
    spy = _Spy(app.state.event_bus)
    route, body, ok = HUMAN[action]

    if asker.endswith("token"):
        async with await _token_client(app, carol_id if asker.startswith("carol") else await _user_id(app, "testuser")) as tok:
            r = await tok.post(route.format(sid=sid), json=body)
    else:
        r = await {"carol": carol, "admin": client}[asker].post(route.format(sid=sid), json=body)

    if not accepted:
        if access_spec == "carol read":
            assert r.status_code == 403, f"{who} could {action}: {r.text}"
            assert r.json()["detail"] == f"Session {sid!r} needs full access"
            assert r.json()["extensions"]["code"] == "access_denied"
        else:
            assert r.status_code == 404, f"{who} could {action}: {r.text}"
            assert r.json()["detail"] == f"Session {sid!r} does not exist"
        assert (await sessions.get(sid)).model_dump(mode="json") == before, f"a refused {action} wrote to the session"
        assert spy.keys == [], f"a refused {action} reached the event bus"
        assert await fake_storage_provider.get_event_store().read_after(0) == events_before, f"a refused {action} was recorded"
        assert await _approval_records(fake_storage_provider) == [], f"a refused {action} wrote an approval record"
        return
    assert r.status_code == ok, f"{who} could not {action}: {r.text}"
    row = await sessions.get(sid)
    if action == "resume":
        assert row.status == SessionStatus.RUNNING
    elif action in ("cancel the approval", "skip the question", "skip the sleep"):
        assert spy.keys == [row.parked_event_key], "the cancel was not published to the yield"
    else:
        assert row.parked_status == "resumable", f"{action} did not wake the session"
    if action == "approve the call":
        assert [rec.decision for rec in await _approval_records(fake_storage_provider)] == ["approved"]


async def _approval_records(sp) -> list:
    from primer.model.storage import OffsetPage
    from primer.model.tool_approval import ToolApprovalRecord

    return list((await sp.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=10))).items)


@pytest.mark.parametrize("action", ["approve the call", "cancel the approval"])
async def test_the_full_floor_and_the_approver_spec_agree(world, app, carol, fake_storage_provider, action):
    """Full on the workspace is the floor, and the gate's approver spec decides on top of it: a gate routed to carol by name is hers to
    decide once she holds full."""
    carol_id = await _user_id(app, "carol")
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        _parked_row("ses-named", by=ADMIN, tool="_approval", metadata={**_ANYONE_GATE, "approvers": {"kind": "users", "users": ["carol"]}}),
    )
    await _workspace(fake_storage_provider, app, "carol", carol_id)
    spy = _Spy(app.state.event_bus)
    route, body, _ = HUMAN[action]

    r = await carol.post(route.format(sid="ses-named"), json=body)

    assert r.status_code == 202, r.text
    if action == "approve the call":
        assert [rec.decision for rec in await _approval_records(fake_storage_provider)] == ["approved"]
    else:
        assert spy.keys == [(await fake_storage_provider.get_storage(WorkspaceSession).get("ses-named")).parked_event_key]


@pytest.mark.parametrize("action", ["approve the call", "cancel the approval"])
async def test_the_approver_spec_still_refuses_a_user_the_access_admits(world, app, carol, fake_storage_provider, action):
    """And the spec still runs: carol holds full on the workspace, but the gate is routed to alice."""
    carol_id = await _user_id(app, "carol")
    carol_ref = PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local")
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        _parked_row("ses-alice", by=carol_ref, tool="_approval", metadata={**_ANYONE_GATE, "approvers": {"kind": "users", "users": ["alice"]}}),
    )
    await _workspace(fake_storage_provider, app, "carol", carol_id)
    route, body, _ = HUMAN[action]

    r = await carol.post(route.format(sid="ses-alice"), json=body)

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "approver_mismatch"
