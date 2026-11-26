"""Slug-path document tree service (S2 Collections v2).

One node = one Document entity row (parent_id + slug + path mirror) plus
one content row (the body, single location). All mutations write both
rows in one backend transaction, mirroring DocumentService's contract.
The content store's UNIQUE(collection_id, path) enforces sibling-slug
uniqueness; an explicit pre-check turns it into a friendly 409.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from primer.int.document_content import DocumentContentStore
from primer.int.storage import Storage
from primer.model.collection import Document, _utcnow
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.payload_too_large import PayloadTooLargeError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

STRICT_SLUG_RE = re.compile(r"[a-z0-9-]+")

DOCUMENT_BODY_CAP = 1024 * 1024
"""Largest document body, in UTF-8 bytes, a USER door accepts (D5). Internal writers (the system collection, harness) are not capped."""
BODY_CAP_MESSAGE = "document body exceeds the 1 MiB cap"


def check_body_size(body: str) -> None:
    """Refuse a body over :data:`DOCUMENT_BODY_CAP` at a user door (KN-12); the services themselves stay uncapped for internal writers."""
    if len(body.encode("utf-8")) > DOCUMENT_BODY_CAP:
        raise PayloadTooLargeError(BODY_CAP_MESSAGE, limit_bytes=DOCUMENT_BODY_CAP)

#: Parent ids per ``parent_id IN (...)`` query.
_PARENTS_PER_QUERY = 200

Indexer = Callable[..., Awaitable[None]]
Unindexer = Callable[..., Awaitable[None]]
PathRewriter = Callable[..., Awaitable[None]]


class TreeNode(BaseModel):
    path: str
    slug: str
    title: str
    document_id: str
    has_children: bool


class TreeReadResult(BaseModel):
    document: Document
    body: str
    children: list[TreeNode]


class _Moved(Exception):
    """The row moved between update's read and its write."""


def _new_document_id() -> str:
    return Document(collection_id="_", slug="x", path="x").id


