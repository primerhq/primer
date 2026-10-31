"""Access gates on the session REST surface (access-control spec 9; plan P2 Task 3).

A session inherits the access of the workspace it lives in, and the rank rule
of the routes that act on a session (the caller ranks at least as high as its
creator, or it is their own) is replaced by that access check: a stranger to
the workspace answers the session's missing-row 404 on every route, a holder
of a level below the route's need answers 403 with the level named, and a
holder of the level acts. There is no own-session bypass any more: a creator
whose grant is revoked loses access. Admins and the system principal clear
every gate; the approver spec of a tool-approval gate still runs on top of
the access gate.

Runs against the real routes and the real auth middleware (the house pattern
of tests/api/test_session_steer_restart_rank.py): ``client`` is the first
registered user (an admin); the other clients are user-role accounts the
admin creates. Workspaces and sessions are seeded the way the routes read
them: direct storage writes; the fake registry behind the app serves the
backend side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.model.access import Access
from primer.model.graph import Graph
from primer.model.principal import PrincipalRef
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionInfo,
    SessionStatus,
    WorkspaceSession,
)
from pydantic import SecretStr
from tests.session._peer_fakes import FakeRegistry, FakeSlot, FakeWorkspace

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
WS = "ws-1"
SID = "ses-r"
HUMAN_TC = "tc-h"


class _Slot(FakeSlot):
    """The on-disk slot, with the set_name the rename route mirrors onto."""

    async def set_name(self, name: str) -> SessionInfo:
        return SessionInfo(
            session_id=SID, agent_id="operator", workspace_id=WS, status=SessionStatus.WAITING,
            started_at=NOW, last_activity_at=NOW, name=name,
        )


class _Workspace(FakeWorkspace):
    state_path = ".state"

    def slot(self, session_id: str) -> _Slot:
        return self.slots.setdefault(session_id, _Slot())  # type: ignore[return-value]

    async def read_file(self, path: str) -> bytes:
        from primer.model.except_ import NotFoundError

        raise NotFoundError(f"file {path!r} does not exist")


class _Registry(FakeRegistry):
    def workspace(self, workspace_id: str) -> _Workspace:
        return self.workspaces.setdefault(workspace_id, _Workspace())  # type: ignore[return-value]


@pytest_asyncio.fixture
async def world(app):
    """One fake workspace per id behind the app's registry (the gates read the rows from storage)."""
    registry = _Registry()
    app.state.workspace_registry.get_workspace = registry.get_workspace  # type: ignore[method-assign]
    return registry


@pytest_asyncio.fixture
async def cast(app, client, fake_storage_provider):
    """carol owns the workspace, dave holds full on it, erin read, mallory nothing."""
    created: list[httpx.AsyncClient] = []

    async def mk(username: str) -> httpx.AsyncClient:
        pw = username + "pw123"
        r = await client.post("/v1/admin/users", json={"username": username, "password": pw})
        assert r.status_code == 201, r.text
        c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        login = await c.post("/v1/auth/login", json={"username": username, "password": pw})
        assert login.status_code == 200, login.text
        created.append(c)
        return c

    users = fake_storage_provider.get_storage(User)

    async def uid(username: str) -> str:
        page = await users.find(
            Predicate(left=FieldRef(name="username"), op=Op.EQ, right=Value(value=username)),
            OffsetPage(offset=0, length=1),
        )
        return page.items[0].id

    c = SimpleNamespace(
        carol=await mk("carol"), dave=await mk("dave"), erin=await mk("erin"), mallory=await mk("mallory"),
    )
    try:
        c.carol_id = await uid("carol")
        c.dave_id = await uid("dave")
        c.erin_id = await uid("erin")
        c.mallory_id = await uid("mallory")
        yield c
    finally:
        for client_ in created:
            await client_.aclose()


# ===========================================================================
# Seeding: the workspace row the gates read, the session rows, the parked rows
# ===========================================================================


