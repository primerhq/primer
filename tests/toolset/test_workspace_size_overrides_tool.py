"""The ``create_workspace`` tool refuses a size override the workspace's provider cannot honour (platform revamp 2026-10, plan 08
task 4), as ``POST /v1/workspaces`` does.

The check sits in ``WorkspaceRegistry.materialise``, the one place every create path passes, so the tool needs only to map the
refusal: ``type=validation-error`` with a message that names ``overrides.resources.<field>``. Nothing is created.
"""

from __future__ import annotations

import json

import pytest

from primer.api.registries import WorkspaceRegistry
from pydantic import SecretStr

from primer.model.workspace import (
    ContainerTemplateConfig,
    KubernetesTemplateConfig,
    LocalWorkspaceConfig,
    ResourceLimits,
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.caller import caller
from tests.toolset.test_workspaces import _SP, _StubBackend


class _RecordingBackend(_StubBackend):
    def __init__(self, provider, calls: list) -> None:
        super().__init__(provider)
        self._calls = calls

    async def create(self, template, *, overrides=None, workspace_id=None, resolvers=None):
        self._calls.append(overrides)
        return await super().create(template, overrides=overrides, workspace_id=workspace_id, resolvers=resolvers)


@pytest.fixture
def calls() -> list:
    return []


@pytest.fixture
async def sp() -> _SP:
    store = _SP()
    providers, templates = store.get_storage(WorkspaceProvider), store.get_storage(WorkspaceTemplate)
    await providers.create(WorkspaceProvider(id="loc", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/x")))
    await providers.create(WorkspaceProvider(
        id="dock", provider=WorkspaceProviderType.CONTAINER,
        config=ContainerWorkspaceConfig(
            runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
            reachability=ContainerReachabilityHostPort(),
        ),
    ))
    await providers.create(WorkspaceProvider(
        id="k3s", provider=WorkspaceProviderType.KUBERNETES,
        config=KubernetesWorkspaceConfig(connection=K8sConnectionInCluster(), namespace="ns", reachability=K8sReachabilityInCluster()),
    ))
    await templates.create(WorkspaceTemplate(id="tpl-loc", provider_id="loc", description="local"))
    await templates.create(WorkspaceTemplate(id="tpl-dock", provider_id="dock", description="docker", backend=ContainerTemplateConfig()))
    await templates.create(WorkspaceTemplate(id="tpl-k3s", provider_id="k3s", description="k8s", backend=KubernetesTemplateConfig(image="rt:1")))
    return store


@pytest.fixture
def toolset(sp, calls):
    registry = WorkspaceRegistry(sp, factory=lambda provider: _RecordingBackend(provider, calls))
    return build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)


async def _create(toolset, template_id: str, resources: dict):
    return await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": template_id, "overrides": {"resources": resources}},
        ctx=caller("user"),
    )


async def test_docker_disk_override_answers_validation_error_naming_the_field(toolset, sp, calls):
    result = await _create(toolset, "tpl-dock", {"disk_bytes": 10737418240})

    assert result.is_error
    out = json.loads(result.output)
    assert out["type"] == "validation-error"
    assert "overrides.resources.disk_bytes" in out["message"]
    assert "Docker cannot limit a workspace's disk" in out["message"]
    assert calls == []
    assert (await sp.get_storage(Workspace).list(_page())).items == []


async def test_a_local_size_override_answers_validation_error(toolset, calls):
    result = await _create(toolset, "tpl-loc", {"cpu_cores": 2, "memory_bytes": 2**30})

    assert result.is_error and json.loads(result.output)["type"] == "validation-error"
    message = json.loads(result.output)["message"]
    assert "overrides.resources.cpu_cores" in message and "overrides.resources.memory_bytes" in message
    assert calls == []


async def test_kubernetes_takes_every_size_and_the_backend_sees_it(toolset, calls):
    result = await _create(toolset, "tpl-k3s", {"cpu_cores": 4, "memory_bytes": 8 * 2**30, "disk_bytes": 21474836480})

    assert not result.is_error, result.output
    assert len(calls) == 1 and calls[0].resources.disk_bytes == 21474836480


async def test_docker_takes_cpu_and_memory(toolset, calls):
    result = await _create(toolset, "tpl-dock", {"cpu_cores": 1.5, "memory_bytes": 2147483648})

    assert not result.is_error, result.output
    assert calls[0].resources.cpu_cores == 1.5


def _page():
    from primer.model.storage import OffsetPage

    return OffsetPage(offset=0, length=10)


# ---- lead ruling 17 of plan 08 (from the review of #782) ---------------------------------------------------------------------


@pytest.mark.parametrize("field, absurd", [("cpu_cores", 1e308), ("memory_bytes", 10**30), ("disk_bytes", 10**30)])
async def test_an_absurd_size_override_answers_validation_error_naming_the_field(toolset, calls, field, absurd):
    result = await _create(toolset, "tpl-k3s", {field: absurd})

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert field in json.loads(result.output)["message"]
    assert calls == []


async def test_a_gpu_count_above_the_cap_is_a_validation_error_on_the_template_write(toolset, sp):
    body = {"id": "tpl-gpu", "provider_id": "k3s", "description": "d", "backend": {"kind": "kubernetes", "image": "rt:1", "gpu": {"count": 65}}}

    result = await toolset.call(tool_name="create_workspace_template", arguments={"entity": body}, ctx=caller("user"))

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert "count" in json.loads(result.output)["message"]
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-gpu") is None


async def test_a_template_with_an_absurd_memory_answers_validation_error_naming_the_template(toolset, sp, calls):
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-big", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        resources=ResourceLimits(memory_bytes=10**30),
    ))

    result = await toolset.call(tool_name="create_workspace", arguments={"template_id": "tpl-big"}, ctx=caller("user"))

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    message = json.loads(result.output)["message"]
    assert "template 'tpl-big'" in message and "resources.memory_bytes" in message
    assert calls == []


@pytest.mark.parametrize("name", ["1BAD", "a b", "RUNTIME_TOKEN"])
async def test_a_bad_env_name_in_the_overrides_answers_validation_error_naming_it(toolset, calls, name):
    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-k3s", "overrides": {"env": {name: "the-secret-value"}}},
        ctx=caller("user"),
    )

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert f"overrides.env.{name}" in json.loads(result.output)["message"]
    assert "the-secret-value" not in result.output
    assert calls == []


async def test_a_template_env_the_cluster_would_refuse_answers_validation_error(toolset, sp, calls):
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-env", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        env={"1BAD": SecretStr("v")},
    ))

    result = await toolset.call(tool_name="create_workspace", arguments={"template_id": "tpl-env"}, ctx=caller("user"))

    assert result.is_error and "template 'tpl-env' sets env.1BAD" in json.loads(result.output)["message"], result.output
    assert calls == []
