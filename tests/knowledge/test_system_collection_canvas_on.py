"""The boot wiki with the canvas toolset on by default (pivot P5, S8: the canvas panel ships).

``canvas.agent_tools_enabled`` now ships on, so the seed holds the canvas in the built-in providers and the wiki build writes the
canvas reference pages on every install. The setting still lets an operator switch the canvas tools off; a build with it off prunes
the canvas pages it no longer holds. A toolset that is on is expected, so a build that is missing the hand-written pages is the only
one that is degraded (the canvas is never "off by design" again).

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


async def test_the_wiki_is_written_with_the_canvas_toolset_on(sp, caplog):
    providers = await builtin_toolset_providers(_registry(sp))
    assert "canvas" in providers and {"system", "misc", "crud"} <= set(providers)
    with caplog.at_level(logging.INFO, logger="primer.knowledge.system_collection"):
        await regenerate_system_collection(sp, toolset_providers=providers, app_version=_VERSION)
    assert "degraded" not in caplog.text, caplog.text
    stamp = await read_stamp(sp)
    assert stamp is not None and stamp.app_version == _VERSION, "the build claimed the stamp"
    paths = await _paths(sp)
    assert "reference/tools/misc/get_datetime" in paths and "start-here/calling-tools" in paths
    # the canvas reference pages are written: the toolset is on by default
    assert {f"{_CANVAS_PAGES}/canvas_get", f"{_CANVAS_PAGES}/canvas_apply", f"{_CANVAS_PAGES}/canvas_wait"} <= paths


async def test_a_switched_off_canvas_prunes_its_pages_on_the_next_build(sp):
    """An install that wrote the canvas reference pages (the toolset is on by default) loses them on the first build with it switched off."""
    registry = _registry(sp)
    # the default is on: the canvas pages are written
    await regenerate_system_collection(sp, toolset_providers=await builtin_toolset_providers(registry), app_version=_VERSION)
    assert {f"{_CANVAS_PAGES}/canvas_get", f"{_CANVAS_PAGES}/canvas_apply", f"{_CANVAS_PAGES}/canvas_wait"} <= await _paths(sp)
    with_canvas = (await read_stamp(sp)).content_hash

    # switch the canvas tools off: the stale canvas pages are pruned on the next build
    await write_canvas_settings(sp, CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=False))
    await regenerate_system_collection(sp, toolset_providers=await builtin_toolset_providers(registry), app_version=_VERSION)
    paths = await _paths(sp)
    assert not [path for path in paths if path.startswith(_CANVAS_PAGES)], "the stale canvas pages were pruned"
    assert "reference/tools/misc/get_datetime" in paths
    assert (await read_stamp(sp)).content_hash != with_canvas
