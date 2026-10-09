"""Pre-write checks for ``ArtifactStorageProvider`` rows, shared by the REST route and the system tools (ticket 01a1226f, the D3/D5 shared-validator pattern).

The reserved default (``artifact-storage-default``) is the row ``ArtifactStorageRegistry.get_default()`` reads, and the factory builds only the kinds in
``BUILDABLE_KINDS`` (today ``db``). A write that set the default to any other kind was accepted (an update, or a create while the row was missing), and
afterwards every consumer of the default failed with a ``ConfigError``: channel media on the Telegram, Slack and Discord adapters, ``agent/inform.py``, the
artifact serve route, the workspace routes, ``event_dispatch``, ``yield_runtime`` and ``executor_builders``. Only the DELETE of the reserved row was guarded.

This module must not import ``primer.api``: ``primer.toolset.system`` imports it at module level and ``primer.api`` imports ``primer.toolset.system``. The
default id therefore comes from the leaf ``primer.artifact.factory``.
"""

from __future__ import annotations

from primer.artifact.factory import BUILDABLE_KINDS, DEFAULT_ARTIFACT_PROVIDER_ID
from primer.common.entity_checks import EntityCheckError
from primer.model.provider import ArtifactStorageProvider

_BUILDABLE_LITERALS = ", ".join(f"``{value}``" for value in sorted(kind.value for kind in BUILDABLE_KINDS))

#: Appended to the create and update descriptors of the system ``artifact_storage_provider`` tools: the agent reads the rule before it writes.
ARTIFACT_DEFAULT_WRITE_NOTE = (
    f"The reserved ``{DEFAULT_ARTIFACT_PROVIDER_ID}`` row is the default artifact provider that chat media and every artifact consumer resolve, "
    f"so its ``provider`` must be a kind the server can build (today {_BUILDABLE_LITERALS}): a create or an update of that row that names another "
    "kind returns ``type=validation-error`` naming ``provider``, and nothing is stored. Any other row may name any kind."
)


def check_artifact_provider_write(entity: ArtifactStorageProvider) -> None:
    """Refuse a create or an update that leaves the reserved default naming a kind the factory cannot build.

    Keyed on the row being written: an update's body id equals the stored id (both surfaces refuse a body id that differs from the path before they run
    this), and a create has no stored row. The rule reads the NEW kind only, so a default already switched (by an earlier build, or under the database's
    feet) is repaired by a write that makes it buildable, a missing default can be created only as a kind that builds, and the day a backend ships it is
    allowed without a second edit. Every other row is unrestricted: a spare row that names a kind not built yet breaks only itself.
    """
    if entity.id != DEFAULT_ARTIFACT_PROVIDER_ID or entity.provider in BUILDABLE_KINDS:
        return
    buildable = ", ".join(sorted(kind.value for kind in BUILDABLE_KINDS))
    raise EntityCheckError(
        "validation",
        f"{DEFAULT_ARTIFACT_PROVIDER_ID!r} is the default artifact provider that chat media and every artifact consumer resolve, and the {entity.provider.value!r} "
        f"backend cannot be built yet, so the default cannot be set to it (provider must be one of: {buildable})",
        code="artifact_default_unbuildable",
        field="provider",
    )


__all__ = ["ARTIFACT_DEFAULT_WRITE_NOTE", "check_artifact_provider_write"]
