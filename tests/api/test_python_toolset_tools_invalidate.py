"""A python toolset's source edited through the ``crud`` tools is seen by the warm toolset (task 01a111d1, D5 phase 2a, finding A3).

The registry caches a toolset adapter by id, built from the row at first use, and the python adapter registers its tools from
``config.source`` at construction. The toolset router drops that cache entry on every update (``_invalidate_toolset``);
``update_python_toolset_source`` wrote the row and never did, so a builder that edited a tool's code kept serving the OLD code from
the running toolset until a restart. Reproduced end to end: the update answered ``ok`` with the new tools and ``source_version`` 2,
while ``registry.get_toolset`` returned the SAME adapter still listing the old tool.

This goes through the production wiring: the ``app`` fixture builds the crud toolset the way the app does, and the toolset is
resolved from ``app.state.provider_registry`` (a reserved id, no storage row), then the warm toolset is resolved AFTER the update.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import primer.api
from primer.api.registries import ProviderRegistry
from primer.model.provider import Toolset, ToolsetProviderType

SRC_V1 = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return f"hello {name}"
'''
SRC_V2 = SRC_V1.replace("def greet(", "def salute(")


@pytest.fixture
def fake_provider_registry(fake_storage_provider):
    """A registry whose python toolsets are REAL providers (the api conftest stubs every toolset with object())."""

    def _toolset_factory(toolset):
        if toolset.provider == ToolsetProviderType.PYTHON:
            from primer.toolset.python_runner.provider import PythonToolsetProvider
            from primer.toolset.python_runner.runners import LocalHardenedRunner

            return PythonToolsetProvider(toolset_id=toolset.id, config=toolset.config, runner=LocalHardenedRunner())
        return object()

    return ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=_toolset_factory,
    )


async def _call(toolset, name: str, **args):
    result = await toolset.call(tool_name=name, arguments=args)
    return result.is_error, json.loads(result.output)


async def _tool_ids(toolset) -> list[str]:
    return sorted([tool.id async for tool in toolset.list_tools()])


async def test_a_source_update_is_seen_by_the_warm_toolset(app) -> None:
    registry = app.state.provider_registry
    crud = await registry.get_toolset("crud")
    created_error, created = await _call(crud, "create_python_toolset", toolset_id="py-warm", source=SRC_V1)
    assert not created_error and created["ok"] is True
    warm = await registry.get_toolset("py-warm")
    assert await _tool_ids(warm) == ["greet"]

    updated_error, updated = await _call(crud, "update_python_toolset_source", toolset_id="py-warm", source=SRC_V2)

    assert not updated_error and updated["ok"] is True and updated["source_version"] == 2
    stored = await app.state.storage_provider.get_storage(Toolset).get("py-warm")
    assert stored.config.source == SRC_V2, "the row was not updated, so this test would prove nothing about the cache"
    after = await registry.get_toolset("py-warm")
    assert await _tool_ids(after) == ["salute"], "the warm toolset still serves the source from before the update"


def test_every_build_crud_toolset_call_in_the_app_wiring_passes_the_registry() -> None:
    """The test app factory builds the crud toolset the way the lifespan does, but only the lifespan serves production: pin the
    keyword on both calls, so dropping it from either one cannot pass unnoticed (the API test above exercises the factory only)."""
    root = Path(primer.api.__file__).parent
    callers: dict[str, bool] = {}
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "build_crud_toolset":
                callers[path.name] = any(keyword.arg == "provider_registry" for keyword in node.keywords)

    assert set(callers) >= {"_app_lifespan.py", "app.py"}, f"the scan did not find both wiring sites ({callers}); it would pass vacuously"
    assert all(callers.values()), f"build_crud_toolset calls without provider_registry=: {[n for n, ok in callers.items() if not ok]}"


async def test_a_refused_update_leaves_the_warm_toolset_alone(app) -> None:
    registry = app.state.provider_registry
    crud = await registry.get_toolset("crud")
    await _call(crud, "create_python_toolset", toolset_id="py-managed", source=SRC_V1)
    store = app.state.storage_provider.get_storage(Toolset)
    row = await store.get("py-managed")
    row.harness_id = "hns_x"
    await store.update(row)
    warm = await registry.get_toolset("py-managed")

    is_error, answer = await _call(crud, "update_python_toolset_source", toolset_id="py-managed", source=SRC_V2)

    assert is_error and answer["type"] == "conflict"
    assert await registry.get_toolset("py-managed") is warm, "a refused update must not evict the cached toolset"
