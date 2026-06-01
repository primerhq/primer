"""HARD_DENY + is_exposable — Spec §7."""

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


def test_hard_deny_contains_meta_dispatcher_and_ssrf() -> None:
    assert "system__call_tool" in HARD_DENY
    assert "web__http-request" in HARD_DENY


def test_hard_deny_blocks_call_tool() -> None:
    tool = _make_tool("system", "call_tool")
    ok, reason = is_exposable(tool, provider=_StubProvider())
    assert ok is False
    assert reason == "hard_denied"


def test_hard_deny_blocks_http_request() -> None:
    tool = _make_tool("web", "http-request")
    ok, reason = is_exposable(tool, provider=_StubProvider())
    assert ok is False
    assert reason == "hard_denied"


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


def test_hard_deny_precedes_yielding_check() -> None:
    """Hard-deny membership wins even if the provider also flags yielding."""
    tool = _make_tool("system", "call_tool")
    ok, reason = is_exposable(tool, provider=_StubProvider(yielding=True))
    assert ok is False
    assert reason == "hard_denied"