def _workspace(owners: list[str], grants: dict[str, str]) -> Workspace:
    return Workspace(
        id=WS, template_id="tpl-1", provider_id="local-1", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub-token")),
        access=Access(owners=owners, grants=grants),
    )


async def _seed_world(sp, c: SimpleNamespace, *, by: PrincipalRef | None = None,
                       status: SessionStatus = SessionStatus.WAITING, sid: str = SID,
                       ended_reason: str | None = None, binding: Any = None) -> None:
    """The workspace carol owns with dave at full and erin at read, plus the session row."""
    await sp.get_storage(Workspace).create(
        _workspace(owners=[c.carol_id], grants={c.dave_id: "full", c.erin_id: "read"}),
    )
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id=WS,
        binding=binding or AgentSessionBinding(agent_id="operator"),
        status=status, ended_reason=ended_reason,
        ended_at=NOW if status == SessionStatus.ENDED else None,
        turn_no=1, created_at=NOW, initiated_by=by,
    ))


def _parked_row(sid: str, *, by: PrincipalRef | None, tool: str, metadata: dict) -> WorkspaceSession:
    """``sid`` parked on one yield of ``tool`` under :data:`HUMAN_TC`, shaped as the agent loop writes it."""
    key = {"ask_user": "ask_user", "_approval": "tool_approval", "sleep": "timer"}[tool] + f":{sid}:{HUMAN_TC}"
    return WorkspaceSession(
        id=sid, workspace_id=WS, binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.RUNNING,
        turn_no=1, created_at=NOW, initiated_by=by, parked_status="parked", parked_at=NOW, parked_event_key=key,
        parked_state={"tool_call_id": HUMAN_TC, "yielded": {"tool_name": tool, "event_key": key, "resume_metadata": metadata}},
    )


def _dave_ref(c: SimpleNamespace) -> PrincipalRef:
    return PrincipalRef(type="user", id=c.dave_id, display="dave", role="user", source="local")


# ===========================================================================
# Read routes: 404 for the stranger, the read served to the reader
# ===========================================================================

READS = {
    "detail": ("get", f"/v1/sessions/{SID}", None),
    "turn_log": ("get", f"/v1/sessions/{SID}/turn_log", None),
    "messages": ("get", f"/v1/sessions/{SID}/messages", None),
    "timeline": ("get", f"/v1/sessions/{SID}/turns/0/timeline", None),
    "external pending": ("get", f"/v1/sessions/{SID}/external_tools/pending", None),
    "canvas": ("get", f"/v1/workspaces/{WS}/sessions/{SID}/canvas", None),
}


@pytest.mark.parametrize("which", list(READS))
async def test_read_routes_404_for_a_stranger_and_serve_the_reader(world, app, client, cast, which):
    """The stranger to the workspace answers the session's missing-row text; the reader gets the read."""
    sp = app.state.storage_provider
    await _seed_world(sp, cast, by=_dave_ref(cast))
    method, path, body = READS[which]

    refused = await getattr(cast.mallory, method)(path, **({"json": body} if body is not None else {}))
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == f"Session {SID!r} does not exist"

    served = await getattr(cast.erin, method)(path, **({"json": body} if body is not None else {}))
    if which == "timeline":
        # An empty log has no turn 0: the reader reaches the turn rule and its
        # own 404, which is not the session's missing-row text.
        assert served.status_code == 404 and served.json()["detail"] != f"Session {SID!r} does not exist", served.text
    else:
        assert served.status_code == 200, served.text


