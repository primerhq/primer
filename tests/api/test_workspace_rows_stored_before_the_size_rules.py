"""A workspace whose stored template snapshot predates the G5 size rules is still served (the review of #869, F1).

The storage reads such a row with the stored-row context, and the new size validators let its value through. But the REST list and get
built their answer from ``row.model_dump()``, which validated the snapshot again WITHOUT that context: one such row made
``GET /v1/workspaces`` answer 500 for every caller, and ``GET /v1/workspaces/{id}`` for it. They build it from the stored field values
now. This runs the real router and the real ``workspaces`` tools over a real SQLite storage, so every read goes through ``_from_row``.

A Local template stored with ``resources.cpu_cores: 0`` (Local ignores the size, so the create never checked it) used to create a
workspace carrying that snapshot; the create refuses it now, like the other stored sizes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.common import STORAGE_LOAD_CONTEXT
from primer.model.provider import SqliteConfig
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.caller import caller
from tests.api.test_workspaces import _FakeBackend

#: Snapshots the rules of the G5 audit refuse on a write, as rows stored before them hold them.
_LEGACY = {
    "pvc-not-a-quantity": {"backend": {"kind": "kubernetes", "image": "rt:1", "pvc_size": "10 Gi"}},
    "pvc-2Ei": {"backend": {"kind": "kubernetes", "image": "rt:1", "pvc_size": "2Ei"}},
    "k8s-cpu-negative": {"backend": {"kind": "kubernetes", "image": "rt:1"}, "resources": {"cpu_cores": -1}},
    "docker-memory-zero": {"provider_id": "dock", "backend": {"kind": "container", "memory_bytes": 0}},
    "local-cpu-zero": {"provider_id": "loc", "resources": {"cpu_cores": 0}},
}


@pytest.fixture
async def world(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    workspace_registry = WorkspaceRegistry(sp, factory=_FakeBackend)
    app = create_test_app(
        storage_provider=sp,
        provider_registry=ProviderRegistry(
            sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
            cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
        ),
        workspace_registry=workspace_registry,
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        r = await client.post("/v1/auth/register", json={"username": "legacyadmin", "password": "legacyadminpass1"})
        assert r.status_code == 200, r.text
        yield sp, client, build_workspaces_toolset(storage_provider=sp, workspace_registry=workspace_registry)
    await sp.aclose()


async def _store_a_legacy_workspace(sp, legacy: dict) -> WorkspaceTemplate:
    old = WorkspaceTemplate.model_validate(
        {"id": "old", "provider_id": "k3s", "description": "stored before the G5 rules", **legacy}, context=STORAGE_LOAD_CONTEXT,
    )
    await sp.get_storage(WorkspaceTemplate).create(old)
    await sp.get_storage(Workspace).create(Workspace(
        id="w-old", template_id="old", template_snapshot=old, provider_id=old.provider_id, created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://runtime.test:5959/", token=SecretStr("token")),
    ))
    return old


@pytest.mark.parametrize("legacy", list(_LEGACY.values()), ids=list(_LEGACY))
async def test_the_rest_list_and_get_serve_a_workspace_whose_snapshot_predates_the_rules(world, legacy):
    sp, client, _ = world
    old = await _store_a_legacy_workspace(sp, legacy)

    got = await client.get("/v1/workspaces/w-old")
    listed = await client.get("/v1/workspaces")

    assert (got.status_code, listed.status_code) == (200, 200), (got.text, listed.text)
    assert got.json()["template_snapshot"] == json.loads(old.model_dump_json()) and got.json()["is_default"] is False
    assert [row["id"] for row in listed.json()["items"]] == ["w-old"]
    assert listed.json()["items"][0]["template_snapshot"] == got.json()["template_snapshot"]
    assert listed.json()["items"][0]["session_count"] == 0


@pytest.mark.parametrize("legacy", list(_LEGACY.values()), ids=list(_LEGACY))
async def test_the_workspaces_tools_read_a_workspace_whose_snapshot_predates_the_rules(world, legacy):
    sp, _, toolset = world
    await _store_a_legacy_workspace(sp, legacy)
    # The caller's session is in prime (the default workspace), which reaches every workspace (#870's reach rule): a session in another
    # workspace is answered "does not exist" for w-old, which this test does not test.
    await sp.set_default_workspace_id(caller("admin").workspace_id)

    got = await toolset.call(tool_name="get_workspace", arguments={"id": "w-old"}, ctx=caller("admin"))
    listed = await toolset.call(tool_name="list_workspaces", arguments={}, ctx=caller("admin"))

    assert not got.is_error and not listed.is_error, (got.output, listed.output)
    assert json.loads(got.output)["id"] == "w-old"
    assert [row["id"] for row in json.loads(listed.output)["items"]] == ["w-old"]


async def test_a_stored_local_template_with_a_cpu_of_zero_is_refused_at_create_and_the_list_stays_up(world, tmp_path):
    sp, client, _ = world
    await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider(
        id="loc", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path=str(tmp_path / "ws")),
    ))
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate.model_validate(
        {"id": "loc-old", "provider_id": "loc", "description": "d", "resources": {"cpu_cores": 0}}, context=STORAGE_LOAD_CONTEXT,
    ))

    created = await client.post("/v1/workspaces", json={"template_id": "loc-old"})
    listed = await client.get("/v1/workspaces")

    assert created.status_code == 422, created.text
    assert created.json()["detail"] == (
        "template 'loc-old' sets resources.cpu_cores to 0.0 (not more than zero). Edit the template."
    )
    assert listed.status_code == 200 and listed.json()["items"] == []
