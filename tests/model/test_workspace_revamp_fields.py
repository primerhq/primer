"""Platform revamp 2026-10 (plan 08, task 1): the template and override fields behind per-workspace size, Kubernetes
placement and the Docker image default.

* ``WorkspaceTemplateOverrides.resources`` carries a per-workspace CPU, memory and disk size; every number is positive.
* ``KubernetesTemplateConfig`` gains typed placement fields (node selector, tolerations, GPU, service account, pull policy).
* ``ContainerTemplateConfig.image`` defaults to the runtime image instead of being required.
* Rows written before the revamp keep loading, and the fields the server ignores say so in their descriptions.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model import workspace as workspace_models
from primer.model.workspace import (
    DEFAULT_GPU_RESOURCE_NAME,
    DOCKER_RUNTIME_IMAGE,
    K8S_LABEL_KEY_PATTERN,
    K8S_LABEL_VALUE_PATTERN,
    K8S_RESOURCE_NAME_PATTERN,
    K8S_SERVICE_ACCOUNT_PATTERN,
    ContainerTemplateConfig,
    K8sGpu,
    K8sToleration,
    KubernetesTemplateConfig,
    ResourceLimits,
    WorkspaceResourceOverrides,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)


def test_the_runtime_image_is_the_docker_default():
    assert DOCKER_RUNTIME_IMAGE == "primer/workspace-runtime:1.1"
    assert ContainerTemplateConfig().image == DOCKER_RUNTIME_IMAGE


def test_rows_written_before_the_revamp_still_load():
    k8s = WorkspaceTemplate.model_validate({"id": "t", "provider_id": "k", "description": "d",
        "backend": {"kind": "kubernetes", "image": "rt:1", "cpu_request": "500m", "network_policy_name": "np"}})
    assert (k8s.backend.node_selector, k8s.backend.tolerations, k8s.backend.gpu) == ({}, [], None)
    assert k8s.backend.service_account_name is None and k8s.backend.image_pull_policy is None
    docker = WorkspaceTemplate.model_validate({"id": "d", "provider_id": "c", "description": "d",
        "backend": {"kind": "container", "image": "python:3.12", "entrypoint": ["python"], "network": {"egress": "deny_all"}}})
    assert docker.backend.image == "python:3.12"
    assert WorkspaceTemplateOverrides.model_validate({"env": {"A": "b"}}).resources is None


def test_a_stored_row_keeps_every_ignored_field_through_a_dump_and_a_load():
    """The ignored fields stay on the model so a row that holds them loads and saves back unchanged."""
    body = {"id": "t", "provider_id": "k", "description": "d", "backend": {
        "kind": "kubernetes", "image": "rt:1", "cpu_request": "500m", "cpu_limit": "2", "memory_request": "1Gi",
        "memory_limit": "4Gi", "network_policy_name": "np", "container_security_context_overrides": {"a": 1}}}
    again = WorkspaceTemplate.model_validate(WorkspaceTemplate.model_validate(body).model_dump(mode="json"))
    for key, value in body["backend"].items():
        assert getattr(again.backend, key) == value, key


@pytest.mark.parametrize("field", ["cpu_cores", "memory_bytes", "disk_bytes"])
@pytest.mark.parametrize("value", [0, -1])
def test_size_overrides_are_positive(field, value):
    with pytest.raises(ValidationError):
        WorkspaceResourceOverrides(**{field: value})


def test_size_overrides_take_positive_numbers_and_default_to_none():
    assert WorkspaceResourceOverrides() == WorkspaceResourceOverrides(cpu_cores=None, memory_bytes=None, disk_bytes=None)
    ok = WorkspaceResourceOverrides(cpu_cores=0.5, memory_bytes=2**30, disk_bytes=10 * 2**30)
    assert (ok.cpu_cores, ok.memory_bytes, ok.disk_bytes) == (0.5, 2**30, 10 * 2**30)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_a_cpu_override_must_be_a_finite_number(value):
    with pytest.raises(ValidationError):
        WorkspaceResourceOverrides(cpu_cores=value)


def test_size_overrides_refuse_an_unknown_key():
    with pytest.raises(ValidationError):
        WorkspaceResourceOverrides.model_validate({"cpu": 2})


def test_the_size_overrides_ride_on_the_template_overrides_and_round_trip():
    over = WorkspaceTemplateOverrides.model_validate({"resources": {"cpu_cores": 2, "disk_bytes": 5 * 2**30}})
    assert over.resources == WorkspaceResourceOverrides(cpu_cores=2, disk_bytes=5 * 2**30)
    again = WorkspaceTemplateOverrides.model_validate(over.model_dump(mode="json"))
    assert again.resources == over.resources


def test_node_selector_takes_label_keys_and_values():
    cfg = KubernetesTemplateConfig(image="rt", node_selector={"kubernetes.io/arch": "amd64", "gpu": "true", "example.com/zone": ""})
    assert cfg.node_selector["gpu"] == "true"


@pytest.mark.parametrize("selector", [{"bad key": "x"}, {"zone": "a b"}, {"-x": "y"}])
def test_node_selector_refuses_a_bad_label(selector):
    with pytest.raises(ValidationError, match="node_selector"):
        KubernetesTemplateConfig(image="rt", node_selector=selector)


@pytest.mark.parametrize("selector, named", [
    ({"fine": "ok", "bad key": "x", "also bad": "y"}, "bad key"),
    ({"fine": "ok", "zone": "a b"}, "zone"),
    ({"x" * 64: "y"}, "x" * 64),                       # a name part is at most 63 characters
    ({"a" * 254 + "/name": "y"}, "a" * 254),           # a prefix is at most 253 characters
    ({"zone": "v" * 64}, "zone"),                      # a value is at most 63 characters
    ({"UPPER.example.com/name": "y"}, "UPPER.example.com/name"),   # the prefix is a lowercase DNS name
])
def test_node_selector_names_the_bad_label(selector, named):
    with pytest.raises(ValidationError) as caught:
        KubernetesTemplateConfig(image="rt", node_selector=selector)
    (error,) = caught.value.errors()
    assert error["loc"] == ("node_selector",)
    assert named in error["msg"]


def test_node_selector_names_only_the_first_bad_label():
    # (the whole ValidationError text echoes the input, so the check reads the error's own message)
    with pytest.raises(ValidationError) as caught:
        KubernetesTemplateConfig(image="rt", node_selector={"fine": "ok", "bad key": "x", "also bad": "y"})
    (error,) = caught.value.errors()
    assert "'bad key'" in error["msg"] and "also bad" not in error["msg"]


def test_node_selector_takes_the_longest_legal_label():
    key = ("a" * 63 + ".") * 3 + "a" * 61 + "/" + "N" * 63         # a 253-character prefix and a 63-character name
    assert len(key.split("/")[0]) == 253
    cfg = KubernetesTemplateConfig(image="rt", node_selector={key: "v" * 63})
    assert cfg.node_selector == {key: "v" * 63}


def test_toleration_rules():
    assert K8sToleration(key="gpu").operator == "Equal"
    assert K8sToleration(operator="Exists").key is None
    with pytest.raises(ValidationError):
        K8sToleration(key="gpu", operator="Exists", value="x")   # Exists takes no value
    with pytest.raises(ValidationError):
        K8sToleration(operator="Equal", value="x")               # no key needs Exists


def test_a_toleration_with_no_key_and_the_default_operator_is_refused():
    with pytest.raises(ValidationError, match="Exists"):
        K8sToleration()


def test_toleration_effects_and_unknown_keys():
    for effect in ("NoSchedule", "PreferNoSchedule", "NoExecute", None):
        assert K8sToleration(key="gpu", effect=effect).effect == effect
    with pytest.raises(ValidationError):
        K8sToleration(key="gpu", effect="Sometimes")
    with pytest.raises(ValidationError):
        K8sToleration.model_validate({"key": "gpu", "tolerationSeconds": 5})


@pytest.mark.parametrize("bad", [{"key": "bad key"}, {"key": "-x"}, {"key": ""}, {"key": "gpu", "value": "a b"}])
def test_a_toleration_key_and_value_are_label_shaped(bad):
    with pytest.raises(ValidationError):
        K8sToleration(**bad)


def test_a_toleration_takes_a_prefixed_key_and_an_empty_value():
    t = K8sToleration(key="example.com/gpu", value="")
    assert (t.key, t.value) == ("example.com/gpu", "")


def test_gpu_defaults_and_bounds():
    assert K8sGpu(count=1).resource_name == "nvidia.com/gpu"
    assert K8sGpu(count=2, resource_name="amd.com/gpu").count == 2
    for bad in ({"count": 0}, {"count": 1, "resource_name": "gpu"}):
        with pytest.raises(ValidationError):
            K8sGpu(**bad)


def test_the_default_gpu_resource_name_is_one_constant():
    assert DEFAULT_GPU_RESOURCE_NAME == "nvidia.com/gpu"
    assert K8sGpu.model_fields["resource_name"].default == DEFAULT_GPU_RESOURCE_NAME
    assert K8sGpu(count=1).resource_name == DEFAULT_GPU_RESOURCE_NAME


@pytest.mark.parametrize("name", [
    "kubernetes.io/gpu",              # the kubernetes.io namespace is Kubernetes' own
    "foo.kubernetes.io/gpu",          # so is any name that has kubernetes.io/ in it
    "requests.nvidia.com/gpu",        # the quota convention: the API server refuses the prefix on a container resource
    "requests.example.com/anything",
])
def test_a_gpu_resource_name_the_api_server_rejects_is_a_422(name):
    """Lead ruling 15: refuse at write time what the API server would refuse at create (it came back as a 500 after a rollback)."""
    with pytest.raises(ValidationError) as caught:
        K8sGpu(count=1, resource_name=name)
    (error,) = caught.value.errors()
    assert error["loc"] == ("resource_name",)
    assert "kubernetes.io" in error["msg"] and "requests." in error["msg"] and name in error["msg"]


@pytest.mark.parametrize("name", ["nvidia.com/gpu", "amd.com/gpu", "example.com/kubernetes", "kubernetes.io.example.com/gpu",
                                  "my-requests.example.com/gpu", "gpu.intel.com/i915"])
def test_a_gpu_resource_name_outside_kubernetes_own_namespace_is_accepted(name):
    assert K8sGpu(count=1, resource_name=name).resource_name == name


def test_a_template_with_a_reserved_gpu_resource_name_is_refused_as_a_whole():
    with pytest.raises(ValidationError, match="resource_name"):
        KubernetesTemplateConfig(image="rt", gpu={"count": 1, "resource_name": "kubernetes.io/gpu"})


def test_service_account_and_pull_policy():
    ok = KubernetesTemplateConfig(image="rt", service_account_name="primer-runner", image_pull_policy="IfNotPresent")
    assert ok.service_account_name == "primer-runner"
    for bad in ({"service_account_name": "Primer_Runner"}, {"image_pull_policy": "Sometimes"}):
        with pytest.raises(ValidationError):
            KubernetesTemplateConfig(image="rt", **bad)


def test_a_service_account_name_is_at_most_253_characters():
    dotted = ".".join(["a" * 63] * 3 + ["a" * 61])
    assert len(dotted) == 253
    assert KubernetesTemplateConfig(image="rt", service_account_name=dotted).service_account_name == dotted
    with pytest.raises(ValidationError):
        KubernetesTemplateConfig(image="rt", service_account_name=dotted + "a")


def test_the_service_account_and_gpu_refusals_read_in_words_not_as_a_regular_expression():
    """The console shows the server's message under the field, so it must not be the pattern itself."""
    with pytest.raises(ValidationError) as sa:
        KubernetesTemplateConfig(image="rt", service_account_name="Primer_Runner")
    with pytest.raises(ValidationError) as gpu:
        K8sGpu(count=1, resource_name="gpu")
    sa_msg, gpu_msg = sa.value.errors()[0]["msg"], gpu.value.errors()[0]["msg"]
    assert "lowercase letters" in sa_msg and "(?:" not in sa_msg
    assert "<domain>/<name>" in gpu_msg and "(?:" not in gpu_msg


