"""Unit tests for KubernetesWorkspaceBackend's manifest helpers.

Exercise pure-Python utilities (deep-merge + manifest construction)
without needing a real K8s cluster.
"""

from __future__ import annotations

from primer.model.workspace import (
    K8sConnectionInCluster,
    K8sGpu,
    K8sReachabilityInCluster,
    K8sToleration,
    KubernetesWorkspaceConfig,
    ResourceLimits,
    WorkspaceResourceOverrides,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
    KubernetesTemplateConfig,
)
import pytest
from pydantic import SecretStr

from primer.model.except_ import ConfigError
from primer.workspace.k8s.backend import (
    _RUNTIME_TOKEN_ENV_NAMES,
    _assert_allowlisted,
    _build_statefulset_manifest,
    _bytes_to_quantity,
    _deep_merge,
    _pvc_name_for,
    _validate_template_overrides,
)
from primer.workspace.k8s.naming import k8s_object_name


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


def test_manifest_starts_the_runtime_when_the_template_has_no_entrypoint() -> None:
    container = _pod_spec()["containers"][0]

    assert container["command"] == ["python", "-m", "primer_runtime.server"]


def test_manifest_keeps_an_explicit_entrypoint_and_args() -> None:
    container = _pod_spec(
        entrypoint=["/opt/custom-runtime"],
        args=["--listen", "5959"],
    )["containers"][0]

    assert container["command"] == ["/opt/custom-runtime"]
    assert container["args"] == ["--listen", "5959"]


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


def _pod_spec(**template_overrides) -> dict:
    manifest = _build_statefulset_manifest(
        sts_name="primer-ws-abc",
        namespace="default",
        workspace_id="abc",
        template=_template(**template_overrides),
        provider_cfg=_provider_cfg(),
    )
    return manifest["spec"]["template"]["spec"]


def test_the_pod_shares_its_process_namespace_so_the_pods_pid_1_reaps_orphans() -> None:
    """The container ALWAYS gets a ``command`` here (the template's entrypoint, or the runtime server), and a Kubernetes
    ``command`` replaces the image's ENTRYPOINT, so the tini baked into the runtime image never runs in a workspace pod.
    With a shared process namespace the pod's pause container is PID 1 and reaps what the command leaves orphaned."""
    assert _pod_spec()["shareProcessNamespace"] is True


def test_a_template_can_turn_the_shared_process_namespace_off() -> None:
    assert _pod_spec(pod_overrides={"shareProcessNamespace": False})["shareProcessNamespace"] is False


def test_the_shared_process_namespace_does_not_widen_what_the_pod_may_share_with_the_host() -> None:
    """``hostPID`` (the NODE's process namespace) stays refused in ``pod_overrides``; ``shareProcessNamespace`` is the
    pod's own, and is not a way around it."""
    with pytest.raises(ConfigError, match="hostPID"):
        _validate_template_overrides(_template(pod_overrides={"hostPID": True}))
    _validate_template_overrides(_template(pod_overrides={"shareProcessNamespace": False}))   # does not raise


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


def test_assert_allowlisted_path_in_message() -> None:
    overlay = {"deeply": {"nested": {"hostPath": {"path": "/"}}}}
    spec = {"deeply": {"nested": {"emptyDir": None}}}
    with pytest.raises(ConfigError, match=r"deeply\.nested\.hostPath"):
        _assert_allowlisted(overlay, spec, source="x")


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


# ===========================================================================
# Platform revamp 2026-10 (plan 08, task 2): override env, size and placement
# ===========================================================================


def _m(template, overrides=None):
    return _build_statefulset_manifest(
        sts_name="s", namespace="ns", workspace_id="ws-1", template=template, provider_cfg=_provider_cfg(),
        overrides=overrides,
    )


def _c(m):
    """The workspace container of a manifest."""
    return m["spec"]["template"]["spec"]["containers"][0]


def _pod(m):
    return m["spec"]["template"]["spec"]


def _pvc_request(m):
    return m["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"]


