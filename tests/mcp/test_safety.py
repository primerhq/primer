"""is_exposable — Spec §7 (revised).

HARD_DENY is now empty: the operator owns the exposure decision.
This file pins that the runtime constraints (yielding tools, workspace
tools that need an AgentSession) still filter, and that previously
hard-denied tools (``system__call_tool``, ``web__http_request``) are
now exposable when the operator opts them in.
"""

from __future__ import annotations

from primer.mcp.safety import HARD_DENY, is_exposable, tool_scoped_id
from primer.model.chat import Tool


class _StubProvider:
    """Minimal ToolsetProvider stand-in for the predicate's two probes."""

    def __init__(self, yielding: bool = False, needs_session: bool = False) -> None:
        self.yielding = yielding
        self.needs_session = needs_session

    def is_yielding(self, name: str) -> bool:  # noqa: ARG002 — stub
        return self.yielding

    def requires_session(self, name: str) -> bool:  # noqa: ARG002 — stub
        return self.needs_session


def _make_tool(toolset_id: str, name: str, descr: str = "") -> Tool:
    return Tool(
        id=name,
        toolset_id=toolset_id,
        description=descr,
        args_schema={"type": "object", "properties": {}},
    )


def test_hard_deny_is_now_empty() -> None:
    # Retained as an empty frozenset for back-compatibility with
    # callers that imported the name; operators own the policy floor.
    assert HARD_DENY == frozenset()


def test_previously_hard_denied_call_tool_now_exposable() -> None:
    """``system__call_tool`` was hard-denied; now the operator opts in."""
    tool = _make_tool("system", "call_tool")
    ok, reason = is_exposable(tool, provider=_StubProvider())
    assert ok is True
    assert reason is None


def test_previously_hard_denied_http_request_now_exposable() -> None:
    """``web__http_request`` was hard-denied; now the operator opts in."""
    tool = _make_tool("web", "http_request")
    ok, reason = is_exposable(tool, provider=_StubProvider())
    assert ok is True
    assert reason is None


def test_yielding_tool_blocked() -> None:
    tool = _make_tool("misc", "sleep")
    ok, reason = is_exposable(tool, provider=_StubProvider(yielding=True))
    assert ok is False
    assert reason == "yielding_unsupported"


def test_workspace_tool_needing_session_blocked() -> None:
    tool = _make_tool("workspaces", "write_file")
    ok, reason = is_exposable(tool, provider=_StubProvider(needs_session=True))
    assert ok is False
    assert reason == "needs_session"


def test_non_workspace_tool_needing_session_not_filtered_here() -> None:
    """``needs_session`` only suppresses tools in the ``workspaces`` toolset.

    Other toolsets that happen to declare a session need-flag would
    surface via a different reason (or not at all) — the workspaces
    branch is the spec-defined trigger.
    """
    tool = _make_tool("search", "search_agents")
    ok, reason = is_exposable(tool, provider=_StubProvider(needs_session=True))
    assert ok is True
    assert reason is None


def test_safe_tool_passes() -> None:
    tool = _make_tool("misc", "uuid_v4")
    ok, reason = is_exposable(tool, provider=_StubProvider())
    assert ok is True
    assert reason is None


def test_tool_scoped_id_helper() -> None:
    tool = _make_tool("search", "search_agents")
    assert tool_scoped_id(tool) == "search__search_agents"


def test_yielding_still_blocks_previously_hard_denied_tool() -> None:
    """Runtime constraints survived the policy floor removal.

    A tool that was once on HARD_DENY AND is yielding still surfaces
    ``yielding_unsupported`` — the technical constraint is real.
    """
    tool = _make_tool("system", "call_tool")
    ok, reason = is_exposable(tool, provider=_StubProvider(yielding=True))
    assert ok is False
    assert reason == "yielding_unsupported"


def test_user_toolset_tool_is_not_exposable() -> None:
    """System-only floor: a tool from a user-defined Toolset row (any
    toolset id outside the reserved built-ins) is never exposable over
    MCP, regardless of the operator allowlist."""
    tool = _make_tool("my-custom-toolset", "do_thing")
    ok, reason = is_exposable(tool, provider=_StubProvider())
    assert ok is False
    assert reason == "not_system_toolset"


def test_reserved_system_toolsets_remain_exposable() -> None:
    """Every reserved built-in toolset is still exposable (the
    system-only floor lets them through)."""
    from primer.mcp.safety import RESERVED_TOOLSET_IDS

    for toolset_id in RESERVED_TOOLSET_IDS:
        # workspaces tools that need a session are denied for a different
        # reason; use a plain non-session tool to isolate the floor.
        tool = _make_tool(toolset_id, "some_tool")
        ok, reason = is_exposable(tool, provider=_StubProvider())
        assert ok is True, f"{toolset_id} should be exposable, got {reason}"