def test_a_gpu_resource_name_domain_is_at_most_253_characters():
    domain = ".".join(["a" * 63] * 3 + ["a" * 61])
    assert len(domain) == 253
    assert K8sGpu(count=1, resource_name=f"{domain}/gpu").resource_name == f"{domain}/gpu"
    with pytest.raises(ValidationError):
        K8sGpu(count=1, resource_name=f"{domain}a/gpu")


def test_the_pull_policy_takes_each_of_the_three_values():
    for policy in ("Always", "IfNotPresent", "Never"):
        assert KubernetesTemplateConfig(image="rt", image_pull_policy=policy).image_pull_policy == policy


def test_a_template_with_every_new_field_round_trips():
    backend = {
        "kind": "kubernetes", "image": "rt:1", "node_selector": {"gpu": "true"},
        "tolerations": [{"key": "gpu", "operator": "Exists", "value": None, "effect": "NoSchedule"}],
        "gpu": {"count": 2, "resource_name": "amd.com/gpu"}, "service_account_name": "runner", "image_pull_policy": "Never",
    }
    once = WorkspaceTemplate.model_validate({"id": "t", "provider_id": "k", "description": "d", "backend": backend})
    twice = WorkspaceTemplate.model_validate(once.model_dump(mode="json"))
    assert twice == once
    assert twice.backend.tolerations == [K8sToleration(key="gpu", operator="Exists", effect="NoSchedule")]
    assert twice.backend.gpu == K8sGpu(count=2, resource_name="amd.com/gpu")


