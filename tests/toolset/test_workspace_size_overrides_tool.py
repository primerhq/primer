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


class _Calls(list):
    """What the backends were asked to ``create`` (the list itself), plus ``built``: the provider ids the registry's factory was
    asked to build a backend for. A refused create must reach neither, because every check of ``WorkspaceRegistry.materialise``
    runs before ``get_backend`` (review of #789, nit 1): ``create`` alone would stay empty even if a check moved after the backend
    had been built."""

    def __init__(self) -> None:
        super().__init__()
        self.built: list[str] = []


def _assert_nothing_built(calls: _Calls) -> None:
    assert (list(calls), calls.built) == ([], []), "a refused create reached the backend factory or create()"


@pytest.fixture
def calls() -> _Calls:
    return _Calls()


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
    def factory(provider):
        calls.built.append(provider.id)
        return _RecordingBackend(provider, calls)

    registry = WorkspaceRegistry(sp, factory=factory)
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
    _assert_nothing_built(calls)
    assert (await sp.get_storage(Workspace).list(_page())).items == []


async def test_a_local_size_override_answers_validation_error(toolset, calls):
    result = await _create(toolset, "tpl-loc", {"cpu_cores": 2, "memory_bytes": 2**30})

    assert result.is_error and json.loads(result.output)["type"] == "validation-error"
    message = json.loads(result.output)["message"]
    assert "overrides.resources.cpu_cores" in message and "overrides.resources.memory_bytes" in message
    _assert_nothing_built(calls)


async def test_kubernetes_takes_every_size_and_the_backend_sees_it(toolset, calls):
    result = await _create(toolset, "tpl-k3s", {"cpu_cores": 4, "memory_bytes": 8 * 2**30, "disk_bytes": 21474836480})

    assert not result.is_error, result.output
    assert len(calls) == 1 and calls[0].resources.disk_bytes == 21474836480 and calls.built == ["k3s"]


async def test_docker_takes_cpu_and_memory(toolset, calls):
    result = await _create(toolset, "tpl-dock", {"cpu_cores": 1.5, "memory_bytes": 2147483648})

    assert not result.is_error, result.output
    assert calls[0].resources.cpu_cores == 1.5 and calls.built == ["dock"]


def _page():
    from primer.model.storage import OffsetPage

    return OffsetPage(offset=0, length=10)


# ---- lead ruling 17 of plan 08 (from the review of #782) ---------------------------------------------------------------------


@pytest.mark.parametrize("field, absurd", [("cpu_cores", 1e308), ("memory_bytes", 10**30), ("disk_bytes", 10**30)])
async def test_an_absurd_size_override_answers_validation_error_naming_the_field(toolset, calls, field, absurd):
    result = await _create(toolset, "tpl-k3s", {field: absurd})

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert field in json.loads(result.output)["message"]
    _assert_nothing_built(calls)


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
    _assert_nothing_built(calls)


#: What an ``invalid_env_name`` refusal says (lead ruling 17 of plan 08, FINAL from the review of #789).
_INVALID_ENV_NAME = "An environment variable name is 1 to 256 characters of ASCII letters, digits, '_', '-' and '.'. It does not start with a digit or with '..', and it is not '.'. Rename or remove it."

#: Names Kubernetes accepts that the first (POSIX-only) version of the rule refused, one that starts with an underscore, dots that
#: are not the ``.`` / ``..`` / ``..x`` start, and the longest allowed.
_KUBERNETES_NAMES = ["my-var", "a.b", "_x", ".x", "x..", "a" * 256]


@pytest.mark.parametrize("name", ["1BAD", "a b", "A=B", "", "café", ".", "..", "..x", "RUNTIME_TOKEN", "PRIMER_RUNTIME_TOKEN"])
async def test_a_bad_env_name_in_the_overrides_answers_validation_error_naming_it(toolset, calls, name):
    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-k3s", "overrides": {"env": {name: "the-secret-value"}}},
        ctx=caller("user"),
    )

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert f"overrides.env.{ascii(name)}: " in json.loads(result.output)["message"]
    assert "the-secret-value" not in result.output
    _assert_nothing_built(calls)


