"""Pre-write checks for ``ArtifactStorageProvider`` rows, shared by the REST route and the system tools (ticket 01a1226f, the D3/D5 shared-validator pattern).

The reserved default (``artifact-storage-default``) is the row ``ArtifactStorageRegistry.get_default()`` reads, and the factory builds only the kinds in
``BUILDABLE_KINDS`` (today ``db``). A write that moved the default to any other kind was accepted, and afterwards every consumer of the default failed with a
``ConfigError``: channel media on the Telegram, Slack and Discord adapters, ``agent/inform.py``, the artifact serve route, the workspace routes,
``event_dispatch``, ``yield_runtime`` and ``executor_builders``. Only the DELETE of the reserved row was guarded.
"""

from __future__ import annotations

from primer.api.registries.artifact_storage_registry import DEFAULT_ARTIFACT_PROVIDER_ID
from primer.artifact.factory import BUILDABLE_KINDS
from primer.common.entity_checks import EntityCheckError
from primer.model.provider import ArtifactStorageProvider


def check_artifact_provider_on_update(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider) -> None:
    """Refuse an update that leaves the reserved default naming a kind the factory cannot build.

    The rule reads the NEW kind, not the change: a default already switched (by an earlier build, or under the database's feet) is repaired by a write that makes
    it buildable, and the day a backend ships it is allowed without a second edit. Every other row is unrestricted: a spare row that names a kind not built yet
    breaks only itself.
    """
    if existing.id != DEFAULT_ARTIFACT_PROVIDER_ID or entity.provider in BUILDABLE_KINDS:
        return
    buildable = ", ".join(sorted(kind.value for kind in BUILDABLE_KINDS))
    raise EntityCheckError(
        "validation",
        f"{DEFAULT_ARTIFACT_PROVIDER_ID!r} is the default artifact provider that chat media and every artifact consumer resolve, and the {entity.provider.value!r} "
        f"backend cannot be built yet, so the default cannot be switched to it (provider must be one of: {buildable})",
        code="artifact_default_unbuildable",
        field="provider",
    )


__all__ = ["check_artifact_provider_on_update"]
