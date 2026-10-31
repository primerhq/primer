"""Every run carries the composite identity ``{user, actor}`` (access-control P4 Task 1, spec 6.3, plan Decision 8).

The user is the run's person (the turn person of a steered turn, the token's owner for an API-token run); the actor is the innermost acting
entity: the session's agent, a graph agent node's agent, a sub-agent, a ToolCall node's graph or a child graph. The builders set it on the
manager, the manager puts it on every ``ToolContext`` and hands it to the approval gate. These drive the worker's real builders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import primer.agent.approval as approval_mod
from primer.access.person import identity_doc
from primer.agent.approval import ApprovalResolver
from primer.agent.invoke import build_subagent_toolmanager
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.api_token import ApiToken
from primer.model.chat import ToolCallPart
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge, _ToolCallNode
from primer.model.principal import ActorRef, PrincipalRef
from primer.model.tool_approval import PolicyApprovalConfig, ToolApprovalPolicy
from primer.model.user import User
from primer.model.yield_ import Yielded
from primer.worker.frames import AgentFrame, apply_leaf
from tests.agent.test_approval_identity import _CaptureRego
from tests.agent.test_inner_dispatch import USER, _Recorder
from tests.agent.test_subagent_identity_fallback import _context, _Registry, _services
from tests.conftest import _FakeStorageProvider
from tests.worker import test_executor_builders_instructions as builders
from tests.worker.test_graph_binding_profile import _Storage as _GraphStorage
from tests.worker.test_run_principal_current_role import _pool, _session, _user, _Workspace

NOW = datetime.now(UTC)
# Admins, so the role floor of the pool's admin-only tool lets the call reach the gate.
CREATOR = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")
PEER = PrincipalRef(type="user", id="u-bob", display="bob", role="admin", source="local")


@pytest.fixture
def captured(monkeypatch) -> _CaptureRego:
    cap = _CaptureRego()
    monkeypatch.setattr(approval_mod, "_rego_evaluator", cap)
    return cap


async def _gated_pool(monkeypatch, sp: _FakeStorageProvider):  # noqa: ANN202 - a test pool
    """The worker pool of ``test_run_principal_current_role`` with a storage-backed approval resolver and a policy on its ``adm__nuke`` tool."""
    pool = await _pool(monkeypatch, sp, [])
    pool._approval_resolver = ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy))
    await sp.get_storage(ToolApprovalPolicy).create(ToolApprovalPolicy(
        id="p-nuke", toolset_id="adm", tool_name="nuke", approval=PolicyApprovalConfig(policy="package primer.tool_approval\n"),
    ))
    return pool


async def test_steered_turn_identity_is_the_peer(monkeypatch, captured):
    """A turn a peer's steer started runs as the peer: the gate's user is the peer, the actor the session's agent."""
    sp = _FakeStorageProvider()
    pool = await _gated_pool(monkeypatch, sp)
    await _user(sp, "u-ada", "admin")
    await _user(sp, "u-bob", "admin")
    driver = await pool._build_agent_executor(_session(CREATOR, turn_person=PEER), _Workspace("ses-1"))

    await driver._executor._tool_manager.execute(ToolCallPart(id="c1", name="adm__nuke", arguments={}))

    (doc,) = captured.inputs
    assert doc["identity"] == {"user": {"id": "u-bob", "name": "bob", "role": "admin"}, "actor": {"kind": "agent", "id": "ag-1"}}


