"""First-boot bootstrap helpers + storage-provider construction.

Extracted verbatim from :mod:`primer.api.app` as part of the app.py
decomposition. ``_bootstrap_web_search`` / ``_bootstrap_web_fetch`` are
called from the lifespan handler before their toolsets are built;
``_build_storage_provider`` constructs the storage provider from the
:class:`~primer.api.config.AppConfig`. All three are re-exported from
``primer.api.app`` for backwards compatibility.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from primer.api.config import AppConfig

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


async def _bootstrap_web_search(storage_provider) -> None:
    """Idempotent: ensure the reserved DDG provider row + active
    config singleton exist. Called from the lifespan handler before
    the web toolset is built.

    Order matters: the DDG row must exist before the active config
    singleton, because the singleton's reference validation runs at
    write time."""
    from primer.model.web_search import (
        ACTIVE_WEB_SEARCH_CONFIG_ID,
        ActiveWebSearchConfig,
        DuckDuckGoConfig,
        SingleProviderConfig,
        WebSearchProvider,
        WebSearchProviderType,
    )

    from primer.model.except_ import ConflictError

    ws_storage = storage_provider.get_storage(WebSearchProvider)
    if await ws_storage.get("DuckDuckGo") is None:
        try:
            await ws_storage.create(WebSearchProvider(
                id="DuckDuckGo",
                provider_type=WebSearchProviderType.DUCKDUCKGO,
                config=DuckDuckGoConfig(),
            ))
            logger.info(
                "bootstrap: created reserved web-search provider DuckDuckGo"
            )
        except ConflictError:
            # Cross-process bootstrap race: another primer process created
            # the reserved row between our get() and create(). The desired
            # end state (the row exists) holds, so this is a no-op.
            logger.debug("bootstrap: DuckDuckGo row created concurrently")

    ac_storage = storage_provider.get_storage(ActiveWebSearchConfig)
    if await ac_storage.get(ACTIVE_WEB_SEARCH_CONFIG_ID) is None:
        try:
            await ac_storage.create(ActiveWebSearchConfig(
                id=ACTIVE_WEB_SEARCH_CONFIG_ID,
                config=SingleProviderConfig(provider_id="DuckDuckGo"),
            ))
            logger.info(
                "bootstrap: created reserved active web-search config "
                "(single -> DuckDuckGo)"
            )
        except ConflictError:
            logger.debug(
                "bootstrap: active web-search config created concurrently"
            )


async def _bootstrap_web_fetch(storage_provider) -> None:
    """Idempotent: ensure the reserved LOCAL provider row + active config
    singleton (single -> local) exist. Mirrors _bootstrap_web_search."""
    from primer.model.web_fetch import (
        ACTIVE_WEB_FETCH_CONFIG_ID, ActiveWebFetchConfig, LocalFetchConfig,
        SingleFetchConfig, WebFetchProvider, WebFetchProviderType,
    )
    from primer.model.except_ import ConflictError

    wf_storage = storage_provider.get_storage(WebFetchProvider)
    if await wf_storage.get("local") is None:
        try:
            await wf_storage.create(WebFetchProvider(
                id="local", provider_type=WebFetchProviderType.LOCAL,
                config=LocalFetchConfig(),
            ))
            logger.info("bootstrap: created reserved web-fetch provider local")
        except ConflictError:
            logger.debug("bootstrap: web-fetch local row created concurrently")

    ac_storage = storage_provider.get_storage(ActiveWebFetchConfig)
    if await ac_storage.get(ACTIVE_WEB_FETCH_CONFIG_ID) is None:
        try:
            await ac_storage.create(ActiveWebFetchConfig(
                id=ACTIVE_WEB_FETCH_CONFIG_ID,
                config=SingleFetchConfig(provider_id="local"),
            ))
            logger.info("bootstrap: created reserved active web-fetch config (single -> local)")
        except ConflictError:
            logger.debug("bootstrap: active web-fetch config created concurrently")


def _build_storage_provider(config: AppConfig) -> "StorageProvider":
    """Construct the storage provider from the AppConfig.

    When ``config.db`` is None, default to embedded SQLite at
    ``~/.primer/db/data.sqlite``. The parent directory is created
    on demand inside :meth:`SqliteStorageProvider.initialize`.
    """
    from primer.model.provider import (
        SqliteConfig as _SqliteConfig,
        StorageProviderConfig as _StorageProviderConfig,
        StorageProviderType as _StorageProviderType,
    )
    from primer.storage.factory import StorageProviderFactory

    from primer.model.provider import PostgresConfig as _PostgresConfig

    sp_config = config.db
    if sp_config is None:
        default_path = Path.home() / ".primer" / "db" / "data.sqlite"
        sp_config = _StorageProviderConfig(
            provider=_StorageProviderType.SQLITE,
            config=_SqliteConfig(path=default_path),
        )

    # PRIMER_DB_SCHEMA overrides the Postgres schema for test isolation.
    # SQLite has no schema concept, so the override is silently ignored
    # when the backend is SQLite.
    if config.db_schema is not None and isinstance(sp_config.config, _PostgresConfig):
        sp_config = sp_config.model_copy(
            update={"config": sp_config.config.model_copy(
                update={"db_schema": config.db_schema}
            )}
        )

    return StorageProviderFactory.create(sp_config)
