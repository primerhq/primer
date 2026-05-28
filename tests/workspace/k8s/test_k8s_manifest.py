"""Unit tests for KubernetesWorkspaceBackend's manifest helpers.

Exercise pure-Python utilities (deep-merge + manifest construction)
without needing a real K8s cluster.
"""

from __future__ import annotations

from primer.model.workspace import (
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
    cfg = KubernetesWorkspaceConfig(namespace="matrix")
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="matrix",
        workspace_id="abc",
        template=_template(),
        provider_cfg=cfg,
    )
    assert m["spec"]["replicas"] == 1
    vct = m["spec"]["volumeClaimTemplates"]
    assert len(vct) == 1
    assert vct[0]["metadata"]["name"] == "ws"


def test_manifest_carries_image() -> None:
    cfg = KubernetesWorkspaceConfig()
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(image="alpine:latest"),
        provider_cfg=cfg,
    )
    container = m["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "alpine:latest"


def test_container_overrides_deep_merge() -> None:
    cfg = KubernetesWorkspaceConfig()
    template = _template(
        container_overrides={"image": "override:tag", "newField": "x"},
    )
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=template,
        provider_cfg=cfg,
    )
    container = m["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "override:tag"
    assert container["newField"] == "x"


def test_pod_overrides_deep_merge() -> None:
    cfg = KubernetesWorkspaceConfig()
    template = _template(
        pod_overrides={"hostNetwork": True, "dnsPolicy": "ClusterFirst"},
    )
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=template,
        provider_cfg=cfg,
    )
    pod_spec = m["spec"]["template"]["spec"]
    assert pod_spec["hostNetwork"] is True
    assert pod_spec["dnsPolicy"] == "ClusterFirst"


def test_provider_storage_class_lands_on_pvc() -> None:
    cfg = KubernetesWorkspaceConfig(storage_class="fast-ssd")
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(),
        provider_cfg=cfg,
    )
    pvc = m["spec"]["volumeClaimTemplates"][0]["spec"]
    assert pvc["storageClassName"] == "fast-ssd"


def test_workspace_label_present() -> None:
    cfg = KubernetesWorkspaceConfig(
        labels={"team": "platform"},
        annotations={"owner": "matrix"},
    )
    m = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(),
        provider_cfg=cfg,
    )
    labels = m["metadata"]["labels"]
    assert labels["primer.workspace.id"] == "abc"
    assert labels["team"] == "platform"
    annotations = m["metadata"]["annotations"]
    assert annotations["owner"] == "matrix"


# ---- _validate_template_overrides ----------------------------------------


def test_overrides_reject_privileged_security_context() -> None:
    template = _template(
        container_overrides={"securityContext": {"privileged": True}},
    )
    with pytest.raises(ConfigError, match="securityContext"):
        _validate_template_overrides(template)


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
        container_overrides={
            "imagePullPolicy": "Always",
            "lifecycle": {"preStop": {"exec": {"command": ["echo", "bye"]}}},
        },
    )
    # Should not raise.
    _validate_template_overrides(template)


def test_assert_no_dangerous_keys_path_in_message() -> None:
    overlay = {"deeply": {"nested": {"hostPath": {"path": "/"}}}}
    with pytest.raises(ConfigError, match=r"deeply\.nested\.hostPath"):
        _assert_no_dangerous_keys(overlay, source="x")
