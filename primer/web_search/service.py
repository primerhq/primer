"""WebSearchService — singleton dispatch + active-config cache.

The service is the single object the ``web::web-search`` tool handler
depends on. It reads the active-config singleton row (with a short
TTL cache), then dispatches the search call:

* single mode -> one adapter; errors propagate.
* aggregated mode -> walk provider_ids in order; skip on
  WebSearchUnavailable / WebSearchProviderError / NotFoundError;
  surface an aggregated WebSearchUnavailable iff every provider
  raises a known class. Unknown exception classes propagate
  immediately (bugs aren't silently swallowed).

The PUT route for the singleton calls :meth:`invalidate_active_config`
on success so edits take effect on the next call. The 5s TTL is the
safety net for distributed deployments where the cache-invalidation
call didn't reach this process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from primer.model.except_ import NotFoundError
from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    ActiveWebSearchConfig,
    AggregatedProviderConfig,
    SingleProviderConfig,
)
from primer.web_search.adapter import (
    SafeSearchLevel,
    SearchHit,
    WebSearchProviderError,
    WebSearchUnavailable,
)


if TYPE_CHECKING:
    from primer.api.registries.web_search_registry import WebSearchRegistry
    from primer.int.storage import Storage


logger = logging.getLogger(__name__)


class WebSearchService:
    def __init__(
        self,
        *,
        registry: "WebSearchRegistry",
        active_config_storage: "Storage[ActiveWebSearchConfig]",
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._registry = registry
        self._storage = active_config_storage
        self._ttl = cache_ttl_seconds
        self._cached: ActiveWebSearchConfig | None = None
        self._cached_at: float = 0.0
        self._cache_lock = asyncio.Lock()

    async def search(
        self,
        *,
        query: str,
        count: int,
        safe_search: SafeSearchLevel,
    ) -> list[SearchHit]:
        cfg = await self._load_active_config()

        if isinstance(cfg.config, SingleProviderConfig):
            adapter = await self._registry.get(cfg.config.provider_id)
            return await adapter.search(
                query=query, count=count, safe_search=safe_search,
            )

        # Aggregated.
        assert isinstance(cfg.config, AggregatedProviderConfig)
        errors: list[tuple[str, BaseException]] = []
        for pid in cfg.config.provider_ids:
            try:
                adapter = await self._registry.get(pid)
            except NotFoundError as exc:
                # Race: row deleted between active-config write and now.
                logger.info(
                    "web-search: provider %s not found, falling back",
                    pid, extra={"error": str(exc)},
                )
                errors.append((pid, exc))
                continue
            try:
                return await adapter.search(
                    query=query, count=count, safe_search=safe_search,
                )
            except WebSearchProviderError as exc:
                logger.warning(
                    "web-search: provider %s misconfigured, falling back",
                    pid, extra={"error": str(exc)},
                )
                errors.append((pid, exc))
                continue
            except WebSearchUnavailable as exc:
                logger.info(
                    "web-search: provider %s unavailable, falling back",
                    pid, extra={"error": str(exc)},
                )
                errors.append((pid, exc))
                continue
            # Anything else propagates — unknown exception classes are
            # programmer bugs we don't silently swallow.

        summary = "; ".join(
            f"{pid}: {type(e).__name__}: {e}" for pid, e in errors
        )
        raise WebSearchUnavailable(
            f"all {len(errors)} providers failed: {summary}"
        )

    async def _load_active_config(self) -> ActiveWebSearchConfig:
        async with self._cache_lock:
            now = time.monotonic()
            if (
                self._cached is not None
                and (now - self._cached_at) < self._ttl
            ):
                return self._cached
            row = await self._storage.get(ACTIVE_WEB_SEARCH_CONFIG_ID)
            if row is None:
                raise WebSearchProviderError(
                    "no active web search config; configure one at "
                    "/v1/web_search_active_config"
                )
            self._cached = row
            self._cached_at = now
            return row

    def invalidate_active_config(self) -> None:
        """Sync; called from the PUT route's on_update hook."""
        self._cached = None
        self._cached_at = 0.0


__all__ = ["WebSearchService"]