def test_the_ignored_fields_say_so():
    for model, name in [(KubernetesTemplateConfig, n) for n in ("cpu_request", "cpu_limit", "memory_request",
                        "memory_limit", "network_policy_name")] + [(ContainerTemplateConfig, "entrypoint"),
                        (ContainerTemplateConfig, "network"), (ResourceLimits, "network"), (ResourceLimits, "disk_bytes")]:
        assert model.model_fields[name].description.startswith("Ignored"), (model.__name__, name)


def test_the_unapplied_security_context_says_any_value_is_refused():
    description = KubernetesTemplateConfig.model_fields["container_security_context_overrides"].description
    assert description == "Not applied; any non-empty value is refused."


def test_the_size_fields_say_which_provider_reads_them():
    for name in ("cpu_cores", "memory_bytes"):
        description = ResourceLimits.model_fields[name].description
        assert "Kubernetes requests and limits" in description, name
        assert "Docker reads backend.cpu_cores / backend.memory_bytes" in description, name
        assert "Local ignores it" in description, name


def test_the_new_fields_say_who_uses_them_and_who_may_write_them():
    fields = KubernetesTemplateConfig.model_fields
    for name in ("node_selector", "tolerations", "gpu", "service_account_name", "image_pull_policy"):
        assert "Kubernetes" in fields[name].description, name
    for name in ("tolerations", "service_account_name"):
        assert "Admin only" in fields[name].description, name
    for name in ("node_selector", "image_pull_policy"):
        assert "Admin only" not in fields[name].description, name


