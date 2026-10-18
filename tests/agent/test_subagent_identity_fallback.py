"""A subagent runs as its parent run, never as the system principal (security review A-20, pivot P1).

``system__invoke_agent`` called with no run identity starts a subagent ranked as an ordinary user. ``invoke_agent`` is a ``user``-floor
tool. Over the MCP endpoint the handler gets no ``ToolContext``, so it used to pass ``identity=None`` to
:func:`primer.agent.invoke.run_subagent`, whose tool-manager builder fell back to the system principal, which clears every role floor: a
``role=user`` MCP caller could invoke an agent whose tools include admin-only system tools and have them run. The handler now passes
:meth:`PrincipalRef.unattributed` when it has no identity.

The builder (:func:`primer.agent.invoke.build_subagent_toolmanager`) kept that system fallback for a context that names no initiator, and a
RESUME rebuilt the subagent's manager from the context stored in the park. A frame written before ``AgentResumeContext.initiated_by``
existed names none, so the approved re-run of a gated tool inside it, and the rest of the resumed subagent's turn, ran as the system
principal whatever the session's own rank. A resume now builds the subagent's manager with the principal its PARENT session runs as
(``session.initiated_by``, else an unattributed user: the rule the session's own turn is built with), never one stored in the park, and a
context that names no initiator, with no principal handed in, is an unattributed user.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import primer.toolset.system as system_module
from primer.agent.invoke import build_subagent_toolmanager
from primer.api.registries import ProviderRegistry
from primer.authz import _role_allows
from primer.model.chat import ToolCallPart, ToolResultPart
from primer.model.principal import PrincipalRef
from primer.model.yield_ import Yielded
from primer.toolset.system import build_system_toolset
from primer.worker.frames import AgentFrame, AgentResumeContext, apply_leaf
from primer.worker.session_resume_coordinator import build_invocation_services
from tests._support.caller import caller
from tests.agent.test_inner_dispatch import ADMIN, USER, _Recorder

UNATTRIBUTED = PrincipalRef.unattributed()

#: The subagent's tool surface: one admin-only tool and one tool any user may run, both of the recording toolset ``rec``.
TOOLS = ["rec__admin_x", "rec__user_x"]


@pytest.fixture
def captured(monkeypatch, fake_storage_provider):
    seen: dict = {}

    async def _fake_run_subagent(**kwargs):
        seen.update(kwargs)
        return "done"

    monkeypatch.setattr(system_module, "run_subagent", _fake_run_subagent)
    registry = ProviderRegistry(
        fake_storage_provider, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )
    return build_system_toolset(storage_provider=fake_storage_provider, provider_registry=registry), seen


async def test_invoke_agent_with_no_identity_starts_a_user_ranked_subagent(captured) -> None:
    toolset, seen = captured

    result = await toolset.call(tool_name="invoke_agent", arguments={"agent_id": "ag-1", "prompt": "go"}, ctx=None)

    assert not result.is_error, result.output
    who = seen["identity"]
    assert who is not None and who.type != "system"
    assert who.role == "user"
    assert not _role_allows(who, "admin")


async def test_invoke_agent_keeps_the_calling_runs_identity(captured) -> None:
    toolset, seen = captured
    ctx = caller("admin")

    result = await toolset.call(tool_name="invoke_agent", arguments={"agent_id": "ag-1", "prompt": "go"}, ctx=ctx)

    assert not result.is_error, result.output
    assert seen["identity"] == ctx.initiated_by
    assert isinstance(seen["identity"], PrincipalRef)


# ---- the subagent's own manager ------------------------------------------------------------------------------------------------------------------------


class _Registry:
    """The provider registry: every toolset id resolves to the one recording provider."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def get_toolset(self, toolset_id: str) -> Any:
        return self.provider


def _context(initiated_by: PrincipalRef | None) -> AgentResumeContext:
    """A subagent's resume context as a park stores it. ``None`` is what a frame written before ``initiated_by`` existed carries."""
    return AgentResumeContext("s-1", "ws-1", None, None, list(TOOLS), initiated_by=initiated_by)