def test_override_env_reaches_the_pod_and_wins_over_the_template():
    t = _template(); t.env = {"A": SecretStr("base"), "B": SecretStr("keep")}
    env = {e["name"]: e["value"] for e in _c(_m(t, WorkspaceTemplateOverrides(env={"A": "over", "C": "new"})))["env"]}
    assert env == {"A": "over", "B": "keep", "C": "new"}


def test_the_override_env_does_not_change_the_template():
    t = _template(); t.env = {"A": SecretStr("base")}
    _m(t, WorkspaceTemplateOverrides(env={"A": "over"}))
    assert t.env == {"A": SecretStr("base")}


def test_no_env_can_replace_the_runtime_token():
    t = _template(); t.env = {"PRIMER_RUNTIME_TOKEN": SecretStr("z")}
    c = _c(_m(t, WorkspaceTemplateOverrides(env={"RUNTIME_TOKEN": "y"})))
    assert not {e["name"] for e in c["env"]} & {"PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"}
    assert c["envFrom"] == [{"secretRef": {"name": k8s_object_name("ws-1")}}]


@pytest.mark.parametrize("name", sorted(_RUNTIME_TOKEN_ENV_NAMES))
@pytest.mark.parametrize("source", ["template", "override"])
def test_neither_source_can_set_a_runtime_token_name_and_the_rest_of_the_env_stays(name, source):
    """An explicit ``env`` entry beats the Secret the pod ``envFrom``s, so a token name taken from the template or the
    override would replace the per-workspace token the platform dials the runtime with."""
    t = _template()
    overrides = None
    if source == "template":
        t.env = {name: SecretStr("z"), "KEEP": SecretStr("1")}
    else:
        overrides = WorkspaceTemplateOverrides(env={name: "y", "KEEP": "1"})
    env = {e["name"]: e["value"] for e in _c(_m(t, overrides))["env"]}
    assert env == {"KEEP": "1"}


def test_the_token_names_are_the_ones_the_workspace_secret_carries():
    assert _RUNTIME_TOKEN_ENV_NAMES == frozenset({"PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"})


def test_the_strict_locking_variable_still_comes_after_the_env():
    t = _template(); t.strict_write_locking = True; t.env = {"A": SecretStr("1")}
    env = _c(_m(t, WorkspaceTemplateOverrides(env={"B": "2"})))["env"]
    assert [e["name"] for e in env] == ["A", "B", "PRIMER_STRICT_WRITE_LOCKING"]


def test_size_overrides_replace_the_template_resources():
    t = _template(); t.resources = ResourceLimits(cpu_cores=2, memory_bytes=4 * 2**30)
    res = _c(_m(t, WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(cpu_cores=4, memory_bytes=8 * 2**30))))["resources"]
    assert res["limits"] == {"cpu": "4.0", "memory": "8589934592"} == res["requests"]


def test_a_cpu_only_override_keeps_the_templates_memory():
    t = _template(); t.resources = ResourceLimits(cpu_cores=2, memory_bytes=4 * 2**30)
    res = _c(_m(t, WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(cpu_cores=4))))["resources"]
    assert res["limits"] == {"cpu": "4.0", "memory": str(4 * 2**30)} == res["requests"]


def test_an_override_sizes_a_template_that_had_no_size():
    res = _c(_m(_template(), WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(memory_bytes=2**30))))["resources"]
    assert res == {"limits": {"memory": str(2**30)}, "requests": {"memory": str(2**30)}}


def test_a_disk_override_alone_adds_no_container_resources():
    m = _m(_template(), WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(disk_bytes=2**30)))
    assert "resources" not in _c(m)
    assert _pvc_request(m) == "1Gi"


