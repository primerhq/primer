"""Template deletion after workspaces gained create-time template snapshots.

A workspace's template_id is provenance. Its template_snapshot is the recipe
used for re-attachment, so deleting or editing the source template does not
change an existing workspace. Reserved templates and templates that hold
admin-only settings keep their independent authorization guards.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.auth.passwords import hash_password
from primer.model.user import User
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from tests.api.test_workspaces import _FakeBackend, _SP

_KINDS = {
    "local": ("loc", "tpl-loc"),
    "container": ("dock", "tpl-dock"),
    "kubernetes": ("k3s", "tpl-k3s"),
}


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def app(sp):
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=registry,
        workspace_registry=WorkspaceRegistry(sp, factory=_FakeBackend),
    )


@pytest.fixture
async def admin(app, sp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/auth/register",
            json={"username": "snapshotadmin", "password": "snapshotadminpass1"},
        )
        assert response.status_code == 200, response.text
        providers = sp.get_storage(WorkspaceProvider)
        templates = sp.get_storage(WorkspaceTemplate)
        await providers.create(
            WorkspaceProvider(
                id="loc",
                provider=WorkspaceProviderType.LOCAL,
                config=LocalWorkspaceConfig(root_path="/tmp/primer-snapshot-tests"),
            )
        )
        await providers.create(
            WorkspaceProvider(
                id="dock",
                provider=WorkspaceProviderType.CONTAINER,
                config=ContainerWorkspaceConfig(
                    runtime="docker",
                    connection=ContainerConnectionSocket(
                        socket_path="/var/run/docker.sock",
                    ),
                    reachability=ContainerReachabilityHostPort(),
                ),
            )
        )
        await providers.create(
            WorkspaceProvider(
                id="k3s",
                provider=WorkspaceProviderType.KUBERNETES,
                config=KubernetesWorkspaceConfig(
                    connection=K8sConnectionInCluster(),
                    namespace="ns",
                    reachability=K8sReachabilityInCluster(),
                ),
            )
        )
        await templates.create(
            WorkspaceTemplate(
                id="tpl-loc", provider_id="loc", description="local",
            )
        )
        await templates.create(
            WorkspaceTemplate(
                id="tpl-dock",
                provider_id="dock",
                description="docker",
                backend=ContainerTemplateConfig(),
            )
        )
        await templates.create(
            WorkspaceTemplate(
                id="tpl-k3s",
                provider_id="k3s",
                description="kubernetes",
                backend=KubernetesTemplateConfig(image="rt:1"),
            )
        )
        yield client


@pytest.fixture
async def user(app, admin) -> AsyncIterator[httpx.AsyncClient]:
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-snapshot",
            username="snapshotuser",
            password_hash=await hash_password("snapshotuserpass1"),
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/auth/login",
            json={"username": "snapshotuser", "password": "snapshotuserpass1"},
        )
        assert response.status_code == 200, response.text
        yield client


def _row(workspace_id: str, template: WorkspaceTemplate) -> Workspace:
    return Workspace(
        id=workspace_id,
        template_id=template.id,
        template_snapshot=template,
        provider_id=template.provider_id,
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/", token=SecretStr("t"),
        ),
    )


@pytest.mark.parametrize("kind", sorted(_KINDS))
async def test_a_template_deletes_while_its_workspace_keeps_the_snapshot(
    admin, user, sp, kind,
) -> None:
    provider_id, template_id = _KINDS[kind]
    made = await admin.post(
        "/v1/workspaces", json={"id": "ws-one", "template_id": template_id},
    )
    assert made.status_code == 201, made.text

    deleted = await user.delete(f"/v1/workspace_templates/{template_id}")

    assert deleted.status_code == 204, deleted.text
    assert await sp.get_storage(WorkspaceTemplate).get(template_id) is None
    row = await sp.get_storage(Workspace).get("ws-one")
    assert row is not None and row.provider_id == provider_id
    assert row.template_snapshot is not None
    assert row.template_snapshot.id == template_id


async def test_an_edit_does_not_drift_an_existing_workspace_snapshot(
    admin, sp,
) -> None:
    made = await admin.post(
        "/v1/workspaces", json={"id": "ws-one", "template_id": "tpl-loc"},
    )
    assert made.status_code == 201, made.text
    body = (await admin.get("/v1/workspace_templates/tpl-loc")).json()
    body["description"] = "edited later"

    updated = await admin.put("/v1/workspace_templates/tpl-loc", json=body)

    assert updated.status_code == 200, updated.text
    row = await sp.get_storage(Workspace).get("ws-one")
    assert row is not None and row.template_snapshot is not None
    assert row.template_snapshot.description == "local"


async def test_snapshot_secrets_are_stored_but_served_masked(admin, sp) -> None:
    template = WorkspaceTemplate(
        id="tpl-env",
        provider_id="loc",
        description="environment",
        env={"TOKEN": SecretStr("snapshot-secret")},
    )
    await sp.get_storage(WorkspaceTemplate).create(template)

    made = await admin.post(
        "/v1/workspaces", json={"id": "ws-env", "template_id": "tpl-env"},
    )

    assert made.status_code == 201, made.text
    assert made.json()["template_snapshot"]["env"]["TOKEN"] == "**********"
    row = await sp.get_storage(Workspace).get("ws-env")
    assert row is not None and row.template_snapshot is not None
    assert (
        row.template_snapshot.env["TOKEN"].get_secret_value()
        == "snapshot-secret"
    )


_ADMIN_HELD = WorkspaceTemplate.model_validate(
    {
        "id": "tpl-held",
        "provider_id": "loc",
        "description": "holds a secret file",
        "files": [
            {
                "path": "creds",
                "source": {"kind": "secret", "name": "OPENAI_API_KEY"},
            }
        ],
    }
)


async def test_admin_only_settings_still_guard_delete(admin, user, sp) -> None:
    await sp.get_storage(WorkspaceTemplate).create(_ADMIN_HELD)
    await sp.get_storage(Workspace).create(_row("ws-secret-user", _ADMIN_HELD))

    forbidden = await user.delete("/v1/workspace_templates/tpl-held")
    deleted = await admin.delete("/v1/workspace_templates/tpl-held")

    assert forbidden.status_code == 403
    assert forbidden.json()["extensions"]["error"] == "forbidden_role"
    assert "ws-secret-user" not in forbidden.text
    assert deleted.status_code == 204, deleted.text


async def test_the_reserved_template_is_still_protected(admin, sp) -> None:
    from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES

    reserved = next(iter(RESERVED_WORKSPACE_TEMPLATES))
    template = WorkspaceTemplate(
        id=reserved, provider_id="loc", description="reserved",
    )
    await sp.get_storage(WorkspaceTemplate).create(template)
    await sp.get_storage(Workspace).create(_row("ws-default", template))

    response = await admin.delete(f"/v1/workspace_templates/{reserved}")

    assert response.status_code == 403
    assert response.json()["extensions"]["error"] == "reserved_id_protected"


async def test_the_provider_reference_guard_is_unchanged(admin) -> None:
    response = await admin.delete("/v1/workspace_providers/loc")

    assert response.status_code == 409
    assert "in_use_by" in response.json()["detail"]
    assert "tpl-loc" in response.json()["detail"]


def test_the_openapi_no_longer_promises_template_in_use(app) -> None:
    responses = app.openapi()["paths"][
        "/v1/workspace_templates/{entity_id}"
    ]["delete"]["responses"]

    assert "403" in responses
    assert "409" not in responses
