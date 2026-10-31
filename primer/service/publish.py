"""Persisting a validated bundle as an immutable ServiceVersion.

The router validates (via :func:`primer.service.bundle.validate_bundle`
plus storage-dependent checks like toolset grants); this module persists:
artifact rows for every file, the version row, the active pointer, and
retention pruning. Kept separate from the router so the
publish_service tool reuses it verbatim.
"""

from __future__ import annotations

import logging
import mimetypes

from primer.int.artifact_storage import ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConflictError
from primer.model.service import Service, ServiceVersion
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.service.bundle import ValidatedBundle

logger = logging.getLogger(__name__)

RETAINED_VERSIONS = 20
"""Newest versions kept per service; the active version is always kept."""

_CREATE_ATTEMPTS = 3
"""A concurrent publish can steal the computed version number: both read
the same latest version and both target next_number. The unique
(service_id, version) index (KN-19j) makes the loser's create a
ConflictError; re-reading and retrying a few times settles ordinary
races without unbounded spinning. Exhausted attempts re-raise, which the
API renders as a 409 the client can retry."""


def _mime_for(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _by_service(service_id: str) -> Predicate:
    return Predicate(
        left=FieldRef(name="service_id"), op=Op.EQ, right=Value(value=service_id)
    )


async def _all_versions(
    sp: StorageProvider, service_id: str
) -> list[ServiceVersion]:
    storage = sp.get_storage(ServiceVersion)
    out: list[ServiceVersion] = []
    offset = 0
    while True:
        page = await storage.find(
            _by_service(service_id), OffsetPage(offset=offset, length=100)
        )
        out.extend(page.items)
        if len(page.items) < 100:
            return out
        offset += 100


async def publish_version(
    sp: StorageProvider,
    artifacts: ArtifactStorage,
    *,
    service: Service,
    bundle: ValidatedBundle,
    activate: bool = True,
) -> ServiceVersion:
    """Persist ``bundle`` as the service's next version.

    The caller has already validated the bundle; this function only
    stores. Two concurrent publishes may compute the same version
    number; the unique (service_id, version) index makes the loser's
    create a ConflictError, which is retried on a fresh read (KN-19j).
    A failed create deletes the artifacts it put, so a publish never
    leaves orphan artifacts behind. Activation is a pointer swap on
    the service row; retention prunes to the newest
    :data:`RETAINED_VERSIONS`, never touching the active version.
    """
    files: dict[str, str] = {}
    for path, data in bundle.files.items():
        files[path] = await artifacts.put(
            data=data, mime_type=_mime_for(path), filename=path
        )

    try:
        created = await _create_next_version(sp, service, bundle, files)
    except Exception:
        # The version row did not land, so nothing references the
        # artifacts just put: delete them (KN-19j).
        for artifact_id in files.values():
            try:
                await artifacts.delete(artifact_id)
            except Exception:
                logger.warning(
                    "failed to delete an artifact orphaned by a failed publish",
                    extra={"artifact_id": artifact_id, "service_id": service.id},
                )
        raise

    if activate:
        service.active_version_id = created.id
        await sp.get_storage(Service).update(service)

    # Re-read for retention: a concurrent publish may have added a row
    # since this publish read the version list.
    await _prune(
        sp, artifacts, service=service,
        versions=await _all_versions(sp, service.id),
    )
    return created


async def _create_next_version(
    sp: StorageProvider,
    service: Service,
    bundle: ValidatedBundle,
    files: dict[str, str],
) -> ServiceVersion:
    """Create the next ServiceVersion, retrying on a stolen number."""
    storage = sp.get_storage(ServiceVersion)
    conflict: ConflictError | None = None
    for _ in range(_CREATE_ATTEMPTS):
        existing = await _all_versions(sp, service.id)
        next_number = max((v.version for v in existing), default=0) + 1
        version = ServiceVersion(
            service_id=service.id,
            version=next_number,
            manifest=bundle.manifest,
            files=files,
            functions=bundle.functions,
        )
        try:
            return await storage.create(version)
        except ConflictError as exc:
            # A concurrent publish took this number: re-read and take
            # the next one.
            conflict = exc
    # Only reachable when every attempt lost the race; re-raise the
    # conflict (the API renders ConflictError as a 409 the client
    # retries).
    raise conflict or ConflictError(
        f"version create failed for service {service.name!r} after "
        f"{_CREATE_ATTEMPTS} attempts"
    )


async def _prune(
    sp: StorageProvider,
    artifacts: ArtifactStorage,
    *,
    service: Service,
    versions: list[ServiceVersion],
) -> None:
    keep_newest = sorted(versions, key=lambda v: v.version, reverse=True)
    keep = {v.id for v in keep_newest[:RETAINED_VERSIONS]}
    if service.active_version_id is not None:
        keep.add(service.active_version_id)
    storage = sp.get_storage(ServiceVersion)
    for version in versions:
        if version.id in keep:
            continue
        for artifact_id in version.files.values():
            await artifacts.delete(artifact_id)
        await storage.delete(version.id)


async def activate_version(
    sp: StorageProvider, *, service: Service, version_id: str
) -> Service:
    """Repoint the service at ``version_id`` (activate or roll back).

    Raises ``ValueError`` when the version does not exist or belongs to
    a different service; the router renders that as 422 because the
    request body, not the URL, is wrong.
    """
    version = await sp.get_storage(ServiceVersion).get(version_id)
    if version is None or version.service_id != service.id:
        raise ValueError(
            f"version {version_id!r} does not belong to service "
            f"{service.name!r}"
        )
    service.active_version_id = version.id
    await sp.get_storage(Service).update(service)
    return service


__all__ = [
    "RETAINED_VERSIONS",
    "activate_version",
    "publish_version",
]