async def test_api_token_run_names_the_owner(monkeypatch, captured):
    """An API-token run's name is its owner's username, read from the User row when the gate fires (the ref only names the token)."""
    sp = _FakeStorageProvider()
    pool = await _gated_pool(monkeypatch, sp)
    pool._provider_registry.storage_provider = sp
    await sp.get_storage(User).create(User(id="u-ada", username="ada-lovelace", created_at=NOW, role="admin"))
    await sp.get_storage(ApiToken).create(ApiToken(
        id="tok-1", user_id="u-ada", name="ci", token_hash="0" * 64, prefix="prmr_abc", created_at=NOW,
    ))
    token_ref = PrincipalRef(type="api_token", id="tok-1", display="ci", role="admin", source="internal", user_id="u-ada")
    driver = await pool._build_agent_executor(_session(token_ref), _Workspace("ses-1"))

    await driver._executor._tool_manager.execute(ToolCallPart(id="c1", name="adm__nuke", arguments={}))

    (doc,) = captured.inputs
    assert doc["identity"]["user"] == {"id": "u-ada", "name": "ada-lovelace", "role": "admin"}


async def test_subagent_actor_is_the_subagent(captured):
    """A sub-agent acts as itself (no more ``"subagent"`` stand-in) for its parent run's person: on its first run, on a chat (no session), and
    when an approved call of its is re-run on resume (``apply_leaf`` through the worker's services)."""
    rec = _Recorder({"user_x": "user"})
    sub = ActorRef(kind="agent", id="agent-sub")

    manager = await build_subagent_toolmanager(_context(USER), agent_id="agent-sub", provider_registry=_Registry(rec))
    ctx = manager.context_for("c1")
    assert (ctx.agent_id, ctx.actor) == ("agent-sub", sub)
    assert identity_doc(ctx.initiated_by, ctx.actor) == {"user": {"id": "u-1", "name": "user", "role": "user"}, "actor": {"kind": "agent", "id": "agent-sub"}}

    chat = _context(USER)
    chat.session_id, chat.chat_id = None, "chat-1"
    chat_manager = await build_subagent_toolmanager(chat, agent_id="agent-sub", provider_registry=_Registry(rec))
    assert chat_manager.context_for("c1").actor == sub

    leaf = Yielded(tool_name="_approval", event_key="tool_approval:s-1:c9", resume_metadata={
        "original_call": {"id": "c9", "name": "rec__user_x", "arguments": {}},
    })
    frame = AgentFrame("agent-sub", [], "c-invoke", 1, _context(USER))
    result = await apply_leaf(frame, leaf, {"decision": "approved"}, _services(rec, USER))
    assert result.error is False, result.output
    (_name, _args, _principal, rerun_ctx) = rec.calls[-1]
    assert rerun_ctx.actor == sub


def _toolcall_graph(graph_id: str) -> Graph:
    return Graph(
        access=Access(public=True), id=graph_id, description="begin -> tool -> end",
        nodes=[_BeginNode(id="begin"), _ToolCallNode(id="tool", tool_id="workspace__read_file", arguments={}), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="tool"), _StaticEdge(from_node="tool", to_node="end")],
    )


async def test_graph_toolcall_actor_is_the_graph(monkeypatch, tmp_path: Path):
    """In a graph run built by the worker: a ToolCall node acts as its graph, a child graph's ToolCall node as the child graph, and an agent node
    as its agent (the holder session names no agent, so none of these is the session's default)."""
    pool = builders._pool(monkeypatch)
    monkeypatch.setattr(pool, "_storage", _GraphStorage(builders._agent(), {}))
    ws = builders._Workspace("s-g", state_repo=await builders._state_repo(tmp_path))
    ws._session.agent_id = None
    driver = await pool._build_executor(builders._graph_row("s-g"), ws)
    executor = driver._executor
    node = _toolcall_graph("unused").nodes[1]

    manager = await executor._manager_for(node)
    assert manager.context_for("c1").actor == ActorRef(kind="graph", id="g-1")

    child = await executor._build_sub_executor(SimpleNamespace(id="sub"), _toolcall_graph("g-child"))
    assert (await child._manager_for(node)).context_for("c1").actor == ActorRef(kind="graph", id="g-child")

    node_agent = Agent(access=Access(public=True), id="ag-node", description="a node agent", model=AgentModel(profile_id="p--m"))
    node_manager = await executor._tool_manager_resolver(node_agent)
    assert node_manager.context_for("c1").actor == ActorRef(kind="agent", id="ag-node")