async def test_parked_prompts_follow_the_read_gate(world, app, client, cast):
    """A parked ask_user prompt and a parked approval gate are readable to the reader, missing-row to the stranger."""
    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.erin_id: "read"}))
    by = _dave_ref(cast)
    await sp.get_storage(WorkspaceSession).create(_parked_row(SID, by=by, tool="ask_user", metadata={"prompt": "May I?"}))

    refused = await cast.mallory.get(f"/v1/sessions/{SID}/ask_user/pending")
    assert refused.status_code == 404 and refused.json()["detail"] == f"Session {SID!r} does not exist"
    served = await cast.erin.get(f"/v1/sessions/{SID}/ask_user/pending")
    assert served.status_code == 200, served.text

    sp2 = sp
    await sp2.get_storage(WorkspaceSession).create(_parked_row(
        "ses-appr", by=by, tool="_approval",
        metadata={"original_call": {"id": HUMAN_TC, "name": "delete_workspace", "arguments": {"id": "ws-x"}}},
    ))
    refused = await cast.mallory.get("/v1/sessions/ses-appr/tool_approval/pending")
    assert refused.status_code == 404 and refused.json()["detail"] == "Session 'ses-appr' does not exist"
    served = await cast.erin.get("/v1/sessions/ses-appr/tool_approval/pending")
    assert served.status_code == 200, served.text


# ===========================================================================
# Graph-run reads: run_id is a session id -> read on that session's workspace
# ===========================================================================


async def test_graph_run_reads_follow_the_session_workspace(world, app, client, cast):
    """The graph is public (every user passes its read gate); the run's workspace is what separates the stranger from the reader."""
    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.erin_id: "read"}))
    await sp.get_storage(Graph).create(Graph(id="g-1", description="g", access=Access(public=True)))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ses-g", workspace_id=WS, binding=GraphSessionBinding(graph_id="g-1"),
        status=SessionStatus.RUNNING, turn_no=1, created_at=NOW, initiated_by=_dave_ref(cast),
    ))

    for tail in ("turn_log", "node_states"):
        refused = await cast.mallory.get(f"/v1/graphs/g-1/runs/ses-g/{tail}")
        assert refused.status_code == 404, (tail, refused.text)
        assert refused.json()["detail"] == "Graph run 'ses-g' does not exist", refused.text
        served = await cast.erin.get(f"/v1/graphs/g-1/runs/ses-g/{tail}")
        assert served.status_code == 200, (tail, served.text)


# ===========================================================================
# Full routes: 403 for the reader, missing-row for the stranger, the act for the full holder
# ===========================================================================

FULLS = {
    "steer": ("post", f"/v1/workspaces/{WS}/sessions/{SID}/steer", {"instruction": "hello"}, SessionStatus.WAITING),
    "restart": ("post", f"/v1/workspaces/{WS}/sessions/{SID}/restart", {"input": "hello"}, SessionStatus.ENDED),
    "cancel": ("post", f"/v1/workspaces/{WS}/sessions/{SID}/cancel", {}, SessionStatus.WAITING),
    "resume": ("post", f"/v1/workspaces/{WS}/sessions/{SID}/resume", {}, SessionStatus.PAUSED),
    "create": ("post", f"/v1/workspaces/{WS}/sessions", {"binding": {"kind": "agent", "agent_id": "operator"}}, SessionStatus.WAITING),
}


@pytest.mark.parametrize("which", list(FULLS))
async def test_full_routes_refuse_the_reader_and_hide_the_row_from_a_stranger(world, app, client, cast, which):
    """A reader of the workspace is refused with the level named; a stranger answers the missing-row text (a 404, never a 403)."""
    sp = app.state.storage_provider
    ended_reason = "cancelled" if which == "restart" else None
    await _seed_world(sp, cast, by=_dave_ref(cast), status=FULLS[which][3], ended_reason=ended_reason)
    method, path, body, _ = FULLS[which]
    what = "Session" if which != "create" else "Workspace"
    ref = SID if which != "create" else WS

    refused = await getattr(cast.erin, method)(path, json=body)
    assert refused.status_code == 403, (which, refused.text)
    assert refused.json()["detail"] == f"{what} {ref!r} needs full access", refused.text

    hidden = await getattr(cast.mallory, method)(path, json=body)
    assert hidden.status_code == 404, (which, hidden.text)
    assert hidden.json()["detail"] == f"{what} {ref!r} does not exist", hidden.text


