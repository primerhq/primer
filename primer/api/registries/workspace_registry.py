"""Workspace backend registry — caches one backend per ``WorkspaceProvider`` row.

Mirrors the :class:`ProviderRegistry` pattern: lazy lookup, in-memory
cache keyed by row id, ``invalidate`` after row mutation, ``aclose``
on shutdown.

Adds two convenience helpers on top:

* :meth:`get_workspace` — given a ``Workspace`` row, finds the right
  backend (via the row's ``provider_id``) and asks the backend for the
  live :class:`Workspace` handle. Raises ``NotFoundError`` if the
  backend has never instantiated this id (e.g. after process restart
  with an ephemeral backend).
* :meth:`destroy` — same lookup, then ``backend.destroy(...)`` + row
  delete.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from primer.model.except_ import NotFoundError
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import (
    WorkspaceProvider,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace.files import FileResolvers
from primer.workspace.resolvers import (
    make_document_resolver,
    make_secret_resolver,
)


if TYPE_CHECKING:
    from primer.int.secret_provider import SecretProvider
    from primer.int.storage_provider import StorageProvider
    from primer.int.workspace import Workspace, WorkspaceBackend


logger = logging.getLogger(__name__)


def _make_default_factory(
    subprocess_timeout_seconds: float = 120.0,
) -> "Callable[[WorkspaceProvider], WorkspaceBackend]":
    """Return a backend factory that threads ``subprocess_timeout_seconds`` through."""
    def _factory(provider: WorkspaceProvider) -> "WorkspaceBackend":  # pragma: no cover
        from primer.workspace.factory import WorkspaceBackendFactory
        return WorkspaceBackendFactory.create(
            provider,
            subprocess_timeout_seconds=subprocess_timeout_seconds,
        )
    return _factory


# Back-compat shim: kept so existing test code that imports _default_factory
# or passes factory=None still works (zero timeout argument -> default 120s).
def _default_factory(
    provider: WorkspaceProvider,
) -> "WorkspaceBackend":  # pragma: no cover
    """Production dispatch via :class:`WorkspaceBackendFactory`.

    Local import keeps the workspace runtime out of the hot import
    graph until a backend is actually constructed.
    """
    from primer.workspace.factory import WorkspaceBackendFactory

    return WorkspaceBackendFactory.create(provider)


class WorkspaceRegistry:
    """Lazy backend cache keyed by :class:`WorkspaceProvider` row id."""

    def __init__(
        self,
        storage_provider: "StorageProvider",
        *,
        factory: Callable[[WorkspaceProvider], "WorkspaceBackend"] | None = None,
        subprocess_timeout_seconds: float = 120.0,
        secret_provider: "SecretProvider | None" = None,
    ) -> None:
        self._sp = storage_provider
        self._secret_provider = secret_provider
        # If a custom factory is supplied, use it as-is; otherwise build a
        # default factory that captures the configured subprocess timeout.
        self._factory = factory or _make_default_factory(subprocess_timeout_seconds)
        self._cache: dict[str, "WorkspaceBackend"] = {}
        self._lock = asyncio.Lock()

    # ---- backend cache -----------------------------------------------

    async def get_backend(self, provider_id: str) -> "WorkspaceBackend":
        async with self._lock:
            cached = self._cache.get(provider_id)
            if cached is not None:
                return cached
            row = await self._sp.get_storage(WorkspaceProvider).get(provider_id)
            if row is None:
                raise NotFoundError(
                    f"WorkspaceProvider {provider_id!r} does not exist"
                )
            backend = self._factory(row)
            await backend.initialize()
            self._cache[provider_id] = backend
            return backend

    async def invalidate(self, provider_id: str) -> None:
        async with self._lock:
            backend = self._cache.pop(provider_id, None)
        if backend is not None:
            await backend.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            backends = list(self._cache.values())
            self._cache.clear()
        for backend in backends:
            try:
                await backend.aclose()
            except Exception as exc:  # noqa: BLE001 -- best-effort
                logger.warning(
                    "WorkspaceRegistry: aclose failed on %s: %s",
                    type(backend).__name__,
                    exc,
                )

    # ---- per-workspace helpers ---------------------------------------

    async def get_workspace_row(self, workspace_id: str) -> WorkspaceRow:
        """Fetch the persisted ``Workspace`` row; 404 if missing."""
        row = await self._sp.get_storage(WorkspaceRow).get(workspace_id)
        if row is None:
            raise NotFoundError(
                f"Workspace {workspace_id!r} does not exist"
            )
        return row

    async def get_workspace(self, workspace_id: str) -> "Workspace":
        """Resolve the live :class:`Workspace` handle.

        After a process restart the backend may have an empty in-memory
        cache. We load the persisted template and pass it to
        ``backend.get`` so Container/K8s backends can re-attach to the
        long-lived sandbox they materialised on a previous boot.
        """
        row = await self.get_workspace_row(workspace_id)
        backend = await self.get_backend(row.provider_id)
        template = await self._sp.get_storage(WorkspaceTemplate).get(
            row.template_id,
        )
        ws = await backend.get(workspace_id, template=template)
        if ws is None:
            raise NotFoundError(
                f"Workspace {workspace_id!r} row exists but the backend "
                "has no live instance and re-attach failed — the "
                "underlying container/pod may have been destroyed "
                "out-of-band."
            )
        return ws

    async def materialise(
        self,
        *,
        template: WorkspaceTemplate,
        overrides: WorkspaceTemplateOverrides | None = None,
        workspace_id: str | None = None,
    ) -> "Workspace":
        """Create a new live workspace via the right backend.

        ``workspace_id`` pins the live instance to a caller-supplied id
        (typically the same id the durable row is keyed by). Forwarding
        it keeps the row id and the live instance id in sync so re-attach
        via :meth:`get_workspace` works after the cache is evicted.
        """
        backend = await self.get_backend(template.provider_id)
        resolvers = FileResolvers(
            document_resolver=make_document_resolver(self._sp),
            secret_resolver=(
                make_secret_resolver(self._secret_provider)
                if self._secret_provider is not None
                else None
            ),
        )
        return await backend.create(
            template,
            overrides=overrides,
            workspace_id=workspace_id,
            resolvers=resolvers,
        )

    async def destroy(self, workspace_id: str) -> None:
        """Destroy the live workspace AND drop the persisted row."""
        row = await self.get_workspace_row(workspace_id)
        backend = await self.get_backend(row.provider_id)
        try:
            await backend.destroy(workspace_id)
        except NotFoundError:
            # Row exists but backend has no live instance; still drop
            # the row so callers can re-materialise without a stale
            # collision.
            pass

        await self._sp.get_storage(WorkspaceRow).delete(workspace_id)


__all__ = ["WorkspaceRegistry"]
