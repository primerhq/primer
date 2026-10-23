"""The seed route regenerates the system collection with every built-in toolset (pivot plan 01 Task 7).

The ensure pass's system-collection step used to run with a caller-carried map
that named only ``system`` and ``crud`` (the seed route) or a hand-written nine
(the boot pass), so one list could drift from the others. ``seed.py`` now builds
the map from the live registry for every id in ``RESERVED_TOOLSET_IDS``; the
seed route and the boot pass both feed it the same way.
"""

from __future__ import annotations

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.bootstrap.seed import builtin_toolset_providers
from primer.model.except_ import NotFoundError


async def test_seed_regenerates_with_every_builtin_toolset(client, fake_storage_provider, monkeypatch):
    from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings

    await fake_storage_provider.get_storage(CanvasSettings).create(CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=True))
    seen = {}

    async def spy(storage_provider, *, toolset_providers, **kwargs):
        seen.update(toolset_providers)
        return 0

    monkeypatch.setattr("primer.knowledge.system_collection.regenerate_system_collection", spy)
    r = await client.post("/v1/setup/seed")
    assert r.status_code == 200, r.text
    assert set(seen) == set(RESERVED_TOOLSET_IDS)


async def test_a_toolset_the_registry_cannot_build_is_skipped(caplog):
    class _Reg:
        async def toolsets_off_by_design(self):
            return frozenset()               # nothing switched off on purpose: every reserved id is asked for

        async def get_toolset(self, tid):
            if tid == "web":
                raise NotFoundError("no web")
            return object()

    got = await builtin_toolset_providers(_Reg())
    assert set(got) == set(RESERVED_TOOLSET_IDS) - {"web"} and "web" in caplog.text
