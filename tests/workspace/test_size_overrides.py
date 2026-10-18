"""Which workspace provider can honour which per-workspace size, and the 422 for one it cannot (platform revamp 2026-10,
plan 08 task 4; master Review Focus 5).

``overrides.resources`` sizes one workspace (``cpu_cores``, ``memory_bytes``, ``disk_bytes``). Kubernetes takes all three. Docker
takes CPU and memory but has no disk quota. A Local workspace runs on the server itself and can be sized by none. A size the
provider cannot honour is refused with a 422 that names the field, never silently ignored.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import ValidationError
from primer.model.workspace import (
    ContainerTemplateConfig,
    KubernetesTemplateConfig,
    ResourceLimits,
    WorkspaceProviderType,
    WorkspaceResourceOverrides,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace import size_overrides
from primer.workspace.size_overrides import (
    MAX_WORKSPACE_CPU_CORES,
    MAX_WORKSPACE_MEMORY_BYTES,
    UNSUPPORTED_RESOURCE_OVERRIDES,
    ResourceOverrideRefused,
    TemplateSizeRefused,
    refuse_oversized_template_sizes,
    refuse_unsupported_resource_overrides,
)

_ALL = WorkspaceResourceOverrides(cpu_cores=2, memory_bytes=2**30, disk_bytes=10 * 2**30)
_DOCKER_DISK = "Docker cannot limit a workspace's disk. Remove this override."
_LOCAL = "A Local workspace runs on the server itself and cannot be sized. Remove this override."


def _o(**sizes) -> WorkspaceTemplateOverrides:
    return WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(**sizes))


def test_kubernetes_honours_every_size_field():
    refuse_unsupported_resource_overrides("kubernetes", WorkspaceTemplateOverrides(resources=_ALL))   # does not raise


def test_docker_refuses_disk_only():
    with pytest.raises(ResourceOverrideRefused) as exc:
        refuse_unsupported_resource_overrides("container", WorkspaceTemplateOverrides(resources=_ALL))
    assert exc.value.fields == ["disk_bytes"]
    assert exc.value.provider_kind == "container"
    refuse_unsupported_resource_overrides("container", _o(cpu_cores=1.5))                     # cpu alone passes
    refuse_unsupported_resource_overrides("container", _o(memory_bytes=2**31))                # memory alone passes
    refuse_unsupported_resource_overrides("container", _o(cpu_cores=1, memory_bytes=2**30))   # both pass


def test_local_refuses_every_size_field():
    everything = ["cpu_cores", "memory_bytes", "disk_bytes"]
    with pytest.raises(ResourceOverrideRefused) as exc:
        refuse_unsupported_resource_overrides("local", WorkspaceTemplateOverrides(resources=_ALL))
    assert exc.value.fields == everything
    for field in everything:
        value = {"cpu_cores": 1, "memory_bytes": 2**30, "disk_bytes": 2**30}[field]
        with pytest.raises(ResourceOverrideRefused) as one:
            refuse_unsupported_resource_overrides("local", _o(**{field: value}))
        assert one.value.fields == [field]


def test_the_fields_come_in_the_models_order_whatever_order_they_were_given():
    with pytest.raises(ResourceOverrideRefused) as exc:
        refuse_unsupported_resource_overrides("local", _o(disk_bytes=2**30, cpu_cores=1))
    assert exc.value.fields == ["cpu_cores", "disk_bytes"]


@pytest.mark.parametrize("kind", sorted(UNSUPPORTED_RESOURCE_OVERRIDES))
def test_no_resources_is_never_refused(kind):
    refuse_unsupported_resource_overrides(kind, None)
    refuse_unsupported_resource_overrides(kind, WorkspaceTemplateOverrides())
    refuse_unsupported_resource_overrides(kind, WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides()))   # {} sets nothing


def test_the_refusal_is_a_422_with_one_error_per_field():
    with pytest.raises(ValidationError) as exc:
        refuse_unsupported_resource_overrides("container", WorkspaceTemplateOverrides(resources=_ALL))
    assert isinstance(exc.value, ResourceOverrideRefused)
    assert exc.value.problem_extensions == {"errors": [{
        "loc": ["body", "overrides", "resources", "disk_bytes"], "msg": _DOCKER_DISK, "type": "unsupported_by_provider",
    }]}
    assert exc.value.message == f"overrides.resources.disk_bytes: {_DOCKER_DISK}"

    with pytest.raises(ResourceOverrideRefused) as local:
        refuse_unsupported_resource_overrides("local", WorkspaceTemplateOverrides(resources=_ALL))
    assert [(e["loc"][-1], e["msg"], e["type"]) for e in local.value.problem_extensions["errors"]] == [
        ("cpu_cores", _LOCAL, "unsupported_by_provider"),
        ("memory_bytes", _LOCAL, "unsupported_by_provider"),
        ("disk_bytes", _LOCAL, "unsupported_by_provider"),
    ]
    assert local.value.message == (
        "overrides.resources.cpu_cores, overrides.resources.memory_bytes, overrides.resources.disk_bytes: " + _LOCAL
    )


def test_the_rules_cover_every_provider_kind_the_model_has():
    """A provider kind added to the model without a row here would let a size through silently."""
    assert set(UNSUPPORTED_RESOURCE_OVERRIDES) == {t.value for t in WorkspaceProviderType}


def test_the_rules_only_name_fields_the_override_model_has():
    named = set().union(*UNSUPPORTED_RESOURCE_OVERRIDES.values())
    assert named <= set(WorkspaceResourceOverrides.model_fields)
    assert size_overrides.SIZE_FIELDS == tuple(WorkspaceResourceOverrides.model_fields)


def test_a_provider_kind_with_no_rule_fails_loudly_instead_of_ignoring_the_size():
    from primer.model.except_ import ConfigError

    with pytest.raises(ConfigError, match="mystery"):
        refuse_unsupported_resource_overrides("mystery", _o(cpu_cores=1))
    refuse_unsupported_resource_overrides("mystery", None)   # nothing asked for, nothing to refuse


# ---- lead ruling 17 of plan 08 (from the review of #782): an absurd size the TEMPLATE sets ---------------------------------
#
# The override fields carry their caps in the model (a 422 with the field in ``loc``). A template's own CPU and memory are older
# fields, so a cap cannot go on the model without making a stored row fail to load; instead the size the workspace will really
# get (the override's, else the template's) is checked at create time, before any backend runs. A template that sets 1e308
# cores came back as a generic 500 after a rolled-back create.


def _k8s(cpu=None, memory=None) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="big", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        resources=ResourceLimits(cpu_cores=cpu, memory_bytes=memory),
    )


def _docker(cpu=None, memory=None) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="dock", provider_id="dock", description="d", backend=ContainerTemplateConfig(cpu_cores=cpu, memory_bytes=memory),
    )


def test_the_caps_are_re_exported_from_the_size_module():
    assert (MAX_WORKSPACE_CPU_CORES, MAX_WORKSPACE_MEMORY_BYTES) == (1024, 64 * 2**40)


def test_a_template_within_the_caps_is_never_refused():
    refuse_oversized_template_sizes(_k8s(), None)
    refuse_oversized_template_sizes(_k8s(cpu=1024, memory=64 * 2**40), None)             # the caps themselves are allowed
    refuse_oversized_template_sizes(_docker(cpu=0.5, memory=2**30), WorkspaceTemplateOverrides())
    refuse_oversized_template_sizes(WorkspaceTemplate(id="loc", provider_id="loc", description="d"), None)


@pytest.mark.parametrize("cpu", [1024.5, 1e308, float("inf"), float("nan")])
def test_a_kubernetes_template_with_an_absurd_cpu_is_refused_naming_the_field(cpu):
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_k8s(cpu=cpu), None)
    assert isinstance(exc.value, ValidationError)
    assert exc.value.fields == ["resources.cpu_cores"] and exc.value.template_id == "big"
    assert "template 'big'" in exc.value.message and "resources.cpu_cores" in exc.value.message
    assert "1024 CPU cores" in exc.value.message and "Edit the template" in exc.value.message
    assert exc.value.problem_extensions == {}      # the value is in the template, not in the request: no `loc` to give


@pytest.mark.parametrize("memory", [64 * 2**40 + 1, 10**30])
def test_a_kubernetes_template_with_an_absurd_memory_is_refused(memory):
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_k8s(memory=memory), None)
    assert exc.value.fields == ["resources.memory_bytes"]
    assert "64 TiB" in exc.value.message


def test_a_docker_template_names_the_backend_fields():
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_docker(cpu=1e308, memory=10**30), None)
    assert exc.value.fields == ["backend.cpu_cores", "backend.memory_bytes"]
    assert "backend.cpu_cores" in exc.value.message and "backend.memory_bytes" in exc.value.message


def test_an_override_that_replaces_the_absurd_field_makes_it_irrelevant():
    """The workspace gets the override's size, so what the template says in that field is not what runs."""
    over = WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(cpu_cores=2))

    refuse_oversized_template_sizes(_k8s(cpu=1e308), over)
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_k8s(cpu=1e308, memory=10**30), over)      # memory is still the template's
    assert exc.value.fields == ["resources.memory_bytes"]


def test_a_local_template_is_not_checked_because_local_ignores_its_size():
    local = WorkspaceTemplate(id="loc", provider_id="loc", description="d", resources=ResourceLimits(cpu_cores=1e308))

    refuse_oversized_template_sizes(local, None)


def test_a_template_size_the_caps_leave_alone_but_the_backend_may_not_like_is_not_this_checks_business():
    """Only the upper bound is the ruling's; zero and negative numbers keep whatever the backend made of them before."""
    refuse_oversized_template_sizes(_k8s(cpu=-1, memory=0), None)
    refuse_oversized_template_sizes(_docker(cpu=0), None)
