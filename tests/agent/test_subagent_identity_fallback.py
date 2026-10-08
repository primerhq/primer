"""``system__invoke_agent`` called with no run identity starts a subagent ranked as an ordinary user (security review A-20).

``invoke_agent`` is a ``user``-floor tool. Over the MCP endpoint the handler gets no ``ToolContext``, so it used to pass
``identity=None`` to :func:`primer.agent.invoke.run_subagent`, whose tool-manager builder falls back to the system principal,
which clears every role floor: a ``role=user`` MCP caller could invoke an agent whose tools include admin-only system tools and
have them run. The handler now passes :meth:`PrincipalRef.unattributed` when it has no identity. (The builder's own fallback is
left alone: every other caller threads a real identity; see the PR's fallback audit.)
"""

from __future__ import annotations

import pytest

import primer.toolset.system as system_module
from primer.api.registries import ProviderRegistry
from primer.authz import _role_allows
from primer.model.principal import PrincipalRef
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller


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
