"""Migration 13: raise stored ``hnsw_ef_search = 40`` to ``100`` on semantic-search providers.

``_PgVectorBaseConfig.hnsw_ef_search``, ``LanceConfig.hnsw_ef_search``, and the
built-in ``lance`` seed row all moved from ``40`` to ``100`` in #841 because
incremental HNSW graphs under concurrent inserts miss recently written
documents at the stock beam of 40. An install that ran before #841 already has
``hnsw_ef_search: 40`` persisted inside its stored ``SemanticSearchProvider``
rows, and the bootstrap seed is create-if-missing, so upgraded installs kept
running at ``ef = 40``.

This migration updates any stored ``SemanticSearchProvider`` row whose
``config.hnsw_ef_search`` is ``40`` to ``100`` via a field-scoped ``patch_if``
on ``("config", "hnsw_ef_search")``, leaving every other ``config`` key (such
as an encrypted/plaintext Postgres password or custom HNSW parameters) and
every row with a non-40 ``hnsw_ef_search`` untouched. An operator who
deliberately chose ``40`` can set it back after upgrading.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200
_LEGACY_EF_SEARCH = 40
_DEFAULT_EF_SEARCH = 100


class SemanticSearchProvider(Identifiable):  # noqa: N801 - class name selects the table
    """Migration-local raw view of a semantic-search provider row."""

    model_config = ConfigDict(extra="allow")

    provider: str | None = Field(default=None)
    config: dict[str, Any] | None = Field(default=None)


class M013HnswEfSearchDefault:
    """Update stored semantic-search provider rows from ``hnsw_ef_search=40`` to ``100``."""

    version = 13
    description = "Raise stored hnsw_ef_search=40 to 100 on semantic-search providers"

    async def apply(self, sp: StorageProvider) -> None:
        providers = sp.get_storage(SemanticSearchProvider)
        offset = 0
        while True:
            page = await providers.list(OffsetPage(offset=offset, length=_PAGE))
            for row in page.items:
                if not isinstance(row.config, dict):
                    continue
                ef = row.config.get("hnsw_ef_search")
                if isinstance(ef, bool) or ef != _LEGACY_EF_SEARCH:
                    continue
                assert row.id is not None
                await providers.patch_if(
                    row.id,
                    set_paths={("config", "hnsw_ef_search"): _DEFAULT_EF_SEARCH},
                    where={("config", "hnsw_ef_search"): [_LEGACY_EF_SEARCH]},
                )
            if len(page.items) < _PAGE:
                break
            offset += _PAGE


__all__ = ["M013HnswEfSearchDefault"]
