"""Migration 4: derive ``Document.slug`` and ``parent_id`` from ``path``.

A document used to identify its node with ``name``. The tree gave it
``slug`` and ``parent_id`` instead, and ``slug`` is required, so every
row an upgraded install already had raises ``missing`` on load and
listing a collection's tree returns a 500. Both values are derivable
from ``path``, which every row already carries and which stays correct.

**Why this is not another step in m003.** It belongs to the same cutover
and would read better there, but m003 has already been applied to an
install, and the runner is forward-only: it never revisits a stamped
version. A repair added to m003 today would silently skip exactly the
installs that need it.

See :mod:`~primer.storage.migrations.m003_session_cutover` for why a
migration declares its own view of the row it is repairing.
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.storage.migrations.m003_session_cutover import _iter_rows

logger = logging.getLogger(__name__)


class Document(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view of a document row, pre- and post-tree.

    ``name`` is left where it is. The live model ignores unknown keys, so
    it costs nothing, and it is the only record of what the node was
    called before slugs existed.
    """

    model_config = ConfigDict(extra="allow")

    collection_id: str | None = Field(default=None)
    path: str | None = Field(default=None)
    slug: str | None = Field(default=None)
    parent_id: str | None = Field(default=None)


class M004DocumentSlugs:
    """Give pre-tree document rows the slug and parent the tree needs."""

    version = 4
    description = "derive Document slug and parent_id from path"

    async def apply(self, sp: StorageProvider) -> None:
        await self._derive_document_slugs(sp)

    async def _derive_document_slugs(self, sp: StorageProvider) -> None:
        docs = sp.get_storage(Document)
        rows = [row async for row in _iter_rows(docs)]

        # Every row's path, so a child can find the id of its parent. Built
        # from all of them, not just the ones being rewritten, because a
        # partially migrated tree still has to resolve.
        by_path: dict[tuple[str, str], str] = {
            (row.collection_id, row.path): row.id
            for row in rows
            if row.collection_id and row.path and row.id
        }

        derived = 0
        for row in rows:
            if row.slug:
                continue  # already migrated
            if not row.path:
                logger.warning(
                    "document has no path to derive a slug from; leaving it",
                    extra={"document_id": row.id},
                )
                continue
            parent_path, _, slug = row.path.rpartition("/")
            if not slug:
                logger.warning(
                    "document path ends in a separator; leaving it",
                    extra={"document_id": row.id, "path": row.path},
                )
                continue
            parent_id = (
                by_path.get((row.collection_id, parent_path))
                if parent_path
                else None
            )
            if parent_path and parent_id is None:
                logger.warning(
                    "document names a parent path with no row; treating it as a root",
                    extra={"document_id": row.id, "path": row.path},
                )
            await docs.update(
                row.model_copy(update={"slug": slug, "parent_id": parent_id})
            )
            derived += 1
        if derived:
            logger.info("derived document slugs", extra={"count": derived})


__all__ = ["M004DocumentSlugs"]
