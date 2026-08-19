"""The python-toolset tools live on ``crud`` now, not on ``system``.

They are the escalation path the runner's isolation exists to contain, so
they belong with the rest of the construction surface, behind the same
approval policies.
"""
from __future__ import annotations

from primer.toolset.crud import build_crud_toolset
from primer.toolset.system import build_system_toolset

PY_TOOLS = {
    "create_python_toolset",
    "update_python_toolset_source",
    "list_python_tools",
}


class _SP:
    def get_storage(self, model):  # pragma: no cover - never dispatched here
        return None


class _PR:
    async def invalidate_toolset(self, *a, **k):  # pragma: no cover
        return None


async def test_they_are_registered_on_the_crud_toolset() -> None:
    provider = build_crud_toolset(storage_provider=_SP())
    ids = {t.id async for t in provider.list_tools()}
    assert PY_TOOLS <= ids


async def test_they_are_gone_from_the_system_toolset() -> None:
    provider = build_system_toolset(
        storage_provider=_SP(), provider_registry=_PR(),
    )
    ids = {t.id async for t in provider.list_tools()}
    assert PY_TOOLS.isdisjoint(ids)


def test_the_mutators_are_still_admin_gated() -> None:
    provider = build_crud_toolset(storage_provider=_SP())
    for name in ("create_python_toolset", "update_python_toolset_source"):
        assert provider.required_role(name) == "admin", name


def test_none_of_them_yield() -> None:
    provider = build_crud_toolset(storage_provider=_SP())
    for name in PY_TOOLS:
        assert provider.is_yielding(name) is False, name