@pytest.mark.parametrize("which", ["steer", "restart", "cancel", "resume"])
async def test_a_holder_of_full_acts_on_the_full_routes(world, app, client, cast, which):
    """The full holder drives the session: the row lands the act."""
    sp = app.state.storage_provider
    ended_reason = "cancelled" if which == "restart" else None
    await _seed_world(sp, cast, by=_dave_ref(cast), status=FULLS[which][3], ended_reason=ended_reason)
    method, path, body, _ = FULLS[which]
    sessions = sp.get_storage(WorkspaceSession)

    r = await getattr(cast.dave, method)(path, json=body)
    assert r.status_code == 200, (which, r.text)
    row = await sessions.get(SID)
    assert row.status in (SessionStatus.RUNNING, SessionStatus.ENDED)
    if which == "cancel":
        assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled")
    else:
        assert row.status == SessionStatus.RUNNING


async def test_delete_session_follows_the_full_gate(world, app, client, cast):
    """Deleting a session is a full act in its workspace (the workspace itself still needs owner to delete)."""
    sp = app.state.storage_provider
    await _seed_world(sp, cast, by=_dave_ref(cast))
    sessions = sp.get_storage(WorkspaceSession)

    refused = await cast.erin.delete(f"/v1/workspaces/{WS}/sessions/{SID}")
    assert refused.status_code == 403 and refused.json()["detail"] == f"Session {SID!r} needs full access"
    hidden = await cast.mallory.delete(f"/v1/workspaces/{WS}/sessions/{SID}")
    assert hidden.status_code == 404 and hidden.json()["detail"] == f"Session {SID!r} does not exist"
    assert await sessions.get(SID) is not None, "a refused delete removed the row"

    r = await cast.dave.delete(f"/v1/workspaces/{WS}/sessions/{SID}")
    assert r.status_code == 204, r.text
    assert await sessions.get(SID) is None


# ===========================================================================
# The session's own operations: the owner acts, the reader is refused
# ===========================================================================


async def test_session_ops_refuse_the_reader(world, app, client, cast):
    """A rename and a canvas batch or clear put the owner's input into the session: full, not read."""
    sp = app.state.storage_provider
    await _seed_world(sp, cast, by=_dave_ref(cast))

    refused = await cast.erin.patch(f"/v1/workspaces/{WS}/sessions/{SID}", json={"name": "the label"})
    assert refused.status_code == 403 and refused.json()["detail"] == f"Session {SID!r} needs full access"
    refused = await cast.erin.post(f"/v1/workspaces/{WS}/sessions/{SID}/canvas/ops", json={"source": 'note = Text("hi")\nroot += [note]'})
    assert refused.status_code == 403 and refused.json()["detail"] == f"Session {SID!r} needs full access"
    refused = await cast.erin.delete(f"/v1/workspaces/{WS}/sessions/{SID}/canvas")
    assert refused.status_code == 403 and refused.json()["detail"] == f"Session {SID!r} needs full access"

    ok = await cast.carol.patch(f"/v1/workspaces/{WS}/sessions/{SID}", json={"name": "the label"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "the label"
    ok = await cast.carol.post(f"/v1/workspaces/{WS}/sessions/{SID}/canvas/ops", json={"source": 'note = Text("hi")\nroot += [note]'})
    assert ok.status_code == 200, ok.text
    ok = await cast.carol.delete(f"/v1/workspaces/{WS}/sessions/{SID}/canvas")
    assert ok.status_code in (200, 204), ok.text


# ===========================================================================
# A person's input into a parked session: the full gate of the steer
# ===========================================================================


async def test_human_input_into_a_parked_session_follows_the_full_gate(world, app, client, cast):
    """An answer, an approval decision and a yield cancel put the person's input into the run: full on the workspace."""
    sp = app.state.storage_provider
    by = _dave_ref(cast)
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.dave_id: "full", cast.erin_id: "read"}))
    await sp.get_storage(WorkspaceSession).create(_parked_row(SID, by=by, tool="ask_user", metadata={"prompt": "May I?"}))

    refused = await cast.erin.post(f"/v1/sessions/{SID}/ask_user/respond", json={"tool_call_id": HUMAN_TC, "response": "go"})
    assert refused.status_code == 403 and refused.json()["detail"] == f"Session {SID!r} needs full access"
    hidden = await cast.mallory.post(f"/v1/sessions/{SID}/ask_user/respond", json={"tool_call_id": HUMAN_TC, "response": "go"})
    assert hidden.status_code == 404 and hidden.json()["detail"] == f"Session {SID!r} does not exist"

    r = await cast.dave.post(f"/v1/sessions/{SID}/ask_user/respond", json={"tool_call_id": HUMAN_TC, "response": "go"})
    assert r.status_code == 202, r.text
    assert (await sp.get_storage(WorkspaceSession).get(SID)).parked_status == "resumable"


