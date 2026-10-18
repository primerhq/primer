"""Which workspace provider can honour which per-workspace size, and the 422 for one it cannot (platform revamp 2026-10,
plan 08 task 4; master Review Focus 5).

``overrides.resources`` sizes one workspace (``cpu_cores``, ``memory_bytes``, ``disk_bytes``). Kubernetes takes all three. Docker
takes CPU and memory but has no disk quota. A Local workspace runs on the server itself and can be sized by none. A size the
provider cannot honour is refused with a 422 that names the field, never silently ignored.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import ValidationError
from primer.model.workspace import WorkspaceProviderType, WorkspaceResourceOverrides, WorkspaceTemplateOverrides
from primer.workspace import size_overrides
from primer.workspace.size_overrides import (
    UNSUPPORTED_RESOURCE_OVERRIDES,
    ResourceOverrideRefused,
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
