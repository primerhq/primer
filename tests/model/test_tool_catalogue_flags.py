"""tool_catalogue_flags() -- platform wave P2 (#28).

The y/w/r/n picker badges (yields, requires_workspace, tool_class,
required_role) are declared exclude=True on Tool itself so they never
reach the LLM-facing tool schema (primer/model/chat.py). Every "list
tools for a picker" route re-adds them from this one function instead
of each hand-rolling its own getattr block.
"""

from __future__ import annotations

from types import SimpleNamespace

from primer.model.chat import Tool, tool_catalogue_flags


def _tool(**overrides) -> Tool:
    base = {
        "id": "t1",
        "toolset_id": "ts1",
        "description": "d",
        "args_schema": {"type": "object", "properties": {}},
    }
    base.update(overrides)
    return Tool(**base)


def test_defaults_are_all_off() -> None:
    flags = tool_catalogue_flags(_tool())
    assert flags == {
        "yields": False,
        "requires_workspace": False,
        "tool_class": "standard",
        "required_role": None,
    }


def test_carries_every_declared_flag() -> None:
    flags = tool_catalogue_flags(_tool(
        yields=True,
        requires_workspace=True,
        tool_class="notifying",
        required_role="admin",
    ))
    assert flags == {
        "yields": True,
        "requires_workspace": True,
        "tool_class": "notifying",
        "required_role": "admin",
    }


def test_flags_are_excluded_from_the_default_dump() -> None:
    """Confirms the premise the function exists to work around: without
    tool_catalogue_flags(), a plain model_dump() drops all four."""
    dumped = _tool(yields=True).model_dump()
    assert "yields" not in dumped
    assert "requires_workspace" not in dumped
    assert "tool_class" not in dumped
    assert "required_role" not in dumped


def test_reads_via_getattr_not_a_bound_method() -> None:
    """Duck-typed on purpose: several routers' tests stand in a
    tool-like double (a bare SimpleNamespace/MagicMock here, not a real
    Tool) with only these four attributes set. A bound-method design
    (tool.catalogue_flags()) would miss a double entirely; this must
    read the same four attributes off anything, real Tool or not."""
    double = SimpleNamespace(
        yields=True, requires_workspace=False,
        tool_class="notifying", required_role="admin",
    )
    assert tool_catalogue_flags(double) == {
        "yields": True,
        "requires_workspace": False,
        "tool_class": "notifying",
        "required_role": "admin",
    }


def test_missing_attributes_fall_back_to_the_tool_defaults() -> None:
    """A double that sets none of the four still gets Tool's own
    defaults, not a crash - mirrors a MagicMock(spec=...) or a future
    tool-like object that predates one of these flags."""
    assert tool_catalogue_flags(SimpleNamespace()) == {
        "yields": False,
        "requires_workspace": False,
        "tool_class": "standard",
        "required_role": None,
    }
