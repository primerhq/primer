"""Translation helpers for the workspace-stack redesign.

The new ContainerWorkspaceConfig / KubernetesWorkspaceConfig no
longer carry image/entrypoint/resources/storage_class/etc. — those
moved to the template. This module is the documented seam for
legacy → new translation when older deployments upgrade.

For the new shapes both helpers return empty dicts (no template-
flavoured fields to lift). They exist as named hooks the migration
step in a future task can extend.
"""

from __future__ import annotations

from primer.model.workspace import (
    ContainerWorkspaceConfig,
    KubernetesWorkspaceConfig,
)


def container_template_defaults_for_legacy_provider(
    cfg: ContainerWorkspaceConfig,
) -> dict:
    """Return template-shaped defaults derived from a legacy provider config.

    The new ContainerWorkspaceConfig has no template-flavoured fields,
    so this is always empty. Kept as a documented hook for the
    migration step in a future engagement.
    """
    return {}


def k8s_template_defaults_for_legacy_provider(
    cfg: KubernetesWorkspaceConfig,
) -> dict:
    """Same idea as container — k8s provider config now has no
    template-flavoured fields. Returns empty."""
    return {}