def _services(rec: _Recorder, parent: PrincipalRef | None):
    """The services the worker builds to resume session ``s-1`` (``build_invocation_services``), whose row's ``initiated_by`` is ``parent``."""
    pool = SimpleNamespace(_storage=None, _provider_registry=_Registry(rec), _approval_resolver=None)
    return build_invocation_services(pool, SimpleNamespace(id="s-1", initiated_by=parent), None, None, None)


async def _run(manager: Any, tool: str) -> ToolResultPart:
    return await manager.execute(ToolCallPart(id=f"call-{tool}", name=f"rec__{tool}", arguments={}))


async def test_a_context_that_names_no_initiator_is_an_unattributed_user_never_the_system() -> None:
    """The builder's own rule when no principal is handed in. A first run always has one (``run_subagent`` stamps the calling run's)."""
    rec = _Recorder({"admin_x": "admin", "user_x": "user"})
    manager = await build_subagent_toolmanager(_context(None), provider_registry=_Registry(rec))

    refused = await _run(manager, "admin_x")
    ran = await _run(manager, "user_x")

    assert refused.error is True and "requires the 'admin' role" in refused.output, refused.output
    assert ran.error is False, "a run with no initiator is an ordinary user, not nobody: a user's tool still runs"
    assert [c[0] for c in rec.calls] == ["user_x"]
    assert manager.context_for("c1").initiated_by == UNATTRIBUTED


@pytest.mark.parametrize(
    "stored", [None, PrincipalRef.system(), ADMIN], ids=["frame-names-no-initiator", "frame-stored-system", "frame-stored-admin"],
)
async def test_a_resumed_subagent_runs_as_its_parent_session_never_as_what_the_park_stored(stored: PrincipalRef | None) -> None:
    """``None``: a frame written before ``initiated_by`` existed. The system principal: what a frame of a session that names no initiator stored
    before security review A-20, when such a session's own run was the system principal. An admin: any stored value at all, because the rank of
    the resumed run is not read from the park."""
    rec = _Recorder({"admin_x": "admin"})
    manager = await _services(rec, USER).build_subagent_toolmanager(_context(stored))

    refused = await _run(manager, "admin_x")

    assert refused.error is True and "requires the 'admin' role" in refused.output, refused.output
    assert rec.calls == [], "an admin-only tool ran in a session whose run is a user's"
    assert manager.context_for("c1").initiated_by == USER


@pytest.mark.parametrize("parent, runs", [(ADMIN, True), (None, False)], ids=["admin-session", "session-that-names-no-initiator"])
async def test_the_parent_sessions_own_rank_decides(parent: PrincipalRef | None, runs: bool) -> None:
    """The control (an admin's session still runs an admin tool, so the refusals above are not a builder that refuses everything) and a session
    row that names no initiator, which the session's own turn runs as an unattributed user (A-20): so does its subagent."""
    rec = _Recorder({"admin_x": "admin"})
    manager = await _services(rec, parent).build_subagent_toolmanager(_context(None))

    result = await _run(manager, "admin_x")

    assert result.error is (not runs), result.output
    assert [c[0] for c in rec.calls] == (["admin_x"] if runs else [])
    assert manager.context_for("c1").initiated_by == (parent or UNATTRIBUTED)


async def test_approving_a_gated_admin_tool_in_a_frame_that_names_no_initiator_does_not_run_it_for_a_users_session() -> None:
    """The escalation, through the real resume path: a subagent frame parked before ``initiated_by`` existed, an admin-only tool the subagent
    called DIRECTLY (not through ``call_tool``) behind an approval gate, and an approval. ``apply_leaf`` re-runs the tool through the subagent's
    own manager, which was the system principal for such a frame: the approved admin-only tool ran in a session whose run is a user's."""
    rec = _Recorder({"admin_x": "admin"})
    frame = AgentFrame(agent_id="sub", llm_messages=[], tool_call_id="call-invoke", depth=1, context=_context(None))
    leaf = Yielded(
        tool_name="_approval",
        event_key="tool_approval:s-1:call-gated",
        resume_metadata={"original_call": {"id": "call-gated", "name": "rec__admin_x", "arguments": {}}},
    )

    out = await apply_leaf(frame, leaf, {"decision": "approved"}, _services(rec, USER))

    assert isinstance(out, ToolResultPart) and out.id == "call-gated" and out.error is True, out
    assert "requires the 'admin' role" in out.output
    assert rec.calls == [], "the approved admin-only tool ran as the system principal"
