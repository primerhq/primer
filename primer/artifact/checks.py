"""Pre-write checks for ``ArtifactStorageProvider`` rows, shared by the REST route and the system tools (ticket 01a1226f, the D3/D5 shared-validator pattern).

The reserved default (``artifact-storage-default``) is always the database store: the routing store reads every artifact without a location record from a direct
``DbArtifactStorage``. New files may go elsewhere through the separate active-store setting, but changing this row's kind would make those older database
artifacts unreadable. The shared write check therefore keeps the row on ``db`` even as more backends become buildable.

This module must not import ``primer.api``: ``primer.toolset.system`` imports it at module level and ``primer.api`` imports ``primer.toolset.system``. The
default id therefore comes from the leaf ``primer.model.providers.artifact`` (``primer.artifact.factory`` re-exports it).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from primer.artifact.filesystem import check_root, prepare_root
from primer.artifact.routing import count_artifacts_held
from primer.common.entity_checks import EntityCheckError
from primer.int.artifact_storage import ArtifactStorage
from primer.model.artifact import ArtifactLocation
from primer.model.except_ import ConfigError
from primer.int.storage_provider import StorageProvider
from primer.model.provider import ArtifactStorageProvider, ArtifactStorageProviderType, S3ArtifactConfig
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.model.providers.artifact import (
    ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
    DEFAULT_ARTIFACT_PROVIDER_ID,
    S3_KEYS_INCOMPLETE_MESSAGE,
    ActiveArtifactStorageConfig,
)

logger = logging.getLogger(__name__)

#: How long the live probe's steps may take together, and how long each best-effort cleanup step may take after a failure. The probe (GX-15: a silent
#: endpoint would otherwise hold the request for the client's retries, minutes for S3) and the update check's verification read (GX-14) share them.
PROBE_TIMEOUT_S = 20.0
CLEANUP_TIMEOUT_S = 5.0

#: Appended to the create and update descriptors of the system ``artifact_storage_provider`` tools: the agent reads the rule before it writes.
ARTIFACT_DEFAULT_WRITE_NOTE = (
    "The reserved ``artifact-storage-default`` row is the database store that every artifact without a location record is read from, so its "
    "``provider`` must stay ``db``: a create or an update of that row that names another kind returns ``type=validation-error`` naming "
    "``provider``, and nothing is stored. To store new files elsewhere, register another provider and select it with "
    "``set_active_artifact_store``. An ``s3`` row takes both ``access_key`` and ``secret_key``, or neither to use the server's own "
    "credentials: one without the other returns ``type=validation-error`` naming the blank key."
)

#: Appended to the delete descriptor of the system ``artifact_storage_provider`` tool: the two refusals of ``check_artifact_provider_delete``,
#: which the agent reads before it acts.
ARTIFACT_DELETE_NOTE = (
    "The delete is refused with ``type=conflict``, and the row stays, while the store is the active artifact store (select another store "
    "with ``set_active_artifact_store`` first) and while it still holds files (the message gives the count: the files are read from it, "
    "so it can be deleted once they are gone). The reserved ``artifact-storage-default`` row cannot be deleted (``type=forbidden``)."
)


def check_artifact_provider_write(entity: ArtifactStorageProvider) -> None:
    """Refuse a create or update that makes the reserved database fallback any other kind, or that gives an S3 row one key without the other.

    The S3 keys go together: both, or neither to use the server's own (ambient) credentials. A half pair is refused
    (``artifact_s3_keys_incomplete``, naming the blank key) rather than completed from the ambient chain. An update runs this AFTER the served
    masks are restored (``preserve_masked_secrets``), so a kept mask counts as the stored key and a blanked box as no key.
    """
    if entity.id == DEFAULT_ARTIFACT_PROVIDER_ID and entity.provider is not ArtifactStorageProviderType.DB:
        raise EntityCheckError(
            "validation",
            f"{DEFAULT_ARTIFACT_PROVIDER_ID!r} is the database store that every artifact without a location record is read from, so its provider must "
            f"stay 'db', not {entity.provider.value!r}; register another provider and select it with PUT /v1/artifact_storage_active_config or "
            "system::set_active_artifact_store",
            code="artifact_default_must_be_db",
            field="provider",
        )
    if entity.provider is ArtifactStorageProviderType.S3:
        missing = entity.config.missing_key()
        if missing is not None:
            raise EntityCheckError("validation", S3_KEYS_INCOMPLETE_MESSAGE, code="artifact_s3_keys_incomplete", field=f"config.{missing}")


async def check_artifact_provider_delete(storage_provider: StorageProvider, provider_id: str) -> None:
    """Refuse deletion of the provider currently selected for new writes."""
    active = await storage_provider.get_storage(ActiveArtifactStorageConfig).get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)
    if active is not None and active.provider_id == provider_id:
        raise EntityCheckError(
            "conflict",
            "currently the active artifact store; select another store first",
            code="cascade_blocked",
            extra={"referenced_by": ACTIVE_ARTIFACT_STORAGE_CONFIG_ID},
        )
    count = await count_artifacts_held(storage_provider, provider_id)
    if count:
        raise EntityCheckError(
            "conflict",
            f"artifact provider {provider_id!r} still holds {count} artifact(s), and they are read from it; it can be deleted once they are gone",
            code="artifact_provider_holds_artifacts",
            extra={"count": count},
        )


def _moved_field(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider) -> str | None:
    """The first location field the edit changes that no verification can allow (the kind, a filesystem root, an S3 bucket or prefix), or ``None``."""
    if entity.provider is not existing.provider:
        return "provider"
    if entity.provider is ArtifactStorageProviderType.FILESYSTEM and entity.config.root != existing.config.root:
        return "config.root"
    if entity.provider is ArtifactStorageProviderType.S3:
        for name in S3ArtifactConfig.LOCATION_FIELDS:
            if name not in S3ArtifactConfig.VERIFIABLE_FIELDS and getattr(entity.config, name) != getattr(existing.config, name):
                return f"config.{name}"
    return None


def _relocated_field(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider) -> str | None:
    """The first S3 endpoint or region change of the edit (the location fields that may still reach the same objects), or ``None``."""
    if entity.provider is ArtifactStorageProviderType.S3 and existing.provider is ArtifactStorageProviderType.S3:
        for name in S3ArtifactConfig.VERIFIABLE_FIELDS:
            if getattr(entity.config, name) != getattr(existing.config, name):
                return f"config.{name}"
    return None


async def _new_settings_reach_the_objects(
    storage_provider: StorageProvider, entity: ArtifactStorageProvider, build: Callable[..., ArtifactStorage] | None,
) -> bool:
    """Build a store from the NEW row and look, by a metadata read, for one artifact the provider holds. False when it is not found or the read
    fails (a dead or unreachable endpoint, refused keys), within ``PROBE_TIMEOUT_S``. The temporary store is closed."""
    from primer.artifact.factory import build_artifact_storage

    held = Predicate(left=FieldRef(name="provider_id"), op=Op.EQ, right=Value(value=entity.id))
    page = await storage_provider.get_storage(ArtifactLocation).find(held, OffsetPage(offset=0, length=1))
    if not page.items:
        return True                                   # nothing to look for: the count raced to zero
    store = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            store = (build or build_artifact_storage)(entity, storage_provider=storage_provider)
            await store.initialize()
            return bool(await store.exists(page.items[0].id))
    except Exception:  # noqa: BLE001 - every failure to read is a failure to reach the objects
        logger.info("artifact provider %r: the new settings could not read a held artifact", entity.id, exc_info=True)
        return False
    finally:
        if store is not None:
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT_S):
                    await store.aclose()
            except Exception:  # noqa: BLE001 - the answer is already decided
                logger.warning("artifact provider %r: closing the verification store failed", entity.id, exc_info=True)


async def check_artifact_provider_update(
    storage_provider: StorageProvider,
    entity: ArtifactStorageProvider,
    existing: ArtifactStorageProvider,
    *,
    build: Callable[..., ArtifactStorage] | None = None,
) -> None:
    """Refuse a location change while stored artifacts still name the row.

    A bucket, prefix, root or kind change is refused outright. An S3 endpoint or region change is refused unless a store built from the NEW settings
    can read one of the artifacts the row holds (GX-14): a renamed host or a corrected region reaches the same objects, a new empty server does not.
    """
    field = _moved_field(entity, existing)
    verify = field is None
    if verify:
        field = _relocated_field(entity, existing)
    if field is None:
        return
    count = await count_artifacts_held(storage_provider, entity.id)
    if not count:
        return
    if verify and await _new_settings_reach_the_objects(storage_provider, entity, build):
        return
    message = (
        f"artifact provider {entity.id!r} holds {count} artifact(s), and the new {field.removeprefix('config.')} cannot see them (a read of one from the "
        "new settings failed); changing it would leave them unreadable. Register a new provider and select it as the active store instead"
        if verify else
        f"artifact provider {entity.id!r} holds {count} artifact(s) at its current location; changing {field} would leave them unreadable. "
        "Register a new provider and select it as the active store instead"
    )
    raise EntityCheckError("conflict", message, code="artifact_provider_holds_artifacts", field=field, extra={"count": count})


def filesystem_root_is_new(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider | None) -> bool:
    """True when ``entity`` names a filesystem root that ``existing`` (the stored row, if any) did not: a create, a kind change or a root change.
    Only a new root is prepared; the root a stored row already names is only checked."""
    if entity.provider is not ArtifactStorageProviderType.FILESYSTEM:
        return False
    return not (
        existing is not None
        and existing.provider is ArtifactStorageProviderType.FILESYSTEM
        and existing.config.root == entity.config.root
    )


async def prepare_artifact_provider(entity: ArtifactStorageProvider, existing: ArtifactStorageProvider | None = None) -> None:
    """Make ready, or check, the location a provider row names before the row is written. Other kinds than filesystem have nothing to do.

    A NEW filesystem root (a create, or an update that changes the root or the kind) is prepared: created when missing, checked for a write and
    given its marker (``primer.artifact.filesystem.prepare_root``). The root a stored row already names is only checked (``check_root``): an
    unchanged re-save handled by a pod without the shared volume must not write the marker into that pod's own empty mount point (review of
    #890, item 5). Either runs in a thread; a refusal is a ``validation`` error ``artifact_root_unusable`` naming ``config.root``.

    The REST route and the system tools run it LAST in their create and update checks, so a write that another check refuses prepares nothing.
    """
    if entity.provider is not ArtifactStorageProviderType.FILESYSTEM:
        return
    step = prepare_root if filesystem_root_is_new(entity, existing) else check_root
    try:
        await asyncio.to_thread(step, entity.config.root)
    except ConfigError as exc:
        raise EntityCheckError("validation", str(exc), code="artifact_root_unusable", field="config.root") from exc


__all__ = [
    "ARTIFACT_DEFAULT_WRITE_NOTE",
    "ARTIFACT_DELETE_NOTE",
    "check_artifact_provider_delete",
    "check_artifact_provider_update",
    "check_artifact_provider_write",
    "filesystem_root_is_new",
    "prepare_artifact_provider",
]