def _carol_ref(c: SimpleNamespace) -> PrincipalRef:
    return PrincipalRef(type="user", id=c.carol_id, display="carol", role="user", source="local", user_id=c.carol_id)


async def test_a_peers_ask_user_answer_runs_the_resumed_turn_as_the_peer(world, app, client, cast):
    """Final access review F6 (lead ruling): an answer is model input, like a steer, so a full peer's answer re-attributes the resumed turn to
    the peer (``turn_person``), and the resume-time workspace check then judges them, never the owner's identity; the owner's own answer keeps it."""
    from primer.worker.executor_builders import session_run_principal

    sp = app.state.storage_provider
    carol = _carol_ref(cast)
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.dave_id: "full"}))
    await sp.get_storage(WorkspaceSession).create(
        _parked_row(SID, by=carol, tool="ask_user", metadata={"prompt": "which file?"}).model_copy(update={"turn_person": carol})
    )
    await sp.get_storage(WorkspaceSession).create(
        _parked_row("ses-own", by=carol, tool="ask_user", metadata={"prompt": "which file?"}).model_copy(update={"turn_person": carol})
    )

    r = await cast.dave.post(f"/v1/sessions/{SID}/ask_user/respond", json={"tool_call_id": HUMAN_TC, "response": "read hr-notes"})
    own = await cast.carol.post("/v1/sessions/ses-own/ask_user/respond", json={"tool_call_id": HUMAN_TC, "response": "a.txt"})

    assert r.status_code == 202 and own.status_code == 202, (r.text, own.text)
    after = await sp.get_storage(WorkspaceSession).get(SID)
    assert after.parked_status == "resumable" and session_run_principal(after).id == cast.dave_id
    assert session_run_principal(await sp.get_storage(WorkspaceSession).get("ses-own")).id == cast.carol_id


async def test_a_peers_external_tool_results_run_the_resumed_turn_as_the_peer(world, app, client, cast):
    """F6, the other door of a parked turn: ``tool_results`` on the steer route re-attribute the resumed turn to the submitter, as a steer does."""
    from primer.model.external_tool import ExternalToolCall
    from primer.worker.executor_builders import session_run_principal

    sp = app.state.storage_provider
    carol = _carol_ref(cast)
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.dave_id: "full"}))
    key = f"external_tool:{SID}:tc-1"
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=SID, workspace_id=WS, binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.RUNNING, turn_no=1,
        created_at=NOW, initiated_by=carol, turn_person=carol, parked_status="parked", parked_at=NOW, parked_event_key=key,
        parked_state={"tool_call_id": "tc-1", "yielded": {"tool_name": "_external", "event_key": key, "resume_metadata": {
            "original_call": {"id": "tc-1", "name": "lookup", "arguments": {}}, "external_call_row_id": "etool-1"}}},
    ))
    await sp.get_storage(ExternalToolCall).create(ExternalToolCall(
        id="etool-1", session_id=SID, tool_call_id="tc-1", tool_name="lookup", arguments={}, created_at=NOW,
    ))

    r = await cast.dave.post(f"/v1/workspaces/{WS}/sessions/{SID}/steer", json={"tool_results": [{"tool_call_id": "tc-1", "result": "x"}]})

    assert r.status_code == 200, r.text
    after = await sp.get_storage(WorkspaceSession).get(SID)
    assert after.parked_status == "resumable" and session_run_principal(after).id == cast.dave_id


