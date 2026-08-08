"""Per-node ModelProfile overrides on graph agent nodes.

An agent's ``model.profile_id`` is a DEFAULT. A graph node may name a
different profile so one agent definition can run on a cheap non-reasoning
profile in one node and a reasoning profile in another.

The resolver seam is caller-supplied and almost every one in the codebase
takes a single ``agent``, so the second argument is passed only when a node
actually declares an override.
"""

from __future__ import annotations

import pytest

from primer.model.graph import _AgentNodeRef


class TestNodeShape:
    def test_defaults_to_the_agent_default(self) -> None:
        n = _AgentNodeRef(id="n1", agent_id="ag")
        assert n.profile_id is None

    def test_accepts_an_override(self) -> None:
        n = _AgentNodeRef(id="n1", agent_id="ag", profile_id="gx10--qwen-fast")
        assert n.profile_id == "gx10--qwen-fast"

    def test_round_trips_through_json(self) -> None:
        n = _AgentNodeRef(id="n1", agent_id="ag", profile_id="p--m")
        assert _AgentNodeRef.model_validate(n.model_dump(mode="json")) == n


class _Runner:
    """Minimal stand-in exposing only the resolution seam under test."""

    def __init__(self, resolver) -> None:
        self._llm_resolver = resolver

    _resolve_node_llm = None  # bound below


from primer.graph._agent_node import _AgentNodeMixin  # noqa: E402

_Runner._resolve_node_llm = _AgentNodeMixin._resolve_node_llm


class TestResolution:
    @pytest.mark.asyncio
    async def test_a_node_without_an_override_calls_the_resolver_with_one_arg(
        self,
    ) -> None:
        """Every pre-existing resolver takes a single agent; passing two
        would break every graph that never wanted an override."""
        calls = []

        async def resolver(agent, *rest):
            calls.append((agent, rest))
            return ("llm", "model")

        node = _AgentNodeRef(id="n1", agent_id="ag")
        out = await _Runner(resolver)._resolve_node_llm(node, "AGENT")
        assert out == ("llm", "model")
        assert calls == [("AGENT", ())]

    @pytest.mark.asyncio
    async def test_a_node_with_an_override_passes_it_through(self) -> None:
        calls = []

        async def resolver(agent, *rest):
            calls.append((agent, rest))
            return ("llm", "model")

        node = _AgentNodeRef(id="n1", agent_id="ag", profile_id="p--m")
        await _Runner(resolver)._resolve_node_llm(node, "AGENT")
        assert calls == [("AGENT", ("p--m",))]

    @pytest.mark.asyncio
    async def test_a_single_argument_resolver_still_works(self) -> None:
        """The 40-odd test fakes in this suite take exactly one argument."""
        async def resolver(agent):
            return ("llm", "model")

        node = _AgentNodeRef(id="n1", agent_id="ag")
        assert await _Runner(resolver)._resolve_node_llm(node, "AGENT") == (
            "llm", "model",
        )


class TestRealResolverAcceptsTheOverride:
    def test_builder_closures_take_the_optional_profile(self) -> None:
        """The graph seam is only useful if the resolver the worker builds
        actually accepts a second argument."""
        import inspect

        from primer.worker import executor_builders

        src = inspect.getsource(executor_builders)
        assert src.count(
            "async def llm_resolver(agent, profile_id: str | None = None):"
        ) == 2