class DocumentTreeService:
    def __init__(self, storage_provider, *, indexer: Indexer | None = None,
                 unindexer: Unindexer | None = None,
                 path_rewriter: PathRewriter | None = None) -> None:
        self._sp = storage_provider
        self._docs: Storage[Document] = storage_provider.get_storage(Document)
        self._content: DocumentContentStore = storage_provider.get_content_store()
        self._indexer = indexer
        self._unindexer = unindexer
        self._path_rewriter = path_rewriter

    # ---- resolution ------------------------------------------------------

    async def resolve(self, *, collection_id: str, path: str) -> Document:
        """path -> entity, with sibling alternatives on miss."""
        doc_id = await self._content.resolve_id(collection_id, path)
        if doc_id is None:
            raise NotFoundError(await self._miss(collection_id, path))
        doc = await self._docs.get(doc_id)
        if doc is None:
            raise NotFoundError(
                f"document entity {doc_id!r} missing for path {path!r}"
            )
        return doc

    async def _miss(self, collection_id: str, path: str) -> str:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        sibs = await self._child_slugs(collection_id, parent)
        where = f"siblings of {parent}" if parent else "collection root has"
        listing = ", ".join(sibs[:20]) if sibs else "(none)"
        return f"no such path {path!r}; {where}: {listing}"

    async def _child_slugs(self, collection_id: str, parent_path: str) -> list[str]:
        parent_id = None
        if parent_path:
            pid = await self._content.resolve_id(collection_id, parent_path)
            if pid is None:
                return []
            parent_id = pid
        return sorted(c.slug for c in await self._children(collection_id, parent_id))

    async def _children(
        self, collection_id: str, parent_id: str | None
    ) -> list[Document]:
        q = Q(Document).where("collection_id", collection_id)
        # where(field, None) compiles to `= NULL` (always false); root
        # children need the dedicated IS NULL predicate.
        q = (
            q.where_null("parent_id")
            if parent_id is None
            else q.where("parent_id", parent_id)
        )
        predicate = q.build()
        out: list[Document] = []
        offset, page = 0, 200
        while True:
            resp = await self._docs.find(
                predicate, OffsetPage(offset=offset, length=page, want_total=False)
            )
            out.extend(resp.items)
            if len(resp.items) < page:
                return out
            offset += page

    async def _children_of(
        self, collection_id: str, parent_ids: list[str],
        conn: Any | None = None,
    ) -> list[Document]:
        """The children of every parent in ``parent_ids`` in one query per page (``parent_id = ANY(...)``), not one query per parent. ``conn`` runs the reads on the caller's open connection (and transaction), so an in-transaction re-read sees the caller's own uncommitted writes."""
        out: list[Document] = []
        for i in range(0, len(parent_ids), _PARENTS_PER_QUERY):
            predicate = (
                Q(Document).where("collection_id", collection_id)
                .where_in("parent_id", parent_ids[i:i + _PARENTS_PER_QUERY]).build()
            )
            offset, page = 0, 200
            while True:
                resp = await self._docs.find(
                    predicate, OffsetPage(offset=offset, length=page, want_total=False),
                    conn=conn,
                )
                out.extend(resp.items)
                if len(resp.items) < page:
                    break
                offset += page
        return out

    async def _resolve_parent(
        self, collection_id: str, parent: str
    ) -> tuple[str | None, str]:
        """parent path ('' = root) -> (parent_id, parent_path)."""
        if not parent:
            return None, ""
        doc = await self.resolve(collection_id=collection_id, path=parent)
        return doc.id, doc.path

    # ---- mutations -------------------------------------------------------

    async def create(self, *, collection_id: str, parent: str, slug: str,
                     body: str, title: str | None = None,
                     strict_slugs: bool = True,
                     meta: dict[str, Any] | None = None) -> Document:
        # The API edge enforces the strict charset. The system-collection
        # regenerator writes entity ids (agent-a, graph_x) that satisfy the
        # model charset but not the strict one, so it opts out here rather
        # than mangling the ids users search by.
        if strict_slugs and not STRICT_SLUG_RE.fullmatch(slug):
            raise BadRequestError(f"slug {slug!r} must match [a-z0-9-]+")
        parent_id, parent_path = await self._resolve_parent(collection_id, parent)
        path = f"{parent_path}/{slug}" if parent_path else slug
        if await self._content.resolve_id(collection_id, path) is not None:
            sibs = await self._child_slugs(collection_id, parent_path)
            raise ConflictError(
                f"path {path!r} already exists; siblings: {', '.join(sibs)}"
            )
        doc = Document(
            id=_new_document_id(),
            collection_id=collection_id,
            parent_id=parent_id,
            slug=slug,
            title=title,
            path=path,
            meta=meta or {},
        )
        async with self._sp.transaction() as conn:
            await self._docs.create(doc, conn=conn)
            await self._content.upsert(
                document_id=doc.id, collection_id=collection_id,
                path=path, content=body, conn=conn,
            )
        if self._indexer is not None:
            await self._indexer(document=doc, content=body)
        return doc

    async def update(self, *, collection_id: str, path: str,
                     body: str | None = None, title: str | None = None,
                     meta: dict[str, Any] | None = None) -> Document:
        fields: dict[str, Any] = {"updated_at": _utcnow()}
        if title is not None:
            fields["title"] = title
        if meta is not None:
            fields["meta"] = meta
        while True:
            doc = await self.resolve(collection_id=collection_id, path=path)
            patch = doc.model_copy(update=fields).model_dump(mode="json", include=set(fields))
            try:
                async with self._sp.transaction() as conn:
                    # Content first, the order move() takes its rows in; the guarded patch then writes only these fields, and only while
                    # the row still sits at the path read, so a move that committed meanwhile is never reverted (KN-19f).
                    if body is not None:
                        await self._content.upsert(
                            document_id=doc.id, collection_id=collection_id,
                            path=doc.path, content=body, conn=conn,
                        )
                    updated = await self._docs.patch_if(doc.id, patch, where={"path": [doc.path]}, conn=conn)
                    if updated is None:
                        raise _Moved  # rolls the content write back
                break
            except _Moved:
                continue  # answer as if the move ran first: resolve the path again
        if body is not None and self._indexer is not None:
            await self._indexer(document=updated, content=body)
        return updated

    async def move(self, *, collection_id: str, path: str,
                   new_parent: str, new_slug: str | None = None,
                   strict_slugs: bool = True,
                   allow_missing_parent: bool = False) -> Document:
        """Move ``path`` under ``new_parent`` (and rename it to ``new_slug``), carrying its subtree.

        ``allow_missing_parent`` is the path-addressed door's tolerance (``DocumentService.move``: it has always been able to move a document into
        a directory that has no row of its own, as ``put_document`` can write one): a ``new_parent`` path holding no document is then used as a
        plain path prefix with no parent row, and none is invented. The tree routes leave it off and refuse such a parent.
        """
        doc = await self.resolve(collection_id=collection_id, path=path)
        slug = new_slug or doc.slug
        if strict_slugs and not STRICT_SLUG_RE.fullmatch(slug):
            raise BadRequestError(f"slug {slug!r} must match [a-z0-9-]+")
        try:
            parent_id, parent_path = await self._resolve_parent(collection_id, new_parent)
        except NotFoundError:
            if not allow_missing_parent:
                raise
            parent_id, parent_path = None, new_parent
        if parent_path == doc.path or parent_path.startswith(doc.path + "/"):
            raise BadRequestError(
                f"cannot move {doc.path!r} into its own subtree {parent_path!r}"
            )
        new_path = f"{parent_path}/{slug}" if parent_path else slug
        if new_path != doc.path and (
            await self._content.resolve_id(collection_id, new_path) is not None
        ):
            raise ConflictError(f"path {new_path!r} already exists")
        old_prefix = doc.path
        moved = doc.model_copy(update={
            "parent_id": parent_id, "slug": slug,
            "path": new_path, "updated_at": _utcnow(),
        })
        patch = moved.model_dump(mode="json", include={"parent_id", "slug", "path", "updated_at"})
        async with self._sp.transaction() as conn:
            # Field-level, guarded on the place the row was read (KN-19f): a whole-row write from the pre-read snapshot
            # would write a concurrent update() of title/meta back; the guard makes that impossible, and a row that
            # moved in the window is a conflict, not a silent clobber.
            if await self._docs.patch_if(doc.id, patch, where={"path": [doc.path]}, conn=conn) is None:
                raise ConflictError(f"{doc.path!r} moved while it was being moved; retry the move")
            # The subtree is read INSIDE the transaction (on the transaction's
            # own connection, so the re-reads see this transaction's own
            # uncommitted writes): a child created between the read of the row
            # above and this write belongs to the moved subtree (a pre-read set
            # would have left it addressed by a path whose parent no longer
            # exists), and a child a peer relocated meanwhile is no longer a
            # descendant and is left where the peer put it.
            descendants = await self._descendants(collection_id, doc, conn=conn)
            await self._content.move(doc.id, new_path, conn=conn)
            for d in descendants:
                d_path = new_path + d.path[len(old_prefix):]
                await self._content.move(d.id, d_path, conn=conn)
                d_patch = d.model_copy(
                    update={"path": d_path, "updated_at": _utcnow()}
                ).model_dump(mode="json", include={"path", "updated_at"})
                if await self._docs.patch_if(d.id, d_patch, where={"path": [d.path]}, conn=conn) is None:
                    raise ConflictError(
                        f"{doc.path!r}'s subtree changed while it was being moved; retry the move"
                    )
            # read-write-read diff against the READ COMMITTED sub-statement window (M1): a peer can commit a child of
            # the walked set between the walk's last statement and this check (no read before it sees that child);
            # a changed child set is a conflict (rollback, retry the move), not a row left addressed by a path whose
            # prefix moved. The re-read runs on the transaction's own connection: on a fresh pooled connection a
            # Postgres re-read would see the pre-write committed state instead of the write's own effects.
            settled = {d.id for d in await self._children_of(
                collection_id, [doc.id, *[d.id for d in descendants]], conn=conn,
            )}
            base = {d.id for d in descendants}
            if settled != base:
                appeared = sorted(settled - base)
                raise ConflictError(
                    f"{doc.path!r}'s subtree changed while it was being moved "
                    f"(descendant(s) {', '.join(appeared) or 'no longer present'}); retry the move"
                )
        # Chunk metadata carries the path, so a move rewrites it. Metadata
        # only: the vectors are unchanged, so nothing re-embeds.
        if self._path_rewriter is not None:
            await self._path_rewriter(
                document_id=doc.id, collection_id=collection_id,
                new_path=new_path,
            )
            for d in descendants:
                await self._path_rewriter(
                    document_id=d.id, collection_id=collection_id,
                    new_path=new_path + d.path[len(old_prefix):],
                )
        return moved

    async def delete(self, *, collection_id: str, path: str,
                     recursive: bool = False) -> list[str]:
        doc = await self.resolve(collection_id=collection_id, path=path)
        targets_check = await self._descendants(collection_id, doc)
        if targets_check and not recursive:
            raise ConflictError(
                f"{path!r} has {len(targets_check)} descendant(s); pass "
                "recursive=true to delete the subtree"
            )
        async with self._sp.transaction() as conn:
            # Re-read the subtree INSIDE the transaction (on the transaction's own
            # connection, so it sees this transaction's own uncommitted writes):
            # a child created between the check above and this write belongs to
            # the subtree (the pre-read set would have deleted the node under it
            # and orphaned the child).
            descendants = await self._descendants(collection_id, doc, conn=conn)
            if descendants and not recursive:
                raise ConflictError(
                    f"{path!r} has {len(descendants)} descendant(s); pass "
                    "recursive=true to delete the subtree"
                )
            targets = [doc, *descendants]
            for t in targets:
                await self._docs.delete(t.id, conn=conn)
                await self._content.delete(t.id, conn=conn)
            # The same diff for delete: a child committed between the re-read and this point would survive the
            # delete orphaned (its parent gone); the re-read after the delete must find none. It runs on the
            # transaction's OWN connection: a fresh pooled one cannot see this transaction's uncommitted
            # deletes, so it would find the just-deleted children still present and conflict on every
            # non-leaf delete (a retry would never converge).
            leftovers = await self._children_of(
                collection_id, [t.id for t in targets], conn=conn,
            )
            if leftovers:
                raise ConflictError(
                    f"{len(leftovers)} descendant(s) of {path!r} appeared while it was being "
                    f"deleted ({', '.join(sorted(l.path for l in leftovers))}); retry the delete"
                )
        if self._unindexer is not None:
            for t in targets:
                await self._unindexer(document_id=t.id, collection_id=collection_id)
        return [t.id for t in targets]

    async def _descendants(
        self, collection_id: str, root: Document,
        conn: Any | None = None,
    ) -> list[Document]:
        """Every descendant of ``root`` in a level-batched walk: one ``parent_id IN (...)`` query per level (each batched by :data:`_PARENTS_PER_QUERY`), not one query per node - a per-node walk paid a collection scan per descendant (3.5). ``conn`` (the caller's open transaction connection) makes the walk see the caller's own uncommitted writes."""
        out: list[Document] = []
        level = [root.id]
        while level:
            kids = await self._children_of(collection_id, level, conn=conn)
            out.extend(kids)
            level = [k.id for k in kids]
        return out

    # ---- reads -----------------------------------------------------------

    async def tree(self, *, collection_id: str, parent: str = "",
                   depth: int = 1) -> list[TreeNode]:
        parent_id, _ = await self._resolve_parent(collection_id, parent)
        out: list[TreeNode] = []
        level_kids = await self._children(collection_id, parent_id)
        level = 1
        while level_kids:
            # One query for the children of the whole level: they say which of this level's nodes have children, and (while
            # the walk goes deeper) they are the next level.
            grand = await self._children_of(collection_id, [k.id for k in level_kids])
            has_children = {g.parent_id for g in grand}
            for k in sorted(level_kids, key=lambda d: d.slug):
                out.append(TreeNode(
                    path=k.path, slug=k.slug, title=k.title or k.slug,
                    document_id=k.id, has_children=k.id in has_children,
                ))
            level_kids = grand if level < depth else []
            level += 1
        return sorted(out, key=lambda n: n.path)

    async def read(self, *, collection_id: str, path: str,
                   with_children: bool = True) -> TreeReadResult:
        """The page at ``path``; ``with_children=False`` skips the child listing (``children`` is empty) for a caller that only compares the page."""
        doc = await self.resolve(collection_id=collection_id, path=path)
        body = await self._content.get(doc.id)
        children = await self._child_nodes(collection_id, doc.id) if with_children else []
        return TreeReadResult(document=doc, body=body or "", children=children)

    async def _child_nodes(
        self, collection_id: str, parent_id: str | None
    ) -> list[TreeNode]:
        kids = sorted(
            await self._children(collection_id, parent_id), key=lambda d: d.slug
        )
        has_children = {g.parent_id for g in await self._children_of(collection_id, [k.id for k in kids])}
        return [
            TreeNode(
                path=k.path, slug=k.slug, title=k.title or k.slug,
                document_id=k.id, has_children=k.id in has_children,
            )
            for k in kids
        ]


__all__ = ["DocumentTreeService", "TreeNode", "TreeReadResult", "STRICT_SLUG_RE"]
