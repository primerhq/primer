"""Tests for primer.graph.router (first_matching_branch + RouterRegistry)."""

from __future__ import annotations

import pytest

from primer.graph.router import (
    RouterRegistry,
    first_matching_branch,
)
from primer.model.chat import Message, TextPart
from primer.model.except_ import ConfigError
from primer.model.graph import (
    BranchCondition,
    GraphContext,
    JsonPathBranch,
    NodeOutput,
)


def _ctx() -> GraphContext:
    return GraphContext(
        initial_input=[Message(role="user", parts=[TextPart(text="hi")])],
        iteration=0,
        nodes={},
    )


def _output(parsed: dict | None = None, text: str = "") -> NodeOutput:
    return NodeOutput(text=text, parsed=parsed, iteration=0)


# ===========================================================================
# first_matching_branch (BranchCondition shape — AND-of-conditions per branch,
# first-match-wins across branches, empty conditions == catch-all)
# ===========================================================================


class TestFirstMatchingBranch:
    def test_first_match_wins(self) -> None:
        branches = [
            JsonPathBranch(
                conditions=[BranchCondition(path="next", op="eq", value="retry")],
                to_node="A",
            ),
            JsonPathBranch(
                conditions=[BranchCondition(path="next", op="eq", value="exit")],
                to_node="exit",
            ),
        ]
        match = first_matching_branch({"next": "exit"}, branches)
        assert match is not None and match.to_node == "exit"

    def test_no_match_returns_none(self) -> None:
        branches = [
            JsonPathBranch(
                conditions=[BranchCondition(path="next", op="eq", value="retry")],
                to_node="A",
            )
        ]
        assert first_matching_branch({"next": "exit"}, branches) is None

    def test_first_among_overlapping(self) -> None:
        # If two branches both match, the first wins.
        branches = [
            JsonPathBranch(conditions=[], to_node="catchall"),  # matches anything
            JsonPathBranch(
                conditions=[BranchCondition(path="next", op="eq", value="exit")],
                to_node="exit",
            ),
        ]
        match = first_matching_branch({"next": "exit"}, branches)
        assert match is not None and match.to_node == "catchall"


# ===========================================================================
# RouterRegistry
# ===========================================================================


class TestRouterRegistry:
    def test_register_and_check_membership(self) -> None:
        reg = RouterRegistry()

        def my_router(ctx: GraphContext, source: NodeOutput) -> str:
            return "destination"

        reg.register("my", my_router)
        assert "my" in reg

    @pytest.mark.asyncio
    async def test_resolve_sync_callable(self) -> None:
        reg = RouterRegistry()

        def my_router(ctx: GraphContext, source: NodeOutput) -> str:
            assert ctx.iteration == 0
            assert source.text == "hello"
            return "next-node"

        reg.register("my", my_router)
        result = await reg.resolve(
            "my", context=_ctx(), source=_output(text="hello")
        )
        assert result == "next-node"

    @pytest.mark.asyncio
    async def test_resolve_async_callable(self) -> None:
        reg = RouterRegistry()

        async def my_router(ctx: GraphContext, source: NodeOutput) -> str:
            return "async-dest"

        reg.register("a", my_router)
        result = await reg.resolve("a", context=_ctx(), source=_output())
        assert result == "async-dest"

    def test_register_duplicate_raises(self) -> None:
        reg = RouterRegistry()
        reg.register("x", lambda c, s: "y")
        with pytest.raises(ConfigError, match="already registered"):
            reg.register("x", lambda c, s: "z")

    def test_register_empty_id_raises(self) -> None:
        reg = RouterRegistry()
        with pytest.raises(ConfigError, match="non-empty"):
            reg.register("", lambda c, s: "y")

    @pytest.mark.asyncio
    async def test_resolve_unknown_id_raises(self) -> None:
        reg = RouterRegistry()
        with pytest.raises(ConfigError, match="not registered"):
            await reg.resolve("ghost", context=_ctx(), source=_output())

    @pytest.mark.asyncio
    async def test_resolve_non_string_return_raises(self) -> None:
        reg = RouterRegistry()
        reg.register("bad", lambda c, s: 42)  # type: ignore[arg-type,return-value]
        with pytest.raises(ConfigError, match="non-string"):
            await reg.resolve("bad", context=_ctx(), source=_output())

    @pytest.mark.asyncio
    async def test_resolve_empty_string_return_raises(self) -> None:
        reg = RouterRegistry()
        reg.register("empty", lambda c, s: "")
        with pytest.raises(ConfigError, match="non-string or empty"):
            await reg.resolve("empty", context=_ctx(), source=_output())
