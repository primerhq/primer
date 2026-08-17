"""Resolving a session's initial binding (S1 P5 Task 28).

A session created without naming an agent falls back to the system
default. With no default configured it is REJECTED rather than guessing
at whichever agent happens to exist, because picking one silently is
how a workspace ends up running something nobody chose.
"""

import pytest

from primer.model.except_ import ConfigError
from primer.model.system_state import SystemState
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
)
from primer.session.default_binding import (
    NO_DEFAULT_AGENT_MESSAGE,
    resolve_initial_binding,
)


class _SP:
    def __init__(self, default_agent_id=None):
        self._state = SystemState(default_agent_id=default_agent_id)

    async def get_system_state(self):
        return self._state


class TestExplicitBinding:
    async def test_agent_binding_is_returned_unchanged(self):
        requested = AgentSessionBinding(agent_id="a", profile_id="p-1")
        got = await resolve_initial_binding(
            requested=requested, storage_provider=_SP("operator"),
        )
        assert got is requested

    async def test_graph_binding_is_returned_unchanged(self):
        requested = GraphSessionBinding(graph_id="g-1")
        got = await resolve_initial_binding(
            requested=requested, storage_provider=_SP("operator"),
        )
        assert got is requested

    async def test_explicit_binding_wins_over_the_default(self):
        got = await resolve_initial_binding(
            requested=AgentSessionBinding(agent_id="chosen"),
            storage_provider=_SP("operator"),
        )
        assert got.agent_id == "chosen"


class TestDefaultFallback:
    async def test_no_binding_resolves_the_configured_default(self):
        got = await resolve_initial_binding(
            requested=None, storage_provider=_SP("operator"),
        )
        assert isinstance(got, AgentSessionBinding)
        assert got.agent_id == "operator"

    async def test_no_binding_and_no_default_is_a_config_error(self):
        with pytest.raises(ConfigError) as exc:
            await resolve_initial_binding(
                requested=None, storage_provider=_SP(None),
            )
        # The exact string is the contract: it is the whole
        # operator-facing signal until bootstrap points the key
        # at the operator agent.
        assert NO_DEFAULT_AGENT_MESSAGE in str(exc.value)
        assert "default_agent_id" in str(exc.value)
