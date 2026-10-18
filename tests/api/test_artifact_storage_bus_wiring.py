"""An edit of an artifact-storage provider evicts the cached store in EVERY process (plan 09 Task 3; the artifact half of ticket 01a11358).

``ArtifactStorageRegistry`` caches one live store per provider row in each process. Before this, a REST edit cleared only the cache of the process that
served it, and a system tool (it runs in whichever process executes the session, a worker in the k3s split) cleared nothing, which is why the
``update_`` / ``delete_`` descriptors said the change waited for a restart. Both paths now go through ``registry.invalidate``, which publishes on the
``artifact_storage_provider`` topic when the lifespan bound the registry to the coordinator's bus.

The unit tests of the registry itself are in ``tests/api/test_artifact_storage_registry.py``; this file pins the WIRING: the system toolset, the REST
router and the lifespan.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
from primer.coordinator.in_memory import InMemoryInvalidationBus
from primer.model.provider import ArtifactStorageProvider
from primer.toolset.system import build_system_toolset
from tests._support.caller import ADMIN_CALLER

ROOT = Path(__file__).resolve().parents[2]
COLLECTION = "/v1/artifact_storage_providers"


def _row(provider_id: str) -> ArtifactStorageProvider:
    return ArtifactStorageProvider(id=provider_id, provider="db")


async def test_a_tool_edit_evicts_the_api_processes_store(app):
    sp, reg = app.state.storage_provider, app.state.artifact_storage_registry
    await sp.get_storage(ArtifactStorageProvider).create(_row("asp-x"))
    warm = await reg.get_provider("asp-x")

    result = await app.state.system_toolset.call(
        tool_name="update_artifact_storage_provider",
        arguments={"id": "asp-x", "entity": {"id": "asp-x", "provider": "db", "config": {}}},
        ctx=ADMIN_CALLER,
    )

    assert not result.is_error, result.output
    assert warm.__class__.__name__ == "DbArtifactStorage"
    assert await reg.get_provider("asp-x") is not warm


async def test_a_tool_delete_evicts_the_api_processes_store(app):
    sp, reg = app.state.storage_provider, app.state.artifact_storage_registry
    await sp.get_storage(ArtifactStorageProvider).create(_row("asp-y"))
    await reg.get_provider("asp-y")
    assert "asp-y" in reg._instances

    result = await app.state.system_toolset.call(tool_name="delete_artifact_storage_provider", arguments={"id": "asp-y"}, ctx=ADMIN_CALLER)

    assert not result.is_error, result.output
    assert "asp-y" not in reg._instances


async def test_a_tool_edit_in_a_worker_process_evicts_the_api_processes_store(app):
    """The case the old descriptors warned about: the tool runs in a different process than the one whose cache holds the warm store."""
    sp, api_reg = app.state.storage_provider, app.state.artifact_storage_registry
    worker_reg = ArtifactStorageRegistry(storage=sp.get_storage(ArtifactStorageProvider), storage_provider=sp)
    bus = InMemoryInvalidationBus()
    await api_reg.bind_invalidation_bus(bus)
    await worker_reg.bind_invalidation_bus(bus)
    worker_toolset = build_system_toolset(
        storage_provider=sp, provider_registry=app.state.provider_registry, artifact_storage_registry=worker_reg,
    )
    await sp.get_storage(ArtifactStorageProvider).create(_row("asp-w"))
    api_warm = await api_reg.get_provider("asp-w")
    worker_warm = await worker_reg.get_provider("asp-w")

    try:
        result = await worker_toolset.call(
            tool_name="update_artifact_storage_provider",
            arguments={"id": "asp-w", "entity": {"id": "asp-w", "provider": "db", "config": {}}},
            ctx=ADMIN_CALLER,
        )

        assert not result.is_error, result.output
        assert await api_reg.get_provider("asp-w") is not api_warm
        assert await worker_reg.get_provider("asp-w") is not worker_warm
    finally:
        await worker_reg.aclose()


async def test_a_rest_edit_evicts_every_registry_on_the_bus(client, app):
    sp, api_reg = app.state.storage_provider, app.state.artifact_storage_registry
    other_reg = ArtifactStorageRegistry(storage=sp.get_storage(ArtifactStorageProvider), storage_provider=sp)
    bus = InMemoryInvalidationBus()
    await api_reg.bind_invalidation_bus(bus)
    await other_reg.bind_invalidation_bus(bus)
    assert (await client.post(COLLECTION, json={"id": "asp-r", "provider": "db", "config": {}})).status_code == 201
    api_warm, other_warm = await api_reg.get_provider("asp-r"), await other_reg.get_provider("asp-r")

    try:
        put = await client.put(f"{COLLECTION}/asp-r", json={"id": "asp-r", "provider": "db", "config": {}})
        assert put.status_code == 200, put.text
        assert await api_reg.get_provider("asp-r") is not api_warm
        assert await other_reg.get_provider("asp-r") is not other_warm

        api_warm, other_warm = await api_reg.get_provider("asp-r"), await other_reg.get_provider("asp-r")
        deleted = await client.delete(f"{COLLECTION}/asp-r")
        assert deleted.status_code in (200, 204), deleted.text
        assert "asp-r" not in api_reg._instances and "asp-r" not in other_reg._instances
    finally:
        await other_reg.aclose()


def test_the_lifespan_binds_the_artifact_registry_to_the_bus():
    src = (ROOT / "primer/api/_app_lifespan.py").read_text(encoding="utf-8")
    first = src.index("await provider_registry.bind_invalidation_bus(")
    assert src.index("await artifact_storage_registry.bind_invalidation_bus(coordinator.invalidation_bus)") > first


def test_the_lifespan_hands_the_registry_to_the_system_toolset():
    """Without it a tool edit has nothing to call: ``_inv_asp`` is a no-op when the toolset was built with no registry."""
    src = (ROOT / "primer/api/_app_lifespan.py").read_text(encoding="utf-8")
    call = src[src.index("system_toolset = build_system_toolset("):]
    call = call[: call.index("\n        )\n")]
    assert "artifact_storage_registry=artifact_storage_registry" in call


@pytest.mark.parametrize("path", ["primer/api/_app_lifespan.py", "primer/api/app.py"])
def test_the_registry_is_built_before_the_system_toolset_that_takes_it(path):
    """The toolset is built with the registry, so the registry has to exist first (the test app builds both too)."""
    src = (ROOT / path).read_text(encoding="utf-8")
    built = re.search(r"\b(?:ArtifactStorageRegistry|_ArtReg)\(", src)
    assert built is not None, "no ArtifactStorageRegistry construction found"
    assert built.start() < src.index("build_system_toolset(")
