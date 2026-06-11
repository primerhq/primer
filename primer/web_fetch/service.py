"""WebFetchService: active-config TTL cache + single/aggregated dispatch.

Mirrors WebSearchService, with two web-fetch additions:
  * is_thin escalation: in aggregated mode, a successful-but-thin result from
    one provider is treated as a soft failure when another provider remains
    (so a static-fetch JS shell escalates to a JS-capable provider). The last
    provider's thin result is returned best-effort.
  * output limit: max_chars / max_lines (or the DEFAULT_MAX_CHARS ceiling when
    both are None) are applied to the chosen page's markdown in one place.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from primer.model.except_ import NotFoundError
from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID, ActiveWebFetchConfig,
    AggregatedFetchConfig, SingleFetchConfig,
)
from primer.web_fetch.adapter import (
    DEFAULT_MAX_CHARS, FetchedPage, WebFetchProviderError, WebFetchUnavailable,
)

if TYPE_CHECKING:
    from primer.api.registries.web_fetch_registry import WebFetchRegistry
    from primer.int.storage import Storage

logger = logging.getLogger(__name__)


def _apply_limit(md: str, max_chars: int | None, max_lines: int | None) -> tuple[str, bool]:
    if max_chars is None and max_lines is None:
        max_chars = DEFAULT_MAX_CHARS
    truncated = False
    if max_lines is not None:
        lines = md.splitlines()
        if len(lines) > max_lines:
            md = "\n".join(lines[:max_lines])
            truncated = True
    if max_chars is not None and len(md) > max_chars:
        md = md[:max_chars]
        truncated = True
    return md, truncated


class WebFetchService:
    def __init__(self, *, registry: "WebFetchRegistry",
                 active_config_storage: "Storage[ActiveWebFetchConfig]",
                 cache_ttl_seconds: float = 5.0) -> None:
        self._registry = registry
        self._storage = active_config_storage
        self._ttl = cache_ttl_seconds
        self._cached: ActiveWebFetchConfig | None = None
        self._cached_at: float = 0.0
        self._cache_lock = asyncio.Lock()

    async def fetch(self, *, url: str, max_chars: int | None,
                    max_lines: int | None) -> FetchedPage:
        cfg = await self._load_active_config()
        page = await self._dispatch(cfg, url)
        text, truncated = _apply_limit(page.content_markdown, max_chars, max_lines)
        return page.model_copy(update={
            "content_markdown": text, "truncated_by_limit": truncated,
        })

    async def _dispatch(self, cfg: ActiveWebFetchConfig, url: str) -> FetchedPage:
        if isinstance(cfg.config, SingleFetchConfig):
            adapter = await self._registry.get(cfg.config.provider_id)
            return await adapter.fetch(url=url)

        assert isinstance(cfg.config, AggregatedFetchConfig)
        pids = cfg.config.provider_ids
        errors: list[tuple[str, BaseException]] = []
        last_thin: FetchedPage | None = None
        for i, pid in enumerate(pids):
            is_last = i == len(pids) - 1
            try:
                adapter = await self._registry.get(pid)
            except NotFoundError as exc:
                errors.append((pid, exc)); continue
            try:
                page = await adapter.fetch(url=url)
            except WebFetchProviderError as exc:
                logger.warning("web-fetch: provider %s misconfigured, falling back", pid, extra={"error": str(exc)})
                errors.append((pid, exc)); continue
            except WebFetchUnavailable as exc:
                logger.info("web-fetch: provider %s unavailable, falling back", pid, extra={"error": str(exc)})
                errors.append((pid, exc)); continue
            if page.is_thin and not is_last:
                logger.info("web-fetch: provider %s returned thin content, escalating", pid)
                last_thin = page; continue
            return page
        if last_thin is not None:
            return last_thin
        summary = "; ".join(f"{pid}: {type(e).__name__}: {e}" for pid, e in errors)
        raise WebFetchUnavailable(f"all {len(errors)} providers failed: {summary}")

    async def _load_active_config(self) -> ActiveWebFetchConfig:
        async with self._cache_lock:
            now = time.monotonic()
            if self._cached is not None and (now - self._cached_at) < self._ttl:
                return self._cached
            row = await self._storage.get(ACTIVE_WEB_FETCH_CONFIG_ID)
            if row is None:
                raise WebFetchProviderError(
                    "no active web fetch config; configure one at /v1/web_fetch_active_config"
                )
            self._cached = row
            self._cached_at = now
            return row

    def invalidate_active_config(self) -> None:
        self._cached = None
        self._cached_at = 0.0


__all__ = ["WebFetchService"]
