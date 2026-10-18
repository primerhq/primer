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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from primer.model.except_ import ConfigError, ValidationError
from primer.model.workspace import WorkspaceResourceOverrides

if TYPE_CHECKING:
    from primer.model.workspace import WorkspaceTemplateOverrides

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


__all__ = [
    "SIZE_FIELDS",
    "UNSUPPORTED_RESOURCE_OVERRIDES",
    "ResourceOverrideRefused",
    "asks_for_a_size",
    "refuse_unsupported_resource_overrides",
]
