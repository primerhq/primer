"""Canonical workspace-root validation and fail-closed materialisation/reattach."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.common import STORAGE_LOAD_CONTEXT
from primer.model.except_ import ConfigError, ValidationError
from primer.model.workspace import (
    ContainerTemplateConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    WorkspaceTemplate,
)
from primer.workspace.k8s.backend import (
    KubernetesWorkspaceBackend,
    _build_statefulset_manifest,
    _workspace_root_from_statefulset,
)
from primer.workspace.sandbox.workspace import SandboxWorkspace


_BAD_ROOTS = ["relative", "", "/", "//x", "/x/../y", "/x/", "/x\x00y"]


def _backend_config(kind, root: str):
    if kind == "container":
        return ContainerTemplateConfig(workdir=root)
    return KubernetesTemplateConfig(image="runtime:1", workdir=root)


@pytest.mark.parametrize("kind", ["container", "kubernetes"])
@pytest.mark.parametrize("root", _BAD_ROOTS)
def test_template_workdir_refuses_noncanonical_workspace_roots(kind, root):
    with pytest.raises(PydanticValidationError, match="workspace root"):
        _backend_config(kind, root)


@pytest.mark.parametrize("kind", ["container", "kubernetes"])
def test_template_workdir_keeps_the_default_and_accepts_a_custom_root(kind):
    default = ContainerTemplateConfig() if kind == "container" else KubernetesTemplateConfig(image="runtime:1")
    assert default.workdir == "/workspace"
    assert _backend_config(kind, "/srv/work").workdir == "/srv/work"


@pytest.mark.parametrize("field, value", [
    ("extra_volumes", [{"name": "ws", "emptyDir": {}}]),
    ("extra_volume_mounts", [{"name": "ws", "mountPath": "/other"}]),
])
def test_new_kubernetes_templates_refuse_the_platform_workspace_volume_identity(field, value):
    with pytest.raises(PydanticValidationError, match="volume name 'ws' is reserved"):
        KubernetesTemplateConfig(image="runtime:1", **{field: value})


def _stored_template_with_reserved_workspace_volume(field: str) -> WorkspaceTemplate:
    template = WorkspaceTemplate(
        id="tpl", provider_id="provider", description="",
        backend=KubernetesTemplateConfig(image="runtime:1"),
    )
    body = template.model_dump(mode="json")
    body["backend"][field] = (
        [{"name": "ws", "emptyDir": {}}]
        if field == "extra_volumes"
        else [{"name": "ws", "mountPath": "/other"}]
    )
    return WorkspaceTemplate.model_validate(body, context=STORAGE_LOAD_CONTEXT)


@pytest.mark.parametrize("field", ["extra_volumes", "extra_volume_mounts"])
async def test_stored_reserved_workspace_volume_decodes_but_materialise_refuses_before_backend(field):
    template = _stored_template_with_reserved_workspace_volume(field)
    assert getattr(template.backend, field)[0].name == "ws"
    registry = _registry()

    with pytest.raises(ValidationError, match="unsafe Kubernetes volume identity"):
        await registry.materialise(template=template)
    registry.get_backend.assert_not_awaited()


async def test_kubernetes_backend_refuses_a_stored_reserved_volume_before_initialising():
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._initialised = False
    backend.initialize = AsyncMock()

    with pytest.raises(ConfigError, match="volume name 'ws' is reserved"):
        await backend.create(_stored_template_with_reserved_workspace_volume("extra_volume_mounts"))
    backend.initialize.assert_not_awaited()


def test_kubernetes_manifest_refuses_a_stored_reserved_volume_for_direct_callers():
    with pytest.raises(ConfigError, match="volume name 'ws' is reserved"):
        _build_statefulset_manifest(
            sts_name="primer-ws-1",
            namespace="primer",
            workspace_id="ws-1",
            template=_stored_template_with_reserved_workspace_volume("extra_volumes"),
            provider_cfg=KubernetesWorkspaceConfig(
                connection=K8sConnectionInCluster(),
                namespace="primer",
                reachability=K8sReachabilityInCluster(),
            ),
        )


def test_kubernetes_non_platform_extra_volumes_remain_valid_and_described():
    backend = KubernetesTemplateConfig(
        image="runtime:1",
        extra_volumes=[{"name": "scratch", "emptyDir": {}}],
        extra_volume_mounts=[{"name": "scratch", "mountPath": "/scratch"}],
    )
    template = WorkspaceTemplate(
        id="tpl", provider_id="provider", description="", backend=backend,
    )
    manifest = _build_statefulset_manifest(
        sts_name="primer-ws-1",
        namespace="primer",
        workspace_id="ws-1",
        template=template,
        provider_cfg=KubernetesWorkspaceConfig(
            connection=K8sConnectionInCluster(),
            namespace="primer",
            reachability=K8sReachabilityInCluster(),
        ),
    )
    pod = manifest["spec"]["template"]["spec"]
    assert [volume["name"] for volume in pod["volumes"]] == ["scratch"]
    assert [mount["name"] for mount in pod["containers"][0]["volumeMounts"]] == ["ws", "scratch"]
    schema = KubernetesTemplateConfig.model_json_schema()["properties"]
    assert schema["extra_volumes"]["description"] == (
        "Additional pod volumes. The name 'ws' is reserved for the platform workspace PVC."
    )
    assert schema["extra_volume_mounts"]["description"] == (
        "Additional workspace-container volume mounts. The name 'ws' is reserved for the platform workspace PVC."
    )
    definitions = KubernetesTemplateConfig.model_json_schema()["$defs"]
    assert definitions["K8sVolume"]["properties"]["name"]["description"] == (
        "Volume name. The name 'ws' is reserved for the platform workspace PVC."
    )
    assert definitions["K8sVolumeMount"]["properties"]["name"]["description"] == (
        "Volume name to mount. The name 'ws' is reserved for the platform workspace PVC."
    )


def _registry(backend=None):
    provider_store = SimpleNamespace(get=AsyncMock(return_value=None))
    storage_provider = SimpleNamespace(get_storage=lambda model: provider_store)
    registry = WorkspaceRegistry.__new__(WorkspaceRegistry)
    registry._sp = storage_provider
    registry._secret_provider = None
    registry.get_backend = AsyncMock(return_value=backend)
    return registry


@pytest.mark.parametrize("kind", ["container", "kubernetes"])
@pytest.mark.parametrize("root", _BAD_ROOTS)
async def test_a_stored_unsafe_root_decodes_but_materialise_refuses_before_building(kind, root):
    template = WorkspaceTemplate(
        id="tpl", provider_id="provider", description="",
        backend=_backend_config(kind, "/workspace"),
    )
    body = template.model_dump(mode="json")
    body["backend"]["workdir"] = root
    template = WorkspaceTemplate.model_validate(body, context=STORAGE_LOAD_CONTEXT)
    assert template.backend.workdir == root
    registry = _registry()

    with pytest.raises(ValidationError, match="backend.workdir"):
        await registry.materialise(template=template)
    registry.get_backend.assert_not_awaited()


@pytest.mark.parametrize("kind", ["container", "kubernetes"])
async def test_materialise_passes_a_valid_custom_root_to_the_backend(kind):
    workspace = object()
    backend = MagicMock()
    backend.create = AsyncMock(return_value=workspace)
    registry = _registry(backend)
    template = WorkspaceTemplate(
        id="tpl", provider_id="provider", description="",
        backend=_backend_config(kind, "/srv/work"),
    )

    assert await registry.materialise(template=template) is workspace
    assert backend.create.await_args.args[0].backend.workdir == "/srv/work"


_MISSING = object()
_DEFAULT_MOUNTS = object()


def _statefulset(
    workdir: str,
    root=_MISSING,
    *,
    mounts=_DEFAULT_MOUNTS,
    object_shape: bool = False,
):
    env = [] if root is _MISSING else [{"name": "WORKSPACE_ROOT", "value": root}]
    if mounts is _DEFAULT_MOUNTS:
        mounts = [{"name": "ws", "mountPath": workdir}]
    container = {
        "name": "workspace",
        "workingDir": workdir,
        "env": env,
        "volumeMounts": mounts,
    }
    if not object_shape:
        return {"spec": {"template": {"spec": {"containers": [container]}}}}
    object_env = [SimpleNamespace(name=item["name"], value=item["value"]) for item in env]
    object_mounts = mounts
    if isinstance(mounts, (list, tuple)):
        object_mounts = [
            SimpleNamespace(name=item.get("name"), mount_path=item.get("mountPath"))
            if isinstance(item, dict)
            else item
            for item in mounts
        ]
    object_container = SimpleNamespace(
        name="workspace",
        working_dir=workdir,
        env=object_env,
        volume_mounts=object_mounts,
    )
    return SimpleNamespace(
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[object_container]))),
    )


@pytest.mark.parametrize("object_shape", [False, True], ids=["dict", "object"])
def test_statefulset_root_accepts_matching_new_and_legacy_default_shapes(object_shape):
    assert _workspace_root_from_statefulset(
        _statefulset("/srv/work", "/srv/work", object_shape=object_shape),
    ) == "/srv/work"
    assert _workspace_root_from_statefulset(
        _statefulset("/workspace", object_shape=object_shape),
    ) == "/workspace"


@pytest.mark.parametrize("workdir, root", [
    ("/srv/work", _MISSING),
    ("/srv/work", "/workspace"),
    ("/srv/work", "/"),
    ("/srv/work", "/x/../srv/work"),
])
def test_statefulset_root_refuses_unproven_legacy_mismatch_or_noncanonical_values(workdir, root):
    with pytest.raises(ConfigError):
        _workspace_root_from_statefulset(_statefulset(workdir, root))


@pytest.mark.parametrize("object_shape", [False, True], ids=["dict", "object"])
@pytest.mark.parametrize("mounts", [
    "not-a-list",
    [],
    [{"name": "ws", "mountPath": "/workspace"}, {"name": "ws", "mountPath": "/workspace"}],
    [{"name": "ws"}],
    [{"name": "ws", "mountPath": "/"}],
    [{"name": "ws", "mountPath": "/workspace/"}],
    [{"name": "ws", "mountPath": "/somewhere-else"}],
])
def test_statefulset_root_refuses_missing_duplicate_malformed_or_mismatched_ws_mounts(
    object_shape, mounts,
):
    with pytest.raises(ConfigError):
        _workspace_root_from_statefulset(
            _statefulset(
                "/workspace",
                "/workspace",
                mounts=mounts,
                object_shape=object_shape,
            ),
        )


def _k8s_backend(statefulset):
    backend = KubernetesWorkspaceBackend.__new__(KubernetesWorkspaceBackend)
    backend._config = KubernetesWorkspaceConfig(
        connection=K8sConnectionInCluster(), namespace="primer", reachability=K8sReachabilityInCluster(),
    )
    backend._core_v1 = AsyncMock()
    backend._apps_v1 = AsyncMock()
    backend._apps_v1.read_namespaced_stateful_set = AsyncMock(return_value=statefulset)
    backend._workspaces = {}
    backend._lock = asyncio.Lock()
    backend._initialised = True
    backend._wait_for_pod_running = AsyncMock()
    backend._read_runtime_token = AsyncMock(return_value="token")
    backend._connect_runtime = AsyncMock(return_value=MagicMock())
    return backend


async def test_k8s_reattach_uses_the_live_matching_root(monkeypatch):
    captured = {}
    workspace = MagicMock()

    async def materialise(**kwargs):
        captured.update(kwargs)
        return workspace

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(materialise))
    backend = _k8s_backend(_statefulset("/srv/work", "/srv/work", object_shape=True))
    template = WorkspaceTemplate(
        id="tpl", provider_id="provider", description="",
        backend=KubernetesTemplateConfig(image="runtime:1", workdir="/srv/work"),
    )

    assert await backend._reattach("ws-1", template) is workspace
    assert captured["workspace_root"] == "/srv/work"
    assert captured["sandbox"].workspace_root == "/srv/work"
    backend._apps_v1.read_namespaced_stateful_set.assert_awaited_once()


@pytest.mark.parametrize("statefulset", [
    _statefulset("/srv/work"),
    _statefulset("/srv/work", "/workspace"),
    _statefulset("/workspace", "/"),
    _statefulset("/workspace", "/workspace/"),
    _statefulset("/workspace", "/workspace", mounts=[]),
])
async def test_k8s_reattach_refuses_before_opening_a_client(statefulset):
    backend = _k8s_backend(statefulset)
    template = WorkspaceTemplate(
        id="tpl", provider_id="provider", description="",
        backend=KubernetesTemplateConfig(image="runtime:1", workdir="/srv/work"),
    )

    with pytest.raises(ConfigError):
        await backend._reattach("ws-1", template)
    backend._read_runtime_token.assert_not_awaited()
    backend._connect_runtime.assert_not_awaited()
