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


# ---- the human-input paths (ticket 01a16a35, from the review of #853) -------------------------------------------------------------------
#
# A person's answer to a session's ask_user and a person's resume of it put that person's input into a run at the session's creator's rank, so
# they follow the same rule as the steer: the caller ranks at least as high as the creator, or the session is the caller's own (lead ruling 1). The
# rule is also a floor on every decision of an approval gate, the respond route and the cancel of the gate's yield (a cancel is a rejection), in
# addition to the gate's approver spec: the gates here have no spec, which admits anyone (lead ruling 2). Skipping any other yield puts input into
# the run too (the tool's result carries the person's free-text reason, and the turn goes on), so every cancel of a yield follows the rule: a Skip of
# the question would otherwise answer it past the rule. Every row goes over HTTP through the auth middleware; a refused row leaves the session row,
# the bus, the event log and the approval records as they were.

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


#: who -> (who started the session, who asks, whether it is accepted). ``carol`` ranks user; ``carol then admin`` is carol recorded while she was
#: an admin (a demoted admin's own session: refused by rank, accepted as her own); the token rows ask with an API token of carol or of the admin,
#: which ranks as its owner does.
WHO: dict[str, tuple[str, str, bool]] = {
    "a user on an admin's session": ("admin", "carol", False),
    "an admin on a user's session": ("carol", "admin", True),
    "a user on their own session": ("carol", "carol", True),
    "a demoted admin on their own session": ("carol then admin", "carol", True),
    "a user's token on an admin's session": ("admin", "carol's token", False),
    "an admin's token on a user's session": ("carol", "admin's token", True),
}


@pytest.mark.parametrize("who", list(WHO))
@pytest.mark.parametrize("action", list(HUMAN))
async def test_a_persons_input_reaches_a_session_only_at_or_below_their_rank(world, app, client, carol, fake_storage_provider, action, who):
    started_by, asker, accepted = WHO[who]
    carol_id = await _user_id(app, "carol")
    creator = {
        "admin": ADMIN,
        "carol": PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
        "carol then admin": PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local"),
    }[started_by]
    sid = "ses-human"
    await _seed_human(fake_storage_provider, action, sid, by=creator)
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
        assert r.status_code == 403, f"{who} could {action}: {r.text}"
        assert (r.json()["detail"], r.json()["extensions"]["error"]) == (OUTRANKED_MESSAGE, "forbidden_rank")
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
async def test_the_rank_floor_holds_even_when_the_approver_spec_names_the_user(world, app, carol, fake_storage_provider, action):
    """The floor is in addition to the spec: a gate routed to carol by name is still not hers to decide on a session that runs above her rank."""
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        _parked_row("ses-named", by=ADMIN, tool="_approval", metadata={**_ANYONE_GATE, "approvers": {"kind": "users", "users": ["carol"]}}),
    )
    spy = _Spy(app.state.event_bus)
    route, body, _ = HUMAN[action]

    r = await carol.post(route.format(sid="ses-named"), json=body)

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_rank"
    assert spy.keys == [] and await _approval_records(fake_storage_provider) == []


@pytest.mark.parametrize("action", ["approve the call", "cancel the approval"])
async def test_the_approver_spec_still_refuses_a_user_the_rank_admits(world, app, carol, fake_storage_provider, action):
    """And the spec still runs: carol ranks with the session's creator, but the gate is routed to alice."""
    carol_ref = PrincipalRef(type="user", id=await _user_id(app, "carol"), display="carol", role="user", source="local")
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        _parked_row("ses-alice", by=carol_ref, tool="_approval", metadata={**_ANYONE_GATE, "approvers": {"kind": "users", "users": ["alice"]}}),
    )
    route, body, _ = HUMAN[action]

    r = await carol.post(route.format(sid="ses-alice"), json=body)

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "approver_mismatch"
