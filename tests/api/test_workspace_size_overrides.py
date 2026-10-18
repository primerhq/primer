"""``POST /v1/workspaces`` refuses a size override the workspace's provider cannot honour (platform revamp 2026-10, plan 08
task 4; master Review Focus 5), on the real router.

A Local workspace runs on the server itself and cannot be sized; Docker has no disk quota. The refusal is a 422 whose
``extensions.errors`` names ``overrides.resources.<field>``, and it comes BEFORE the backend is asked to build anything, so
nothing is created, no row and no container. Kubernetes takes all three sizes and Docker takes CPU and memory.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.storage import OffsetPage
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
    WorkspaceTemplate,
)
from tests.api.test_workspaces import _FakeBackend, _SP, _FakeWorkspace


class _RecordingBackend(_FakeBackend):
    """A backend that remembers what ``create`` was asked for, so a test can tell "refused" from "built then rolled back"."""

    def __init__(self, provider, calls: list) -> None:
        super().__init__(provider)
        self._calls = calls

    async def create(self, template, *, overrides=None, workspace_id=None, resolvers=None):
        self._calls.append({"template": template.id, "overrides": overrides})
        # One backend per provider, each with its own counter: number the workspaces across all of them.
        return await super().create(
            template, overrides=overrides, workspace_id=workspace_id or f"ws-rec-{len(self._calls)}", resolvers=resolvers,
        )


@pytest.fixture
def backend_calls() -> list:
    return []


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def app(sp, backend_calls):
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=registry,
        workspace_registry=WorkspaceRegistry(sp, factory=lambda provider: _RecordingBackend(provider, backend_calls)),
    )


@pytest.fixture
async def client(app, sp):
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/register", json={"username": "sizeadmin", "password": "sizeadminpass1"})
        assert r.status_code == 200, r.text
        providers = sp.get_storage(WorkspaceProvider)
        templates = sp.get_storage(WorkspaceTemplate)
        await providers.create(WorkspaceProvider(
            id="loc", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-size-tests"),
        ))
        await providers.create(WorkspaceProvider(
            id="dock", provider=WorkspaceProviderType.CONTAINER,
            config=ContainerWorkspaceConfig(
                runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
                reachability=ContainerReachabilityHostPort(),
            ),
        ))
        await providers.create(WorkspaceProvider(
            id="k3s", provider=WorkspaceProviderType.KUBERNETES,
            config=KubernetesWorkspaceConfig(
                connection=K8sConnectionInCluster(), namespace="ns", reachability=K8sReachabilityInCluster(),
            ),
        ))
        await templates.create(WorkspaceTemplate(id="tpl-loc", provider_id="loc", description="local"))
        await templates.create(WorkspaceTemplate(
            id="tpl-dock", provider_id="dock", description="docker", backend=ContainerTemplateConfig(),
        ))
        await templates.create(WorkspaceTemplate(
            id="tpl-k3s", provider_id="k3s", description="kubernetes", backend=KubernetesTemplateConfig(image="rt:1"),
        ))
        yield c


async def _workspace_rows(sp) -> list:
    return (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=10))).items


def _create(client, template_id: str, resources: dict):
    return client.post("/v1/workspaces", json={"template_id": template_id, "overrides": {"resources": resources}})


async def test_docker_disk_override_is_a_422_naming_the_field(client, backend_calls, sp):
    r = await _create(client, "tpl-dock", {"disk_bytes": 10737418240})

    assert r.status_code == 422
    body = r.json()
    assert body["type"] == "/errors/validation-error"
    assert body["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "resources", "disk_bytes"],
        "msg": "Docker cannot limit a workspace's disk. Remove this override.",
        "type": "unsupported_by_provider",
    }]
    assert body["detail"] == "overrides.resources.disk_bytes: Docker cannot limit a workspace's disk. Remove this override."
    assert backend_calls == [] and await _workspace_rows(sp) == []


async def test_local_size_override_is_a_422(client, backend_calls, sp):
    r = await _create(client, "tpl-loc", {"cpu_cores": 2})

    assert r.status_code == 422
    assert r.json()["extensions"]["errors"] == [{
        "loc": ["body", "overrides", "resources", "cpu_cores"],
        "msg": "A Local workspace runs on the server itself and cannot be sized. Remove this override.",
        "type": "unsupported_by_provider",
    }]
    assert backend_calls == [] and await _workspace_rows(sp) == []


async def test_local_names_every_size_field_it_was_given(client, backend_calls):
    r = await _create(client, "tpl-loc", {"cpu_cores": 1, "memory_bytes": 2**30, "disk_bytes": 2**30})

    assert r.status_code == 422
    assert [e["loc"][-1] for e in r.json()["extensions"]["errors"]] == ["cpu_cores", "memory_bytes", "disk_bytes"]
    assert backend_calls == []


async def test_docker_cpu_and_memory_reach_the_backend(client, backend_calls):
    r = await _create(client, "tpl-dock", {"cpu_cores": 1.5, "memory_bytes": 2147483648})

    assert r.status_code == 201, r.text
    assert len(backend_calls) == 1
    sent = backend_calls[0]["overrides"].resources
    assert sent.cpu_cores == 1.5 and sent.memory_bytes == 2147483648 and sent.disk_bytes is None


async def test_kubernetes_size_override_is_kept_on_the_row(client, backend_calls, sp):
    r = await _create(client, "tpl-k3s", {"cpu_cores": 4, "memory_bytes": 8 * 2**30, "disk_bytes": 21474836480})

    assert r.status_code == 201, r.text
    assert r.json()["overrides"]["resources"]["disk_bytes"] == 21474836480
    [row] = await _workspace_rows(sp)
    assert row.overrides.resources.disk_bytes == 21474836480 and row.overrides.resources.cpu_cores == 4
    assert backend_calls[0]["overrides"].resources.disk_bytes == 21474836480


async def test_a_zero_size_is_refused_by_the_model(client, backend_calls):
    r = await _create(client, "tpl-k3s", {"cpu_cores": 0})

    assert r.status_code == 422
    assert r.json()["extensions"]["errors"][0]["loc"][-1] == "cpu_cores"
    assert backend_calls == []


async def test_no_size_is_asked_of_any_provider_when_the_override_has_none(client, backend_calls):
    for template_id in ("tpl-loc", "tpl-dock", "tpl-k3s"):
        r = await client.post("/v1/workspaces", json={"template_id": template_id})
        assert r.status_code == 201, (template_id, r.text)
    r = await client.post("/v1/workspaces", json={"template_id": "tpl-loc", "overrides": {"env": {"A": "b"}, "resources": {}}})
    assert r.status_code == 201, r.text
    assert len(backend_calls) == 4


async def test_the_refusal_comes_before_the_id_is_taken(client, backend_calls, sp):
    """A refused create leaves the id free: the same request without the size creates the workspace."""
    refused = await client.post("/v1/workspaces", json={
        "id": "ws-sized", "template_id": "tpl-loc", "overrides": {"resources": {"memory_bytes": 2**30}},
    })
    assert refused.status_code == 422 and await _workspace_rows(sp) == []

    again = await client.post("/v1/workspaces", json={"id": "ws-sized", "template_id": "tpl-loc"})
    assert again.status_code == 201, again.text


def test_the_422_is_documented_on_the_create_route(app):
    assert "422" in app.openapi()["paths"]["/v1/workspaces"]["post"]["responses"]


def test_the_fake_workspace_the_tests_build_is_the_one_the_other_api_tests_use():
    """Guards the import above: these tests lean on ``tests.api.test_workspaces``'s fakes for the 201 path."""
    assert _FakeWorkspace("ws-x").id == "ws-x"