async def test_an_invalid_env_name_is_refused_with_the_kubernetes_rule_in_the_message(toolset, calls):
    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-k3s", "overrides": {"env": {"1BAD": "v"}}},
        ctx=caller("user"),
    )

    assert result.is_error and json.loads(result.output)["message"] == f"overrides.env.'1BAD': {_INVALID_ENV_NAME}", result.output
    _assert_nothing_built(calls)


async def test_a_257_character_env_name_answers_validation_error_and_the_message_cuts_it(toolset, calls):
    name = "a" * 257

    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-k3s", "overrides": {"env": {name: "v"}}},
        ctx=caller("user"),
    )

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    message = json.loads(result.output)["message"]
    assert message.startswith("overrides.env.") and name not in message and "..." in message
    _assert_nothing_built(calls)


async def test_a_name_with_a_newline_is_shown_escaped_in_the_message(toolset, calls):
    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-k3s", "overrides": {"env": {"a\nb": "v"}}},
        ctx=caller("user"),
    )

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    message = json.loads(result.output)["message"]
    assert "\n" not in message and "'a\\nb'" in message, message
    _assert_nothing_built(calls)


@pytest.mark.parametrize("name", ["1BAD", "has space", "a" * 257, ".", "..", "..x", "PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"])
async def test_a_template_env_the_cluster_would_refuse_answers_validation_error(toolset, sp, calls, name):
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-env", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        env={name: SecretStr("the-secret-value")},
    ))

    result = await toolset.call(tool_name="create_workspace", arguments={"template_id": "tpl-env"}, ctx=caller("user"))

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    message = json.loads(result.output)["message"]
    assert message.startswith("template 'tpl-env' sets env.")
    if len(ascii(name)) <= 80:
        assert f"env.{ascii(name)}: " in message
    assert "the-secret-value" not in result.output
    _assert_nothing_built(calls)


@pytest.mark.parametrize("name", _KUBERNETES_NAMES)
async def test_a_kubernetes_env_name_in_the_overrides_creates_the_workspace(toolset, calls, name):
    result = await toolset.call(
        tool_name="create_workspace",
        arguments={"template_id": "tpl-k3s", "overrides": {"env": {name: "v"}}},
        ctx=caller("user"),
    )

    assert not result.is_error, result.output
    assert len(calls) == 1 and list(calls[0].env) == [name]


@pytest.mark.parametrize("name", _KUBERNETES_NAMES)
async def test_a_stored_template_with_a_kubernetes_env_name_still_creates_workspaces(toolset, sp, calls, name):
    """The first (POSIX-only) version of the rule turned such a template into a refusal on every create."""
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="tpl-env", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        env={name: SecretStr("v")},
    ))

    result = await toolset.call(tool_name="create_workspace", arguments={"template_id": "tpl-env"}, ctx=caller("user"))

    assert not result.is_error, result.output
    assert len(calls) == 1


# ---- the G5 audit: the template's own disk (backend.pvc_size) and sizes of zero or less ----------------------------------------


@pytest.mark.parametrize("pvc_size", ["10 Gi", "lots", "-5Gi", "1000Pi"])
async def test_a_template_disk_no_workspace_may_have_is_a_validation_error_on_the_template_write(toolset, sp, pvc_size):
    body = {"id": "tpl-disk", "provider_id": "k3s", "description": "d", "backend": {"kind": "kubernetes", "image": "rt:1", "pvc_size": pvc_size}}

    result = await toolset.call(tool_name="create_workspace_template", arguments={"entity": body}, ctx=caller("user"))

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    assert "pvc_size" in json.loads(result.output)["message"]
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-disk") is None


async def test_a_stored_template_with_a_disk_no_workspace_may_have_answers_validation_error_at_create(toolset, sp, calls):
    from primer.model.common import STORAGE_LOAD_CONTEXT

    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate.model_validate({
        "id": "tpl-old-disk", "provider_id": "k3s", "description": "d",
        "backend": {"kind": "kubernetes", "image": "rt:1", "pvc_size": "lots"},
    }, context=STORAGE_LOAD_CONTEXT))

    result = await toolset.call(tool_name="create_workspace", arguments={"template_id": "tpl-old-disk"}, ctx=caller("user"))

    assert result.is_error and json.loads(result.output)["type"] == "validation-error", result.output
    message = json.loads(result.output)["message"]
    assert message.startswith("template 'tpl-old-disk' sets backend.pvc_size to 'lots' (not a Kubernetes quantity"), message
    _assert_nothing_built(calls)
