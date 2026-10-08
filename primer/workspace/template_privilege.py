"""Which workspace template fields only an admin may write (security review 2026-10-08: AUTHZ-02, INJ-02, INJ-05,
AUTHZ-05, SSRF-06).

Templates are user-tier, but a few fields reach the host or the cluster, or read operator secrets:

* container ``extra_mounts`` (a host bind mount);
* Kubernetes ``extra_volumes``, ``extra_volume_mounts``, ``pod_overrides`` and ``container_security_context_overrides``;
* a ``kind=secret`` file source (it reads a ``PRIMER_SECRET_*`` value by name), on a template or in the per-workspace
  overrides.

A non-admin write that sets one of them to a non-empty value, or changes it from the stored value, is refused. The REST
routes answer 403 ``forbidden_role``; the ``workspaces`` tools answer ``type=forbidden``. A template that HOLDS any of
them is not user-editable at all (keeping the admin's mount but swapping the image would run user code with it); an
admin-free template stays fully user-editable. Deleting a template that holds them is admin-only too.

What the held rule does NOT do: it stops a non-admin from REWRITING (or deleting) an admin-held template, not from
creating a workspace FROM it. A user may still instantiate such a template, with their own user-tier
``init_commands`` overrides, and run code alongside its mounts and secret files. So an admin must not put host-privileged
mounts or secrets in a template that ordinary users may instantiate. ``init_commands`` are NOT gated: on the local backend they run in a host shell, but a role=user caller
already runs any command in that same shell through a workspace session's exec tool, so gating them closes nothing.

The Kubernetes overlays are also checked against an allowlist for every caller (``primer.workspace.k8s.backend``).
"""

from __future__ import annotations

from typing import Any

from primer.model.workspace import (
    ContainerTemplateConfig,
    FileMount,
    KubernetesTemplateConfig,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)

_K8S_FIELDS = ("extra_volumes", "extra_volume_mounts", "pod_overrides", "container_security_context_overrides")


def _secret_sources(files: list[FileMount]) -> list[tuple[str, str, str | None]]:
    return sorted(
        (fm.path, fm.source.name, fm.mode) for fm in files if getattr(fm.source, "kind", None) == "secret"
    )


def _gated_values(template: WorkspaceTemplate | None) -> dict[str, Any]:
    """The admin-only fields of ``template``, each normalised so an absent or empty value compares equal to ``None``."""
    out: dict[str, Any] = {"backend.extra_mounts": None, "files.secret": None}
    out.update({f"backend.{f}": None for f in _K8S_FIELDS})
    if template is None:
        return out
    backend = template.backend
    if isinstance(backend, ContainerTemplateConfig):
        out["backend.extra_mounts"] = [m.model_dump(mode="json") for m in backend.extra_mounts] or None
    elif isinstance(backend, KubernetesTemplateConfig):
        for f in _K8S_FIELDS:
            value = getattr(backend, f)
            if isinstance(value, list):
                value = [v.model_dump(mode="json") for v in value]
            out[f"backend.{f}"] = value or None
    out["files.secret"] = _secret_sources(template.files) or None
    return out


def admin_only_template_changes(new: WorkspaceTemplate, existing: WorkspaceTemplate | None) -> list[str]:
    """The admin-only fields ``new`` sets or changes relative to ``existing`` (``None`` on create)."""
    before = _gated_values(existing)
    after = _gated_values(new)
    return [field for field, value in after.items() if value != before[field]]


def admin_only_settings_held(template: WorkspaceTemplate | None) -> list[str]:
    """The admin-only fields ``template`` holds (non-empty). A non-admin may not update such a template at all: keeping
    the admin's mount or secret but swapping the image, entrypoint, user or init_commands runs the user's code with it."""
    return [field for field, value in _gated_values(template).items() if value is not None]


def held_refusal_message(fields: list[str], action: str = "update") -> str:
    return (
        f"this template holds admin-only settings ({', '.join(fields)}); only an admin may {action} it"
    )


def admin_only_override_fields(overrides: WorkspaceTemplateOverrides | None) -> list[str]:
    """The admin-only fields a per-workspace override sets (today: a secret file source)."""
    if overrides is not None and _secret_sources(overrides.files):
        return ["overrides.files.secret"]
    return []


def refusal_message(fields: list[str]) -> str:
    return (
        f"only an admin may set or change {', '.join(fields)} (host mounts, Kubernetes overlays and secret file "
        "sources reach the host, the cluster or operator secrets)"
    )


__all__ = [
    "admin_only_override_fields",
    "admin_only_settings_held",
    "admin_only_template_changes",
    "held_refusal_message",
    "refusal_message",
]
