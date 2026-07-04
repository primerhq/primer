"""required_role: declared on the Tool, read fail-closed by the provider."""
from __future__ import annotations

import pytest

from primer.toolset._describe import make_tool
from primer.toolset.internal import InternalToolsetProvider


def _tool(id, toolset_id, required_role=None):
    return make_tool(
        id=id, toolset_id=toolset_id, purpose="p", when="w",
        args_schema={"type": "object", "properties": {}}, examples=[],
        required_role=required_role,
    )


async def _noop(arguments, **_):  # minimal handler
    from primer.model.chat import ToolCallResult
    return ToolCallResult(content=[])


def test_declared_role_is_read():
    t = _tool("create_thing", "system", required_role="user")
    assert t.required_role == "user"
    p = InternalToolsetProvider("system", {"create_thing": (t, _noop)})
    assert p.required_role("create_thing") == "user"


def test_undeclared_role_fails_closed_to_admin():
    t = _tool("mystery", "system")  # no required_role
    p = InternalToolsetProvider("system", {"mystery": (t, _noop)})
    assert p.required_role("mystery") == "admin"


def test_unknown_tool_name_fails_closed_to_admin():
    p = InternalToolsetProvider("system", {})
    assert p.required_role("nonexistent") == "admin"
