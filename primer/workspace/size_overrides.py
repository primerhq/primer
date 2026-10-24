"""Which workspace provider can honour which per-workspace size, and the 422 for one it cannot (platform revamp 2026-10,
plan 08 task 4; master Review Focus 5).

``WorkspaceTemplateOverrides.resources`` sizes ONE workspace (``cpu_cores``, ``memory_bytes``, ``disk_bytes``) on top of its
template. Not every provider can do that:

* **Kubernetes** takes all three: CPU and memory become the pod's requests and limits, the disk becomes the PVC request.
* **Docker** takes CPU and memory (``NanoCpus`` and ``Memory`` on the container), but its adapter has no disk quota, so a
  disk size would be accepted and then do nothing.
* **Local** runs on the server itself, in the provider's root folder; none of the three means anything there.

A size the provider cannot honour is REFUSED, never silently ignored: a person who asked for 20 GiB of disk and got a
workspace without it would not find out until the disk filled. The refusal is a 422 whose ``extensions.errors`` names each
field the way the request body spells it (``body.overrides.resources.disk_bytes``), so the console can put it under the field
and a client can read it like any other validation error. :func:`refuse_unsupported_resource_overrides` runs in
``WorkspaceRegistry.materialise``, the one place every create path passes (``POST /v1/workspaces``, the ``create_workspace``
tool, the bootstrap seed), and BEFORE the backend is asked to build anything, so a refused create leaves nothing behind.

**Sane maxima** (lead ruling 17, from the review of #782). 1e308 cores, 10**30 bytes and 10**30 GPUs used to be accepted and to
come back as a generic 500 after the create had been rolled back, when the cluster refused the manifest. The caps are
``MAX_WORKSPACE_*`` and ``MAX_GPU_COUNT`` in ``primer.model.workspace``, re-exported here: an override above one is a 422 from
the model (``loc`` ``body.overrides.resources.<field>``, so the console shows it under the field), and so is a GPU count above
the cap. The template's own CPU and memory are older fields, which cannot take a cap on the model without making a stored row
fail to load, so :func:`refuse_oversized_template_sizes` checks the size the workspace will really get (the override's, else
the template's) at create time instead.

**The template's own disk, and sizes of zero or less** (the G5 audit). A Kubernetes template's ``backend.pvc_size`` was free text
that no door checked: ``10 Gi``, ``lots``, ``-5Gi`` and ``1000Pi`` were stored and handed to the cluster. The model now refuses a
``pvc_size`` that is not a Kubernetes quantity, is not more than zero or is above 64 TiB, and a template CPU or memory of zero or
less (``gt=0``), on every write. A row stored before those rules still loads (the storage reads rows back with
``STORAGE_LOAD_CONTEXT``, and these validators let a stored value through: read path tolerant, write path strict), so the create
checks the same three things on the size the workspace will really get: :func:`refuse_oversized_template_sizes` refuses a
template disk that is not a valid quantity of at most 64 TiB (unless ``overrides.resources.disk_bytes`` replaces it) and a
template CPU or memory that is not more than zero, as it refuses one above the cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from primer.model.except_ import ConfigError, ValidationError
from primer.model.workspace import (
    MAX_GPU_COUNT,
    MAX_WORKSPACE_CPU_CORES,
    MAX_WORKSPACE_DISK_BYTES,
    MAX_WORKSPACE_MEMORY_BYTES,
    ContainerTemplateConfig,
    KubernetesTemplateConfig,
    WorkspaceResourceOverrides,
    disk_quantity_problem,
)

if TYPE_CHECKING:
    from primer.model.workspace import WorkspaceTemplate, WorkspaceTemplateOverrides

#: The size fields of ``overrides.resources``, in the model's order (a refusal lists its fields in this order).
SIZE_FIELDS: tuple[str, ...] = tuple(WorkspaceResourceOverrides.model_fields)

#: Per provider kind (``WorkspaceProviderType`` values), the size fields it cannot honour. A kind missing here has no rule and a
#: size asked of it fails loudly (``tests/workspace/test_size_overrides.py`` pins that every kind of the model has a row).
UNSUPPORTED_RESOURCE_OVERRIDES: dict[str, frozenset[str]] = {
    "local": frozenset({"cpu_cores", "memory_bytes", "disk_bytes"}),
    "container": frozenset({"disk_bytes"}),
    "kubernetes": frozenset(),
}

#: The sentence each refusing kind gives for every field it refuses (shown to a person, so it says what to do).
_SENTENCES: dict[str, str] = {
    "local": "A Local workspace runs on the server itself and cannot be sized. Remove this override.",
    "container": "Docker cannot limit a workspace's disk. Remove this override.",
}


class ResourceOverrideRefused(ValidationError):
    """The 422 for a size override the workspace's provider cannot honour.

    ``fields`` are the refused size fields in the model's order; ``message`` reads
    ``overrides.resources.<field>[, overrides.resources.<field>]: <sentence>`` and ``problem_extensions`` carries one
    ``errors`` entry per field with the ``loc`` the request body gives it (type ``unsupported_by_provider``).
    """

    def __init__(self, provider_kind: str, fields: list[str]) -> None:
        self.provider_kind = provider_kind
        self.fields = list(fields)
        self._sentence = _SENTENCES.get(
            provider_kind, f"A {provider_kind} workspace cannot take this size. Remove this override.",
        )
        names = ", ".join(f"overrides.resources.{field}" for field in self.fields)
        super().__init__(f"{names}: {self._sentence}")

    @property
    def problem_extensions(self) -> dict[str, Any]:
        return {
            "errors": [
                {
                    "loc": ["body", "overrides", "resources", field],
                    "msg": self._sentence,
                    "type": "unsupported_by_provider",
                }
                for field in self.fields
            ],
        }


def asks_for_a_size(overrides: "WorkspaceTemplateOverrides | None") -> bool:
    """Whether ``overrides`` sets at least one size (``resources`` absent, null or ``{}`` sets none)."""
    resources = overrides.resources if overrides is not None else None
    return resources is not None and any(getattr(resources, field) is not None for field in SIZE_FIELDS)


def refuse_unsupported_resource_overrides(provider_kind: str, overrides: "WorkspaceTemplateOverrides | None") -> None:
    """Raise :class:`ResourceOverrideRefused` naming every size field of ``overrides`` that a ``provider_kind`` provider cannot honour.

    Returns normally when no size is asked for, or when the provider honours every size asked for. A ``provider_kind`` with
    no rule raises :class:`ConfigError` (503) when a size is asked of it: a provider kind added to the model without a
    decision here must not let a size through silently.
    """
    if not asks_for_a_size(overrides):
        return
    unsupported = UNSUPPORTED_RESOURCE_OVERRIDES.get(provider_kind)
    if unsupported is None:
        raise ConfigError(
            f"no size-override rule for workspace provider kind {provider_kind!r}; "
            "add it to primer.workspace.size_overrides.UNSUPPORTED_RESOURCE_OVERRIDES"
        )
    assert overrides is not None and overrides.resources is not None
    refused = [
        field for field in SIZE_FIELDS
        if field in unsupported and getattr(overrides.resources, field) is not None
    ]
    if refused:
        raise ResourceOverrideRefused(provider_kind, refused)


_TIB = 2**40


class TemplateSizeRefused(ValidationError):
    """The 422 for a size the TEMPLATE sets that a workspace may not get (above the cap, not more than zero, or a disk that is not a
    Kubernetes quantity), and that no override replaces.

    ``fields`` are the template fields at fault (``resources.cpu_cores`` ... and ``backend.pvc_size`` on Kubernetes,
    ``backend.cpu_cores`` ... on Docker). The value is stored in the template, not sent in this request, so there is no ``loc`` to
    give: the message names the template and the field, and the console shows it in the line above the dialog's footer.
    ``refused`` holds ``(field, value, why)``, ``why`` a phrase such as ``more than the 1024 CPU cores a workspace may have``.
    """

    def __init__(self, template_id: str, refused: list[tuple[str, Any, str]]) -> None:
        self.template_id = template_id
        self.fields = [field for field, _value, _why in refused]
        sets = " and ".join(f"{field} to {value!r} ({why})" for field, value, why in refused)
        super().__init__(f"template {template_id!r} sets {sets}. Edit the template.")

    @property
    def problem_extensions(self) -> dict[str, Any]:
        return {}


def refuse_oversized_template_sizes(template: "WorkspaceTemplate", overrides: "WorkspaceTemplateOverrides | None") -> None:
    """Raise :class:`TemplateSizeRefused` when a size this workspace will get from its TEMPLATE is one no workspace may have.

    Only the size that really applies is looked at: a field the per-workspace override sets replaces the template's, so whatever
    the template says there is not what runs. Kubernetes reads ``resources.cpu_cores`` / ``resources.memory_bytes`` and the disk
    ``backend.pvc_size``, Docker ``backend.cpu_cores`` / ``backend.memory_bytes``; a Local template's size is ignored, so it is not
    checked. A CPU or memory is refused above the cap (a value that is not a number at all, ``nan``, is above any cap) and when it
    is not more than zero; the disk is refused when it is not a Kubernetes quantity, not more than zero or above 64 TiB. A
    template written since the G5 audit cannot hold any of these (the model refuses them), so what this catches below the caps is
    a row stored before (the model loads it as it is).
    """
    backend = template.backend
    disk: tuple[str, str] | None = None
    if isinstance(backend, KubernetesTemplateConfig):
        cpu = ("resources.cpu_cores", template.resources.cpu_cores)
        memory = ("resources.memory_bytes", template.resources.memory_bytes)
        disk = ("backend.pvc_size", backend.pvc_size)
    elif isinstance(backend, ContainerTemplateConfig):
        cpu = ("backend.cpu_cores", backend.cpu_cores)
        memory = ("backend.memory_bytes", backend.memory_bytes)
    else:
        return
    resources = overrides.resources if overrides is not None else None
    replaced = {field for field in SIZE_FIELDS if resources is not None and getattr(resources, field) is not None}
    refused: list[tuple[str, Any, str]] = []
    for (name, value), field, cap, limit in (
        (cpu, "cpu_cores", MAX_WORKSPACE_CPU_CORES, f"{MAX_WORKSPACE_CPU_CORES} CPU cores"),
        (memory, "memory_bytes", MAX_WORKSPACE_MEMORY_BYTES, f"{MAX_WORKSPACE_MEMORY_BYTES // _TIB} TiB of memory"),
    ):
        if field in replaced or value is None:
            continue
        if not value <= cap:     # written so that nan, which compares false with everything, is refused
            refused.append((name, value, f"more than the {limit} a workspace may have"))
        elif not value > 0:
            refused.append((name, value, "not more than zero"))
    if disk is not None and "disk_bytes" not in replaced:
        name, quantity = disk
        problem = disk_quantity_problem(quantity)
        if problem is not None:
            refused.append((name, quantity, problem))
    if refused:
        raise TemplateSizeRefused(template.id, refused)


__all__ = [
    "MAX_GPU_COUNT",
    "MAX_WORKSPACE_CPU_CORES",
    "MAX_WORKSPACE_DISK_BYTES",
    "MAX_WORKSPACE_MEMORY_BYTES",
    "SIZE_FIELDS",
    "UNSUPPORTED_RESOURCE_OVERRIDES",
    "ResourceOverrideRefused",
    "TemplateSizeRefused",
    "asks_for_a_size",
    "refuse_oversized_template_sizes",
    "refuse_unsupported_resource_overrides",
]
