"""The routing artifact store: what ``ArtifactStorageRegistry.get_default()`` returns (plan 09, design section 15.2).

Every artifact consumer keeps calling ``get_default()`` and using ``put`` / ``get`` / ``delete``. This store decides, per call, which provider's
backend does the work:

* ``put`` writes to the ACTIVE provider, the one :class:`~primer.model.providers.artifact.ActiveArtifactStorageConfig` names (the reserved
  database provider when no row exists). The setting is read on every put and never cached, so a switch is seen by every process at once.
* A backend whose ids the database cannot serve (``needs_location_record``: filesystem, S3) gets an :class:`~primer.model.artifact.ArtifactLocation`
  record, written AFTER the bytes and BEFORE the id is returned. A database-kind provider writes none: every one of them stores in the same
  ``Artifact`` table, which the fallback reads.
* After the record is written, the provider row is read again. The delete of a provider is refused while it is active or holds a record, but a
  put reads the active setting once, at its start: an upload still in flight when an admin switched away and deleted the provider would
  otherwise record its artifact on a provider that no longer exists, and hand back an id that reads as missing (the G5 audit, G5-03). A put
  that finds the row gone removes its record and its bytes and fails with a 409; retried, it goes to the store that is active now. What is
  left is a window two statements wide: a delete whose check counted the records before this record was written, and whose row delete
  lands after this re-read.
* ``get`` and ``delete`` follow the record, even when the recorded provider is not the active one, so an artifact stays readable after a switch.
* An id with no record is a database artifact and goes to ``fallback``, a DIRECT ``DbArtifactStorage``. It is never a registry lookup: the reserved
  provider row must not be able to break the reading of every old artifact.

The store owns no resources: the provider instances are cached, and closed, by the registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ClassVar

from primer.int.artifact_storage import ArtifactBlob, ArtifactStorage
from primer.model.artifact import ArtifactLocation
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.providers.artifact import (
    ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
    DEFAULT_ARTIFACT_PROVIDER_ID,
    ActiveArtifactStorageConfig,
)
from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value

if TYPE_CHECKING:
    from primer.int.storage import Storage
    from primer.int.storage_provider import StorageProvider
    from primer.model.provider import ArtifactStorageProvider

logger = logging.getLogger(__name__)


class RoutingArtifactStorage(ArtifactStorage):
    """Route puts to the active provider and reads and deletes to the provider recorded for the artifact.

    ``resolve`` turns a provider id into its live backend (``ArtifactStorageRegistry.get_provider``); it raises ``NotFoundError`` for an id with no
    provider row. ``fallback`` is the database backend, used for the reserved default id and for every artifact with no record. ``providers`` is
    the provider rows' storage, read after a record is written to confirm the provider still exists (``resolve`` may answer from a cache).
    """

    # The router writes the records itself. A router wrapped in another router would find its ids by itself, so it never asks for one.
    needs_location_record: ClassVar[bool] = False

    def __init__(
        self,
        *,
        resolve: Callable[[str], Awaitable[ArtifactStorage]],
        fallback: ArtifactStorage,
        locations: "Storage[ArtifactLocation]",
        active_config: "Storage[ActiveArtifactStorageConfig]",
        providers: "Storage[ArtifactStorageProvider]",
        default_provider_id: str = DEFAULT_ARTIFACT_PROVIDER_ID,
    ) -> None:
        self._resolve = resolve
        self._fallback = fallback
        self._locations = locations
        self._providers = providers
        self._active_config = active_config
        self._default_provider_id = default_provider_id

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def active_provider_id(self) -> str:
        """The provider new artifacts go to: the singleton row's choice, else the reserved default. One row read, never cached."""
        row = await self._active_config.get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID)
        return row.provider_id if row is not None else self._default_provider_id

    async def put(self, *, data: bytes, mime_type: str, filename: str | None = None) -> str:
        provider_id = await self.active_provider_id()
        # An active provider that cannot be resolved fails the write. Falling back to the database would put the file somewhere the operator did not pick.
        store = self._fallback if provider_id == self._default_provider_id else await self._resolve(provider_id)
        artifact_id = await store.put(data=data, mime_type=mime_type, filename=filename)
        if store.needs_location_record:
            try:
                await self._locations.create(ArtifactLocation(id=artifact_id, provider_id=provider_id))
                if await self._providers.get(provider_id) is None:
                    raise ConflictError(
                        f"artifact store {provider_id!r} was deleted while this upload was being written, so nothing was stored; "
                        "try again: new uploads go to the active artifact store"
                    )
            except (Exception, asyncio.CancelledError):
                # The id was never returned, so no one can ask for these bytes: remove them rather than leave a file no record points to. A cancel
                # (a client that went away mid-request) lands here too. So may a record INSERT that committed before the call failed (a cancel or a
                # lost connection after the commit): it is removed as well, or the provider would count an artifact it does not hold, and refuse
                # every delete and move with "holds 1 artifact(s)" that no one could clear (the G5 audit, G5-05).
                await self._discard(store, provider_id, artifact_id)
                raise
        return artifact_id

    async def get(self, artifact_id: str) -> ArtifactBlob | None:
        record = await self._locations.get(artifact_id)
        if record is None:
            return await self._fallback.get(artifact_id)
        store = await self._recorded_store(artifact_id, record)
        if store is None:
            return None
        return await store.get(artifact_id)

    async def delete(self, artifact_id: str) -> None:
        record = await self._locations.get(artifact_id)
        if record is None:
            await self._fallback.delete(artifact_id)
            return
        store = await self._recorded_store(artifact_id, record)
        if store is None:
            return
        # The bytes first, then the record: a delete that fails in the store keeps the record, so the artifact is still found and the delete can be retried.
        await store.delete(artifact_id)
        with contextlib.suppress(NotFoundError):
            await self._locations.delete(artifact_id)

    async def _recorded_store(self, artifact_id: str, record: ArtifactLocation) -> ArtifactStorage | None:
        """The backend that holds a recorded artifact, or ``None`` (logged) when its provider row no longer exists."""
        try:
            return await self._resolve(record.provider_id)
        except NotFoundError:
            logger.warning("artifact %s is recorded on provider %r, which does not exist", artifact_id, record.provider_id)
            return None

    async def _discard(self, store: ArtifactStorage, provider_id: str, artifact_id: str) -> None:
        """Undo a put that failed after its bytes were written (the record write failed or was cancelled, or the provider was gone at the
        re-read): its location record, if one was written, and its bytes. The id was never handed out. Best effort, each on its own."""
        try:
            await self._locations.delete(artifact_id)
        except NotFoundError:
            pass                                    # the usual case: the record was never written
        except Exception as exc:  # noqa: BLE001 - best effort: the failure the caller needs to see is the put's own
            logger.warning(
                "the put of artifact %s on provider %r failed, and removing its location record also failed: %s",
                artifact_id, provider_id, exc,
            )
        try:
            await store.delete(artifact_id)
        except Exception as exc:  # noqa: BLE001 - best effort: the failure the caller needs to see is the put's own
            logger.warning(
                "the put of artifact %s on provider %r failed, and removing its bytes also failed: %s",
                artifact_id, provider_id, exc,
            )


async def count_artifacts_held(storage_provider: "StorageProvider", provider_id: str) -> int:
    """Count artifacts held by ``provider_id`` through its hot-field index.

    Database-kind providers write no records, so a ``db`` provider always counts zero (and a spare one can always be deleted).
    """
    held = Predicate(left=FieldRef(name="provider_id"), op=Op.EQ, right=Value(value=provider_id))
    page = await storage_provider.get_storage(ArtifactLocation).find(held, OffsetPage(offset=0, length=1))
    return page.total or 0


__all__ = ["RoutingArtifactStorage", "count_artifacts_held"]
