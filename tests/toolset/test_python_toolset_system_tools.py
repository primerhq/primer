"""System tools for managing python toolsets. These are the escalation path.

Builds the real system toolset and reads its public predicates. The stubs
follow tests/toolset/test_internal_yielding_flags.py, which constructs this
provider the same way.
"""

from __future__ import annotations

import pytest

from primer.toolset.system import build_system_toolset

PY_TOOLS = {
    "create_python_toolset",
    "update_python_toolset_source",
    "list_python_tools",
}


class _SystemSP:
    def get_storage(self, model):  # pragma: no cover - never dispatched here
        return None


class _SystemPR:
    async def invalidate_toolset(self, *a, **k):  # pragma: no cover
        return None


def _provider():
    return build_system_toolset(
        storage_provider=_SystemSP(), provider_registry=_SystemPR()
    )


@pytest.mark.asyncio
async def test_the_three_tools_are_registered() -> None:
    ids = {t.id async for t in _provider().list_tools()}
    assert PY_TOOLS <= ids


def test_the_mutators_are_admin_gated() -> None:
    # A tool that registers arbitrary python IS a tool that runs arbitrary
    # python. It is the escalation path the runner's isolation exists to
    # contain, so a default agent must not reach it.
    provider = _provider()
    for name in ("create_python_toolset", "update_python_toolset_source"):
        assert provider.required_role(name) == "admin", name


@pytest.mark.asyncio
async def test_they_describe_themselves_properly() -> None:
    async for tool in _provider().list_tools():
        if tool.id in PY_TOOLS:
            assert "Use when" in tool.description, tool.id


def test_none_of_them_yield() -> None:
    provider = _provider()
    for name in PY_TOOLS:
        assert provider.is_yielding(name) is False, name


@pytest.mark.asyncio
async def test_their_examples_validate_against_their_schemas() -> None:
    # make_tool already enforces this; asserting it here means a bad example
    # in one of these descriptions fails this test rather than at import.
    async for tool in _provider().list_tools():
        if tool.id in PY_TOOLS:
            assert tool.examples, tool.id
