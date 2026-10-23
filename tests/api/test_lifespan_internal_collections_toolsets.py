"""The lifespan builds the internal-collections subsystem with every built-in toolset (the #795 review).

When a config row exists at boot, the lifespan builds the subsystem itself and hands it a toolset map: the tools of the toolsets on it
are the ones the subsystem embeds. That map used to be written by hand and had lost ``crud``, and a toolset added later (``canvas``)
could be dropped from it with every test green. It is the registry's map now (``builtin_toolset_providers``, the one the ensure pass
takes), and this boots the real lifespan (as test_lifespan_teardown.py does) to pin it to every id in ``RESERVED_TOOLSET_IDS``.
The bootstrap route builds the subsystem the same way when the row lands after boot; its twin is in test_internal_collections.py.
"""

from __future__ import annotations

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.model.internal import INTERNAL_COLLECTIONS_CONFIG_ID, InternalCollectionsConfig
from primer.model.scheduler import RuntimeMode
from tests.api.conftest import _FakeStorageProvider


@pytest.mark.asyncio
async def test_the_lifespan_builds_the_subsystem_with_every_builtin_toolset(monkeypatch: pytest.MonkeyPatch) -> None:
    from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings

    storage = _FakeStorageProvider()
    await storage.get_storage(CanvasSettings).create(CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=True))
    await storage.get_storage(InternalCollectionsConfig).create(InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID, embedding_provider_id="emb", embedding_model="m", search_provider_id="ssp",
    ))
    monkeypatch.setattr("primer.api.app._build_storage_provider", lambda _cfg: storage)
    app = create_app(AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None))
    async with app.router.lifespan_context(app):
        subsystem = app.state.internal_collections
        assert subsystem is not None, "a config row at boot builds the subsystem"
        given = subsystem._toolset_providers
        assert set(given) == set(RESERVED_TOOLSET_IDS)
        for toolset_id, provider in given.items():
            assert provider is await app.state.provider_registry.get_toolset(toolset_id), toolset_id
