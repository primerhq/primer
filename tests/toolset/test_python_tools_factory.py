"""S5 P1: the python-toolset tools are built by a shared, re-scopable factory."""
from __future__ import annotations

from primer.toolset._python_tools import build_python_toolset_tools

PY_TOOLS = {
    "create_python_toolset",
    "update_python_toolset_source",
    "list_python_tools",
}


class _SP:
    def get_storage(self, model):  # pragma: no cover - never dispatched here
        return None


def test_factory_returns_the_three_tools_scoped_to_the_requested_toolset() -> None:
    built = build_python_toolset_tools(storage_provider=_SP(), toolset_id="crud")
    assert set(built) == PY_TOOLS
    for name, (tool, handler) in built.items():
        assert tool.id == name, name
        assert tool.toolset_id == "crud", name
        assert callable(handler), name


def test_factory_preserves_the_role_gates() -> None:
    built = build_python_toolset_tools(storage_provider=_SP(), toolset_id="system")
    assert built["create_python_toolset"][0].required_role == "admin"
    assert built["update_python_toolset_source"][0].required_role == "admin"
    assert built["list_python_tools"][0].required_role == "user"


def test_none_of_them_yield() -> None:
    built = build_python_toolset_tools(storage_provider=_SP(), toolset_id="crud")
    for name, (tool, _) in built.items():
        assert tool.yields is False, name
