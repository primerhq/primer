"""Serving-plane plumbing: name resolution, blob cache, path picking.

Spec: ``docs/superpowers/specs/2026-08-08-services-design.md`` section 6.
Everything here is deliberately process-local state: the resolver's TTL
bounds cross-replica staleness after an activate (each replica converges
within RESOLVE_TTL_SECONDS with no coordination), and the artifact LRU
never needs invalidation because bundle files are immutable and
content-addressed by artifact id.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

from fastapi import Request

from primer.int.artifact_storage import ArtifactBlob, ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.service import Service, ServiceVersion
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

RESOLVE_TTL_SECONDS = 5.0
"""How long a name->(service, version) resolution is trusted."""

LRU_MAX_BYTES = 64 * 1024 * 1024
"""Total artifact bytes kept hot in memory per process."""


class ServiceResolver:
    """TTL-cached ``name -> (Service, active ServiceVersion | None)``.

    ``resolve`` returns ``None`` for an unknown name, ``(service, None)``
    for a service with no active version (created but unpublished), and
    ``(service, version)`` when serving. Negative results are cached too:
    a scanner hammering unknown names must not hammer storage.
    """

    def __init__(
        self,
        sp: StorageProvider,
        *,
        ttl_seconds: float = RESOLVE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sp = sp
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[
            str, tuple[float, tuple[Service, ServiceVersion | None] | None]
        ] = {}

    async def resolve(
        self, name: str
    ) -> tuple[Service, ServiceVersion | None] | None:
        cached = self._cache.get(name)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        result: tuple[Service, ServiceVersion | None] | None = None
        page = await self._sp.get_storage(Service).find(
            Predicate(left=FieldRef(name="name"), op=Op.EQ, right=Value(value=name)),
            OffsetPage(offset=0, length=1),
        )
        if page.items:
            service = page.items[0]
            version: ServiceVersion | None = None
            if service.active_version_id is not None:
                version = await self._sp.get_storage(ServiceVersion).get(
                    service.active_version_id
                )
            result = (service, version)
        self._cache[name] = (self._clock() + self._ttl, result)
        return result

    def invalidate(self, name: str) -> None:
        self._cache.pop(name, None)


class ArtifactLRU:
    """Byte-bounded LRU over immutable artifact blobs."""

    def __init__(self, *, max_bytes: int = LRU_MAX_BYTES) -> None:
        self._max = max_bytes
        self._total = 0
        self._entries: OrderedDict[str, ArtifactBlob] = OrderedDict()

    async def get(
        self, artifacts: ArtifactStorage, artifact_id: str
    ) -> ArtifactBlob | None:
        hit = self._entries.get(artifact_id)
        if hit is not None:
            self._entries.move_to_end(artifact_id)
            return hit
        blob = await artifacts.get(artifact_id)
        if blob is None:
            return None
        size = len(blob.data)
        if size <= self._max:
            self._entries[artifact_id] = blob
            self._total += size
            while self._total > self._max:
                _, evicted = self._entries.popitem(last=False)
                self._total -= len(evicted.data)
        return blob


def pick_path(version: ServiceVersion, path: str) -> str | None:
    """Resolve a request path inside a version's file map.

    Empty path -> the entry; exact file hit -> that file; an
    extension-less miss falls back to the entry (SPA client routing);
    a miss WITH an extension is a real 404 (a missing asset must not
    come back as HTML).
    """
    if path in ("", "/"):
        path = version.manifest.entry
    if path in version.files:
        return path
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment and version.manifest.entry in version.files:
        return version.manifest.entry
    return None


def get_resolver(request: Request) -> ServiceResolver:
    resolver = getattr(request.app.state, "svc_resolver", None)
    if resolver is None:
        resolver = ServiceResolver(request.app.state.storage_provider)
        request.app.state.svc_resolver = resolver
    return resolver


def get_artifact_lru(request: Request) -> ArtifactLRU:
    lru = getattr(request.app.state, "svc_artifact_lru", None)
    if lru is None:
        lru = ArtifactLRU()
        request.app.state.svc_artifact_lru = lru
    return lru


def invalidate_service_cache(request: Request, name: str) -> None:
    """In-process resolver invalidation after publish/activate.

    Other replicas converge via the TTL; there is deliberately no
    cross-process bus for this (spec section 6).
    """
    resolver = getattr(request.app.state, "svc_resolver", None)
    if resolver is not None:
        resolver.invalidate(name)


__all__ = [
    "LRU_MAX_BYTES",
    "RESOLVE_TTL_SECONDS",
    "ArtifactLRU",
    "ServiceResolver",
    "get_artifact_lru",
    "get_resolver",
    "invalidate_service_cache",
    "pick_path",
]