def test_a_disk_override_sets_the_pvc_request():
    m = _m(_template(), WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(disk_bytes=20 * 2**30)))
    assert m["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert (_bytes_to_quantity(512 * 2**20), _bytes_to_quantity(1000)) == ("512Mi", "1000")


@pytest.mark.parametrize("n, quantity", [
    (2**30, "1Gi"), (10 * 2**30, "10Gi"), (2**40, "1024Gi"), (3 * 2**29, "1536Mi"), (2**20, "1Mi"),
    (512 * 2**20, "512Mi"), (2**20 + 1, str(2**20 + 1)), (1000, "1000"), (1, "1"),
])
def test_bytes_become_the_largest_whole_quantity(n, quantity):
    assert _bytes_to_quantity(n) == quantity


def test_without_overrides_the_manifest_is_what_it_was():
    assert _m(_template()) == _m(_template(), WorkspaceTemplateOverrides())
    assert _m(_template())["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "10Gi"


def test_the_template_size_and_pvc_are_used_when_nothing_is_overridden():
    t = _template(pvc_size="3Gi"); t.resources = ResourceLimits(cpu_cores=1.5, memory_bytes=2**30)
    m = _m(t, WorkspaceTemplateOverrides(env={"A": "b"}))
    assert _c(m)["resources"]["limits"] == {"cpu": "1.5", "memory": str(2**30)}
    assert _pvc_request(m) == "3Gi"


def test_placement_fields_reach_the_pod():
    t = _template(node_selector={"gpu": "true"}, tolerations=[K8sToleration(key="gpu", operator="Exists", effect="NoSchedule")],
                  gpu=K8sGpu(count=2), service_account_name="runner", image_pull_policy="IfNotPresent")
    m = _m(t); pod = m["spec"]["template"]["spec"]; c = _c(m)
    assert pod["nodeSelector"] == {"gpu": "true"}
    assert pod["tolerations"] == [{"key": "gpu", "operator": "Exists", "effect": "NoSchedule"}]
    assert pod["serviceAccountName"] == "runner" and c["imagePullPolicy"] == "IfNotPresent"
    assert c["resources"]["limits"]["nvidia.com/gpu"] == "2" == c["resources"]["requests"]["nvidia.com/gpu"]


def test_unset_placement_fields_add_no_keys():
    m = _m(_template()); pod = m["spec"]["template"]["spec"]
    assert not {"nodeSelector", "tolerations", "serviceAccountName"} & set(pod)
    assert "imagePullPolicy" not in _c(m) and "resources" not in _c(m)


def test_a_toleration_keeps_every_field_it_set_and_nothing_else():
    t = _template(tolerations=[
        K8sToleration(key="dedicated", operator="Equal", value="ml", effect="NoExecute"),
        K8sToleration(operator="Exists"),
    ])
    assert _pod(_m(t))["tolerations"] == [
        {"key": "dedicated", "operator": "Equal", "value": "ml", "effect": "NoExecute"},
        {"operator": "Exists"},
    ]


def test_a_gpu_with_another_resource_name_and_the_size_share_one_resources_block():
    t = _template(gpu=K8sGpu(count=3, resource_name="amd.com/gpu")); t.resources = ResourceLimits(cpu_cores=2, memory_bytes=2**30)
    res = _c(_m(t))["resources"]
    assert res["limits"] == {"cpu": "2.0", "memory": str(2**30), "amd.com/gpu": "3"} == res["requests"]


def test_a_gpu_alone_makes_the_resources():
    assert _c(_m(_template(gpu=K8sGpu(count=1))))["resources"] == {
        "limits": {"nvidia.com/gpu": "1"}, "requests": {"nvidia.com/gpu": "1"},
    }


def test_the_requests_are_a_copy_of_the_limits():
    t = _template(gpu=K8sGpu(count=1))
    res = _c(_m(t))["resources"]
    assert res["requests"] == res["limits"] and res["requests"] is not res["limits"]


def test_the_placement_fields_and_pod_overrides_apply_together():
    t = _template(node_selector={"zone": "a"}, pod_overrides={"dnsPolicy": "ClusterFirst", "shareProcessNamespace": False})
    pod = _pod(_m(t))
    assert pod["nodeSelector"] == {"zone": "a"}
    assert pod["dnsPolicy"] == "ClusterFirst" and pod["shareProcessNamespace"] is False


def test_the_pull_policy_lands_on_the_container_not_the_pod():
    m = _m(_template(image_pull_policy="Never"))
    assert _c(m)["imagePullPolicy"] == "Never" and "imagePullPolicy" not in _pod(m)