def test_the_gpu_says_the_count_is_user_tier_and_another_resource_name_is_admin_only():
    """Lead ruling 15: a user may use only the default resource name; any other is admin-only. (The pin that said `gpu` has no
    admin-only part moved here with it.)"""
    gpu = KubernetesTemplateConfig.model_fields["gpu"].description
    assert "Any user may set the count" in gpu and DEFAULT_GPU_RESOURCE_NAME in gpu and "admin-only" in gpu
    name = K8sGpu.model_fields["resource_name"].description
    assert "Admin only" in name and DEFAULT_GPU_RESOURCE_NAME in name


def test_the_patterns_match_what_kubernetes_accepts():
    import re

    assert re.fullmatch(K8S_LABEL_KEY_PATTERN, "kubernetes.io/arch") and re.fullmatch(K8S_LABEL_KEY_PATTERN, "gpu")
    assert not re.fullmatch(K8S_LABEL_KEY_PATTERN, "a/b/c") and not re.fullmatch(K8S_LABEL_KEY_PATTERN, "gpu\n")
    assert re.fullmatch(K8S_LABEL_VALUE_PATTERN, "") and re.fullmatch(K8S_LABEL_VALUE_PATTERN, "a_b.c-d")
    assert not re.fullmatch(K8S_LABEL_VALUE_PATTERN, "a b") and not re.fullmatch(K8S_LABEL_VALUE_PATTERN, "_a")
    assert re.fullmatch(K8S_RESOURCE_NAME_PATTERN, "nvidia.com/gpu") and not re.fullmatch(K8S_RESOURCE_NAME_PATTERN, "gpu")
    assert re.fullmatch(K8S_SERVICE_ACCOUNT_PATTERN, "primer-runner") and not re.fullmatch(K8S_SERVICE_ACCOUNT_PATTERN, "Runner")


def test_the_new_names_are_exported():
    for name in ("DEFAULT_GPU_RESOURCE_NAME", "DOCKER_RUNTIME_IMAGE", "K8S_LABEL_KEY_PATTERN", "K8S_LABEL_VALUE_PATTERN", "K8S_RESOURCE_NAME_PATTERN",
                 "K8S_SERVICE_ACCOUNT_PATTERN", "K8sToleration", "K8sGpu", "WorkspaceResourceOverrides"):
        assert name in workspace_models.__all__, name
