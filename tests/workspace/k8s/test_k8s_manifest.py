"""Unit tests for KubernetesWorkspaceBackend's manifest helpers.

Exercise pure-Python utilities (deep-merge + manifest construction)
without needing a real K8s cluster.
"""

from __future__ import annotations

from primer.model.workspace import (
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesWorkspaceConfig,
    WorkspaceTemplate,
    KubernetesTemplateConfig,
)
import pytest

from primer.model.except_ import ConfigError
from primer.workspace.k8s.backend import (
    _assert_no_dangerous_keys,
    _build_statefulset_manifest,
    _deep_merge,
    _pvc_name_for,
    _validate_template_overrides,
)


def _provider_cfg(**overrides) -> KubernetesWorkspaceConfig:
    """Build a valid KubernetesWorkspaceConfig with new-shape defaults."""
    base = dict(
        connection=K8sConnectionInCluster(),
        namespace="primer",
        reachability=K8sReachabilityInCluster(),
    )
    base.update(overrides)
    return KubernetesWorkspaceConfig(**base)


# ---- _deep_merge ---------------------------------------------------------


def test_deep_merge_scalars_overlay_wins() -> None:
    base = {"a": 1, "b": 2}
    overlay = {"a": 99}
    assert _deep_merge(base, overlay) == {"a": 99, "b": 2}


def test_deep_merge_dicts_recurse() -> None:
    base = {"x": {"a": 1, "b": 2}}
    overlay = {"x": {"b": 22, "c": 33}}
    assert _deep_merge(base, overlay) == {"x": {"a": 1, "b": 22, "c": 33}}


def test_deep_merge_lists_extend() -> None:
    base = {"items": [1, 2]}
    overlay = {"items": [3, 4]}
    assert _deep_merge(base, overlay) == {"items": [1, 2, 3, 4]}


def test_deep_merge_none_removes_key() -> None:
    base = {"keep": 1, "drop": 2}
    overlay = {"drop": None}
    assert _deep_merge(base, overlay) == {"keep": 1}


def test_deep_merge_overlay_only_adds_key() -> None:
    base = {"a": 1}
    overlay = {"new": "value"}
    assert _deep_merge(base, overlay) == {"a": 1, "new": "value"}


# ---- _pvc_name_for -------------------------------------------------------


def test_pvc_name_format() -> None:
    assert _pvc_name_for("primer-ws-abc") == "ws-primer-ws-abc-0"


# ---- _build_statefulset_manifest -----------------------------------------


def _template(image: str = "python:3.13", **kwargs) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t1", provider_id="k1", description="",
        backend=KubernetesTemplateConfig(image=image, **kwargs),
    )


def test_manifest_has_one_replica_and_pvc_template() -> None:
    cfg = _provider_cfg(namespace="primer")
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="primer",
        workspace_id="abc",
        template=_template(),
        provider_cfg=cfg,
    )
    assert m["spec"]["replicas"] == 1
    vct = m["spec"]["volumeClaimTemplates"]
    assert len(vct) == 1
    assert vct[0]["metadata"]["name"] == "ws"


def test_manifest_carries_image() -> None:
    cfg = _provider_cfg()
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(image="alpine:latest"),
        provider_cfg=cfg,
    )
    container = m["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "alpine:latest"


def test_pod_overrides_deep_merge() -> None:
    cfg = _provider_cfg()
    template = _template(
        pod_overrides={"dnsPolicy": "ClusterFirst", "restartPolicy": "Always"},
    )
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=template,
        provider_cfg=cfg,
    )
    pod_spec = m["spec"]["template"]["spec"]
    assert pod_spec["dnsPolicy"] == "ClusterFirst"
    assert pod_spec["restartPolicy"] == "Always"


def test_template_storage_class_lands_on_pvc() -> None:
    cfg = _provider_cfg()
    template = _template(storage_class="fast-ssd")
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=template,
        provider_cfg=cfg,
    )
    pvc = m["spec"]["volumeClaimTemplates"][0]["spec"]
    assert pvc["storageClassName"] == "fast-ssd"


def test_statefulset_env_carries_strict_flag_when_enabled() -> None:
    cfg = _provider_cfg()
    template = WorkspaceTemplate(
        id="t1", provider_id="k1", description="",
        backend=KubernetesTemplateConfig(image="python:3.13"),
        strict_write_locking=True,
    )
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=template,
        provider_cfg=cfg,
    )
    env = m["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "PRIMER_STRICT_WRITE_LOCKING", "value": "1"} in env


def test_statefulset_env_omits_strict_flag_by_default() -> None:
    cfg = _provider_cfg()
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(),
        provider_cfg=cfg,
    )
    env = m["spec"]["template"]["spec"]["containers"][0]["env"]
    assert all(e["name"] != "PRIMER_STRICT_WRITE_LOCKING" for e in env)