async def test_an_approval_decision_needs_full_and_keeps_the_approver_spec(world, app, client, cast):
    """The access gate replaces the rank floor under the gate's approver spec: full lets the spec decide, the spec still refuses the wrong user."""
    sp = app.state.storage_provider
    by = _dave_ref(cast)
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.dave_id: "full", cast.erin_id: "read"}))
    await sp.get_storage(WorkspaceSession).create(_parked_row(
        SID, by=by, tool="_approval",
        metadata={"original_call": {"id": HUMAN_TC, "name": "delete_workspace", "arguments": {"id": "ws-x"}}},
    ))

    refused = await cast.erin.post(f"/v1/sessions/{SID}/tool_approval/respond", json={"tool_call_id": HUMAN_TC, "decision": "approved"})
    assert refused.status_code == 403 and refused.json()["detail"] == f"Session {SID!r} needs full access"

    # Re-grant erin to full (keeping dave) so the access gate admits her, then show the spec
    # (which names dave) still refuses the wrong user: full is the floor, the approver spec decides on top.
    await cast.carol.put(
        f"/v1/workspaces/{WS}/access",
        json={"owners": [cast.carol_id], "grants": {cast.dave_id: "full", cast.erin_id: "full"}},
    )
    await sp.get_storage(WorkspaceSession).create(_parked_row(
        "ses-named", by=by, tool="_approval",
        metadata={
            "original_call": {"id": HUMAN_TC, "name": "delete_workspace", "arguments": {"id": "ws-x"}},
            "approvers": {"kind": "users", "users": ["dave"]},
        },
    ))
    refused = await cast.erin.post("/v1/sessions/ses-named/tool_approval/respond", json={"tool_call_id": HUMAN_TC, "decision": "approved"})
    assert refused.status_code == 403 and refused.json()["extensions"]["error"] == "approver_mismatch", refused.text

    # The named approver (dave) holds full and is admitted by the spec.
    r = await cast.dave.post("/v1/sessions/ses-named/tool_approval/respond", json={"tool_call_id": HUMAN_TC, "decision": "approved"})
    assert r.status_code == 202, r.text
    assert (await sp.get_storage(WorkspaceSession).get("ses-named")).parked_status == "resumable"


# ===========================================================================
# Review Focus 1: no own-session bypass; admins keep the role floor
# ===========================================================================


async def test_creator_loses_access_with_the_grant(world, app, client, cast):
    """Dave's own session is not his to drive once carol revokes his grant: steer and read both answer the missing-row text."""
    sp = app.state.storage_provider
    await _seed_world(sp, cast, by=_dave_ref(cast))

    own = await cast.dave.post(f"/v1/workspaces/{WS}/sessions/{SID}/steer", json={"instruction": "hello"})
    assert own.status_code == 200, own.text

    revoked = await cast.carol.put(
        f"/v1/workspaces/{WS}/access",
        json={"owners": [cast.carol_id], "grants": {cast.erin_id: "read"}},
    )
    assert revoked.status_code == 200, revoked.text

    refused = await cast.dave.post(f"/v1/workspaces/{WS}/sessions/{SID}/steer", json={"instruction": "hello"})
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == f"Session {SID!r} does not exist"
    refused = await cast.dave.get(f"/v1/sessions/{SID}")
    assert refused.status_code == 404 and refused.json()["detail"] == f"Session {SID!r} does not exist"

    owner_still_acts = await cast.carol.post(f"/v1/workspaces/{WS}/sessions/{SID}/steer", json={"instruction": "hello"})
    assert owner_still_acts.status_code == 200, owner_still_acts.text


