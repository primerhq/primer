"""The M5 liveness guard runs against the fully wired production registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api import _app_lifespan
from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.api.route_tools import coverage_projection, validate_curated_route_declarations
from primer.knowledge.wiki.reference import reference_pages, reference_path
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from primer.model.except_ import NotFoundError
from primer.model.scheduler import RuntimeMode
from primer.toolset.tool_catalog import collect_tool_entries


async def test_full_lifespan_validates_fixed_tools_and_builds_their_reference_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_providers: list[set[str]] = []

    async def checked(app):
        checked_providers.append(set(app.state.provider_registry.builtin_toolset_providers()))
        await validate_curated_route_declarations(app)

    monkeypatch.setattr(_app_lifespan, "validate_curated_route_declarations", checked)
    # Isolate persistence and avoid wiki ingestion here: reference rendering is
    # checked directly against every live descriptor below. No model or network.
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(tmp_path))
    app = create_app(AppConfig(
        runtime_mode=RuntimeMode.API,
        auto_bootstrap=False,
        db={"provider": "sqlite", "config": {"path": tmp_path / "m5.sqlite"}},
    ))
    manifest = json.loads(Path("scripts/data/tools_universal_m0_manifest.json").read_text())
    frozen_ids = {tool["scoped_id"] for tool in manifest["tool_catalogue"]}
    async with app.router.lifespan_context(app):
        assert len(checked_providers) == 1
        assert {
            "system", "crud", "collections", "workspaces", "harness", "trigger",
            "canvas", "misc", "web", "workspace_ext",
        } <= checked_providers[0]
        entries = await collect_tool_entries(app.state.provider_registry.builtin_toolset_providers())
        live_ids = {entry.scoped_id for entry in entries}
        assert len(entries) == len(live_ids)
        assert frozen_ids <= live_ids
        # The inventory's reserved-provider universe excludes the conditional
        # download helper and the session/client scopes; the full boot has them.
        assert live_ids - frozen_ids == {
            "web__download", "client__open_file", "workspace__edit", "workspace__exec",
            "workspace__glob", "workspace__grep", "workspace__ls", "workspace__read", "workspace__write",
        }
        pages = reference_pages(entries, used_in={})
        for entry in entries:
            page = pages[reference_path(entry.toolset_id, entry.tool.id)]
            assert page.meta["toolset"] == entry.toolset_id
            assert page.meta["tool"] == entry.tool.id
        assert sum(entry.tool.operation is not None for entry in entries) == 263
        assert sum(row.state == "curated" for row in coverage_projection(app)) == 135

        # Disabling an optional toolset is an exposure decision, not a missing
        # implementation. The canonical providers must still validate it.
        store = app.state.storage_provider.get_storage(CanvasSettings)
        settings = await store.get(CANVAS_SETTINGS_ID)
        disabled = CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=False)
        if settings is None:
            await store.create(disabled)
        else:
            await store.update(settings.model_copy(update={"agent_tools_enabled": False}))
        with pytest.raises(NotFoundError):
            await app.state.provider_registry.get_toolset("canvas")
        await validate_curated_route_declarations(app)
