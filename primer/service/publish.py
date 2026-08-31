"""Persisting a validated bundle as an immutable ServiceVersion.

The router validates (via :func:`primer.service.bundle.validate_bundle`
plus storage-dependent checks like toolset grants); this module persists:
artifact rows for every file, the version row, the active pointer, and
retention pruning. Kept separate from the router so the
publish_service tool reuses it verbatim.
"""

from __future__ import annotations

import mimetypes

from primer.int.artifact_storage import ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.service import Service, ServiceVersion
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value
from primer.service.bundle import ValidatedBundle

RETAINED_VERSIONS = 20
"""Newest versions kept per service; the active version is always kept."""


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
    stores. Activation is a pointer swap on the service row; retention
    prunes to the newest :data:`RETAINED_VERSIONS`, never touching the
    active version.
    """
    existing = await _all_versions(sp, service.id)
    next_number = max((v.version for v in existing), default=0) + 1

    files: dict[str, str] = {}
    for path, data in bundle.files.items():
        files[path] = await artifacts.put(
            data=data, mime_type=_mime_for(path), filename=path
        )

    version = ServiceVersion(
        service_id=service.id,
        version=next_number,
        manifest=bundle.manifest,
        files=files,
        functions=bundle.functions,
    )
    created = await sp.get_storage(ServiceVersion).create(version)

    if activate:
        service.active_version_id = created.id
        await sp.get_storage(Service).update(service)

    await _prune(sp, artifacts, service=service, versions=existing + [created])
    return created


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