@pytest.mark.parametrize("which", ["steer", "detail"])
async def test_an_admin_still_clears_the_gates(world, app, client, cast, which):
    """The role floor holds: the admin reads and drives sessions in workspaces nobody granted to it."""
    sp = app.state.storage_provider
    await _seed_world(sp, cast, by=_dave_ref(cast))

    if which == "steer":
        r = await client.post(f"/v1/workspaces/{WS}/sessions/{SID}/steer", json={"instruction": "hello"})
    else:
        r = await client.get(f"/v1/sessions/{SID}")
    assert r.status_code == 200, r.text


# ===========================================================================
# Final access review F12: a hidden row answers byte-for-byte like a missing one on every door
# ===========================================================================


def _norm(resp: httpx.Response, *ids: str) -> tuple[int, str]:
    """Status and body with the per-request fields dropped and the probed ids replaced, so a hidden and an unknown id compare equal."""
    import json

    body = resp.json()
    body.pop("instance", None)
    if isinstance(body.get("extensions"), dict):
        body["extensions"].pop("request_id", None)
    text = json.dumps(body, sort_keys=True)
    for i in ids:
        text = text.replace(i, "<ID>")
    return resp.status_code, text


async def test_four_more_doors_answer_a_hidden_row_like_an_unknown_one(world, app, client, cast):
    from primer.model.workspace_session import PendingSessionMessage  # noqa: F401  (the dismiss reads it)

    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={}))  # carol's private ws-1
    await sp.get_storage(Graph).create(Graph(id="g-pub", description="g", access=Access(owners=[cast.carol_id], public=True)))
    await sp.get_storage(WorkspaceSession).create(_parked_row(SID, by=None, tool="_approval", metadata={}))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="run-1", workspace_id=WS, binding=GraphSessionBinding(graph_id="g-pub"), status=SessionStatus.WAITING, turn_no=1, created_at=NOW,
    ))
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.mallory_id], grants={}).model_copy(update={"id": "ws-m"}))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ses-m", workspace_id="ws-m", binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.WAITING, turn_no=1,
        created_at=NOW,
    ))
    m = cast.mallory
    approve = {"tool_call_id": HUMAN_TC, "decision": "approved"}
    pairs = {
        "tool_approval/respond": (
            await m.post(f"/v1/sessions/{SID}/tool_approval/respond", json=approve),
            await m.post("/v1/sessions/ses-zzzz/tool_approval/respond", json=approve), SID, "ses-zzzz"),
        "pending DELETE": (
            await m.delete(f"/v1/workspaces/{WS}/sessions/{SID}/pending_messages/p1"),
            await m.delete(f"/v1/workspaces/{WS}/sessions/ses-zzzz/pending_messages/p1"), SID, "ses-zzzz"),
        "session PATCH": (
            await m.patch(f"/v1/workspaces/{WS}/sessions/ses-m", json={"name": "x"}),
            await m.patch("/v1/workspaces/ws-zz/sessions/ses-m", json={"name": "x"}), WS, "ws-zz"),
    }
    for tail in ("turn_log", "node_states", "nodes/n1/turn_log"):
        pairs[f"graph run {tail}"] = (
            await m.get(f"/v1/graphs/g-pub/runs/run-1/{tail}"), await m.get(f"/v1/graphs/g-pub/runs/run-zz/{tail}"), "run-1", "run-zz")

    seen = {door: (_norm(hidden, hid, uid), _norm(unknown, hid, uid)) for door, (hidden, unknown, hid, uid) in pairs.items()}
    assert {door: pair for door, pair in seen.items() if pair[0][0] != 404 or pair[0] != pair[1]} == {}
