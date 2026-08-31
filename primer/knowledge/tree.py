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

from pydantic import BaseModel

from primer.int.document_content import DocumentContentStore
from primer.int.storage import Storage
from primer.model.collection import Document, _utcnow
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.storage import OffsetPage
from primer.storage.q import Q

STRICT_SLUG_RE = re.compile(r"[a-z0-9-]+")

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
                predicate, OffsetPage(offset=offset, length=page)
            )
            out.extend(resp.items)
            if len(resp.items) < page:
                return out
            offset += page

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
                     strict_slugs: bool = True) -> Document:
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
                     body: str | None = None, title: str | None = None) -> Document:
        doc = await self.resolve(collection_id=collection_id, path=path)
        updated = doc.model_copy(update={
            "title": doc.title if title is None else title,
            "updated_at": _utcnow(),
        })
        async with self._sp.transaction() as conn:
            await self._docs.update(updated, conn=conn)
            if body is not None:
                await self._content.upsert(
                    document_id=doc.id, collection_id=collection_id,
                    path=doc.path, content=body, conn=conn,
                )
        if body is not None and self._indexer is not None:
            await self._indexer(document=updated, content=body)
        return updated

    async def move(self, *, collection_id: str, path: str,
                   new_parent: str, new_slug: str | None = None,
                   strict_slugs: bool = True) -> Document:
        doc = await self.resolve(collection_id=collection_id, path=path)
        slug = new_slug or doc.slug
        if strict_slugs and not STRICT_SLUG_RE.fullmatch(slug):
            raise BadRequestError(f"slug {slug!r} must match [a-z0-9-]+")
        parent_id, parent_path = await self._resolve_parent(collection_id, new_parent)
        if parent_path == doc.path or parent_path.startswith(doc.path + "/"):
            raise BadRequestError(
                f"cannot move {doc.path!r} into its own subtree {parent_path!r}"
            )
        new_path = f"{parent_path}/{slug}" if parent_path else slug
        if new_path != doc.path and (
            await self._content.resolve_id(collection_id, new_path) is not None
        ):
            raise ConflictError(f"path {new_path!r} already exists")
        descendants = await self._descendants(collection_id, doc)
        old_prefix = doc.path
        moved = doc.model_copy(update={
            "parent_id": parent_id, "slug": slug,
            "path": new_path, "updated_at": _utcnow(),
        })
        async with self._sp.transaction() as conn:
            await self._content.move(doc.id, new_path, conn=conn)
            await self._docs.update(moved, conn=conn)
            for d in descendants:
                d_path = new_path + d.path[len(old_prefix):]
                await self._content.move(d.id, d_path, conn=conn)
                await self._docs.update(
                    d.model_copy(update={"path": d_path, "updated_at": _utcnow()}),
                    conn=conn,
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
        descendants = await self._descendants(collection_id, doc)
        if descendants and not recursive:
            raise ConflictError(
                f"{path!r} has {len(descendants)} descendant(s); pass "
                "recursive=true to delete the subtree"
            )
        targets = [doc, *descendants]
        async with self._sp.transaction() as conn:
            for t in targets:
                await self._docs.delete(t.id, conn=conn)
                await self._content.delete(t.id, conn=conn)
        if self._unindexer is not None:
            for t in targets:
                await self._unindexer(document_id=t.id, collection_id=collection_id)
        return [t.id for t in targets]

    async def _descendants(
        self, collection_id: str, root: Document
    ) -> list[Document]:
        out: list[Document] = []
        frontier = [root]
        while frontier:
            cur = frontier.pop(0)
            kids = await self._children(collection_id, cur.id)
            out.extend(kids)
            frontier.extend(kids)
        return out

    # ---- reads -----------------------------------------------------------

    async def tree(self, *, collection_id: str, parent: str = "",
                   depth: int = 1) -> list[TreeNode]:
        parent_id, _ = await self._resolve_parent(collection_id, parent)
        out: list[TreeNode] = []
        frontier: list[tuple[str | None, int]] = [(parent_id, 1)]
        while frontier:
            pid, level = frontier.pop(0)
            for node in await self._child_nodes(collection_id, pid):
                out.append(node)
                if level < depth and node.has_children:
                    frontier.append((node.document_id, level + 1))
        return sorted(out, key=lambda n: n.path)


    async def read(self, *, collection_id: str, path: str) -> TreeReadResult:
        doc = await self.resolve(collection_id=collection_id, path=path)
        body = await self._content.get(doc.id)
        children = await self._child_nodes(collection_id, doc.id)
        return TreeReadResult(document=doc, body=body or "", children=children)

    async def _child_nodes(
        self, collection_id: str, parent_id: str | None
    ) -> list[TreeNode]:
        kids = sorted(
            await self._children(collection_id, parent_id), key=lambda d: d.slug
        )
        nodes: list[TreeNode] = []
        for k in kids:
            grand = await self._children(collection_id, k.id)
            nodes.append(TreeNode(
                path=k.path, slug=k.slug, title=k.title or k.slug,
                document_id=k.id, has_children=bool(grand),
            ))
        return nodes


__all__ = ["DocumentTreeService", "TreeNode", "TreeReadResult", "STRICT_SLUG_RE"]