def test_workspace_label_present() -> None:
    cfg = _provider_cfg()
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(),
        provider_cfg=cfg,
    )
    labels = m["metadata"]["labels"]
    assert labels["primer.workspace.id"] == "abc"
    assert labels["app.kubernetes.io/managed-by"] == "primer"


# ---- _validate_template_overrides ----------------------------------------


def test_overrides_reject_host_path_volume() -> None:
    template = _template(
        extra_volumes=[
            {"name": "h", "hostPath": {"path": "/"}},
        ],
    )
    with pytest.raises(ConfigError, match="hostPath"):
        _validate_template_overrides(template)


def test_overrides_reject_host_network_in_pod_overrides() -> None:
    template = _template(pod_overrides={"hostNetwork": True})
    with pytest.raises(ConfigError, match="hostNetwork"):
        _validate_template_overrides(template)


def test_overrides_reject_nested_run_as_user() -> None:
    template = _template(
        pod_overrides={"securityContext": {"runAsUser": 0}},
    )
    with pytest.raises(ConfigError, match="securityContext"):
        _validate_template_overrides(template)


def test_overrides_accept_benign_keys() -> None:
    template = _template(
        pod_overrides={
            "dnsPolicy": "ClusterFirst",
            "restartPolicy": "Always",
        },
    )
    # Should not raise.
    _validate_template_overrides(template)


def test_assert_no_dangerous_keys_path_in_message() -> None:
    overlay = {"deeply": {"nested": {"hostPath": {"path": "/"}}}}
    with pytest.raises(ConfigError, match=r"deeply\.nested\.hostPath"):
        _assert_no_dangerous_keys(overlay, source="x")


# ===========================================================================
# list() -- cross-process / restart-durable enumeration via label selector
# ===========================================================================


from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from primer.workspace.k8s.backend import KubernetesWorkspaceBackend


def _sts_item(workspace_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels={
                "primer.workspace.id": workspace_id,
                "workspace-id": workspace_id,
                "app.kubernetes.io/managed-by": "primer",
            },
        ),
    )


def _backend_with_apps(items: list) -> KubernetesWorkspaceBackend:
    apps_v1 = MagicMock()
    apps_v1.list_namespaced_stateful_set = AsyncMock(
        return_value=SimpleNamespace(items=items),
    )
    backend = KubernetesWorkspaceBackend(
        _provider_cfg(),
        core_v1=MagicMock(),
        apps_v1=apps_v1,
        custom_objects=MagicMock(),
    )
    backend._initialised = True
    return backend


@pytest.mark.asyncio
async def test_list_enumerates_statefulsets_by_label() -> None:
    """list() maps live StatefulSets back to workspace ids -- workspaces
    materialised by another process / a previous run are surfaced."""
    backend = _backend_with_apps([_sts_item("ws-aaa"), _sts_item("ws-bbb")])
    ids = sorted(await backend.list())
    assert ids == ["ws-aaa", "ws-bbb"]
    backend._apps_v1.list_namespaced_stateful_set.assert_awaited_once_with(
        namespace="primer",
        label_selector="app.kubernetes.io/managed-by=primer",
    )


@pytest.mark.asyncio
async def test_list_unions_in_memory_and_cluster() -> None:
    """A workspace held in memory but not yet visible as an STS (mid-create)
    is not dropped from the listing."""
    backend = _backend_with_apps([_sts_item("ws-cluster")])
    backend._workspaces["ws-in-mem"] = MagicMock()
    ids = sorted(await backend.list())
    assert ids == ["ws-cluster", "ws-in-mem"]


@pytest.mark.asyncio
async def test_list_falls_back_to_memory_on_cluster_error() -> None:
    """A transient cluster query failure must not erase locally-known
    workspaces."""
    apps_v1 = MagicMock()
    apps_v1.list_namespaced_stateful_set = AsyncMock(
        side_effect=RuntimeError("apiserver unreachable"),
    )
    backend = KubernetesWorkspaceBackend(
        _provider_cfg(), core_v1=MagicMock(), apps_v1=apps_v1,
        custom_objects=MagicMock(),
    )
    backend._initialised = True
    backend._workspaces["ws-local"] = MagicMock()
    assert await backend.list() == ["ws-local"]
