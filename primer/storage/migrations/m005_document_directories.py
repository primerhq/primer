"""Migration 5: give every nested document a real directory to hang from.

Pre-tree data is flat. ``path`` was the only structure a document had, so
a row could sit at ``cookbook/x.md`` with nothing at ``cookbook`` at all.
:mod:`~primer.storage.migrations.m004_document_slugs` derives a slug for
such a row and, finding no parent, leaves it as a root. That keeps it
readable but leaves it inconsistent: its path is no longer its parent's
path plus its slug, so the first move or rename computes a fresh path
and strands the body sitting under the old one.

This migration closes that gap by creating the missing directories. A
synthesised node gets a document row *and* an empty content entry,
because the content store is what resolves a parent path: without the
entry the tree service cannot list the directory or create anything
beneath it.

**Why this is not part of m004.** m004 has already been applied to an
install, and the runner is forward-only, so extending it would skip the
very rows that need repairing. Splitting the repair out also lets it
state its own rule, which covers both cases in one pass: any document
whose path names a parent that has no row gets that parent built.
"""

from __future__ import annotations

import logging

from primer.int.storage_provider import StorageProvider
from primer.model.collection import Document as LiveDocument
from primer.model.except_ import ConflictError
from primer.storage.migrations.m003_session_cutover import _iter_rows
from primer.storage.migrations.m004_document_slugs import Document

logger = logging.getLogger(__name__)


class M005DocumentDirectories:
    """Build the directory rows that flat pre-tree paths only implied."""

    version = 5
    description = "synthesise missing directory documents for nested paths"

    async def apply(self, sp: StorageProvider) -> None:
        docs = sp.get_storage(Document)
        content = sp.get_content_store()
        rows = [row async for row in _iter_rows(docs)]

        by_path: dict[tuple[str, str], str] = {
            (row.collection_id, row.path): row.id
            for row in rows
            if row.collection_id and row.path and row.id
        }

        repaired = 0
        synthesised = 0
        # Shallowest first, so a directory that does have a row is adopted
        # rather than synthesised when its children are reached.
        for row in sorted(rows, key=lambda r: (r.path or "").count("/")):
            if not row.collection_id or not row.path:
                continue
            parent_path, _, slug = row.path.rpartition("/")
            if not parent_path or not slug:
                continue  # a root, and correctly so
            if row.parent_id and by_path.get((row.collection_id, parent_path)) == (
                row.parent_id
            ):
                continue  # already hangs from the right row
            parent_id, made = await self._ensure_chain(
                docs, content, row.collection_id, parent_path, by_path,
            )
            synthesised += made
            await docs.update(
                row.model_copy(update={"slug": slug, "parent_id": parent_id})
            )
            repaired += 1

        if synthesised:
            logger.info(
                "synthesised directory documents", extra={"count": synthesised},
            )
        if repaired:
            logger.info(
                "reparented nested documents", extra={"count": repaired},
            )

    async def _ensure_chain(
        self, docs, content, collection_id: str, parent_path: str,
        by_path: dict[tuple[str, str], str],
    ) -> tuple[str | None, int]:
        """Return the id owning ``parent_path``, building the chain if absent."""
        made = 0
        parent_id: str | None = None
        walked = ""
        for segment in parent_path.split("/"):
            walked = f"{walked}/{segment}" if walked else segment
            existing = by_path.get((collection_id, walked))
            if existing is not None:
                parent_id = existing
                continue
            node_id = LiveDocument(collection_id="_", slug="x", path="x").id
            try:
                await docs.create(
                    Document(
                        id=node_id,
                        collection_id=collection_id,
                        path=walked,
                        slug=segment,
                        parent_id=parent_id,
                    )
                )
                await content.upsert(
                    document_id=node_id, collection_id=collection_id,
                    path=walked, content="",
                )
            except ConflictError:
                # Every pod runs the chain on boot, and a deploy rolls the
                # API and the workers together, so two of them can reach an
                # unapplied version at the same moment. The path is unique
                # in the content store, which is what makes the second
                # writer lose rather than duplicate the directory. Adopt
                # what the winner wrote.
                #
                # The row is created before the content entry, so losing the
                # race leaves our row behind with no path of its own. Drop it:
                # a document the content store cannot resolve is invisible to
                # every read path and would block nothing but confuse everything.
                won = await content.resolve_id(collection_id, walked)
                if won is None:
                    raise
                await docs.delete(node_id)
                by_path[(collection_id, walked)] = won
                parent_id = won
                continue
            by_path[(collection_id, walked)] = node_id
            parent_id = node_id
            made += 1
        return parent_id, made


__all__ = ["M005DocumentDirectories"]
