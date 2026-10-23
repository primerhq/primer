"""The boot wiki with the canvas toolset off by design (audit G8-02; review B1 on #856).

``canvas.agent_tools_enabled`` ships off, so the seed leaves the canvas out of the built-in providers. The wiki build expects every
reserved toolset, and used to read the missing canvas as a skipped one: every boot then left the wiki as stored ("degraded: skipped
toolset(s): canvas"), and a fresh install never got one. A toolset that is off BY DESIGN is not expected.

These run the boot path for real: the real ``RESERVED_TOOLSET_IDS``, the real hand-written wiki and agent docs, the default settings
and a registry that holds every built-in provider, through ``builtin_toolset_providers`` as the lifespan calls it. Nothing is
monkeypatched.
"""
from __future__ import annotations

import logging

import pytest_asyncio

from primer.api.registries import ProviderRegistry
from primer.bootstrap.seed import builtin_toolset_providers
from primer.canvas.settings import write_canvas_settings
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.knowledge.wiki.stamp import read_stamp
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.storage.factory import StorageProviderFactory
from tests.docs._live_toolsets import built_in_providers

_VERSION = "9.0.0"
_CANVAS_PAGES = "reference/tools/canvas"


@pytest_asyncio.fixture
async def sp(tmp_path):
    provider = StorageProviderFactory.create(
        StorageProviderConfig(provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "t.sqlite")),
    )
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


def _registry(sp) -> ProviderRegistry:
    """A registry over ``sp`` (where the canvas setting is read) that holds every built-in provider, as the lifespan's does."""
    return ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
        **{f"{toolset_id}_toolset_provider": provider for toolset_id, provider in built_in_providers().items()},
    )


async def _paths(sp) -> set[str]:
    nodes = await DocumentTreeService(sp).tree(collection_id=SYSTEM_COLLECTION_ID, parent="", depth=10)
    return {node.path for node in nodes}


async def test_the_wiki_is_written_while_the_canvas_toolset_is_off(sp, caplog):
    providers = await builtin_toolset_providers(_registry(sp))
    assert "canvas" not in providers and {"system", "misc", "crud"} <= set(providers)
    with caplog.at_level(logging.INFO, logger="primer.knowledge.system_collection"):
        await regenerate_system_collection(sp, toolset_providers=providers, app_version=_VERSION)
    assert "degraded" not in caplog.text, caplog.text
    stamp = await read_stamp(sp)
    assert stamp is not None and stamp.app_version == _VERSION, "the build claimed the stamp"
    paths = await _paths(sp)
    assert "reference/tools/misc/get_datetime" in paths and "start-here/calling-tools" in paths
    assert not [path for path in paths if path.startswith(_CANVAS_PAGES)]


async def test_an_installs_stale_canvas_pages_are_pruned_on_the_next_build(sp):
    """An install that wrote the canvas reference pages (the toolset was on) loses them on the first build with it off."""
    registry = _registry(sp)
    await write_canvas_settings(sp, CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=True))
    await regenerate_system_collection(sp, toolset_providers=await builtin_toolset_providers(registry), app_version=_VERSION)
    assert {f"{_CANVAS_PAGES}/canvas_get", f"{_CANVAS_PAGES}/canvas_apply", f"{_CANVAS_PAGES}/canvas_wait"} <= await _paths(sp)
    with_canvas = (await read_stamp(sp)).content_hash

    await write_canvas_settings(sp, CanvasSettings(id=CANVAS_SETTINGS_ID))
    await regenerate_system_collection(sp, toolset_providers=await builtin_toolset_providers(registry), app_version=_VERSION)
    paths = await _paths(sp)
    assert not [path for path in paths if path.startswith(_CANVAS_PAGES)], "the stale canvas pages were pruned"
    assert "reference/tools/misc/get_datetime" in paths
    assert (await read_stamp(sp)).content_hash != with_canvas
