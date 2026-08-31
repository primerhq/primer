"""``collections`` internal toolset - navigate and edit document trees.

Always registered: a collection is a text wiki first, so listing,
reading, searching and editing need no embedder, no vector store and no
indexing pass. One ``search`` tool covers the whole capability ladder:
mode auto resolves to the best rung the collection offers (semantic when
its index is ready, else the backend's built-in full-text), the result
states ``mode_used``, and an explicit ask for an unavailable rung fails
naming the modes that are available.

Navigation ergonomics are the contract here: a miss reports the siblings
it did find, and every write refuses a system-owned collection.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from primer.knowledge.fulltext import fulltext_collection
from primer.knowledge.grep import grep_collection as _grep
from primer.knowledge.indexing import (
    make_document_indexer,
    make_document_path_rewriter,
    make_document_unindexer,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.collection import Collection, Document
from primer.model.except_ import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PrimerError,
)
from primer.model.storage import OffsetPage
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok_json as _ok
from primer.toolset.internal import InternalToolsetProvider, ToolHandler

logger = logging.getLogger(__name__)

COLLECTIONS_TOOLSET_ID = "collections"


# ---------------------------------------------------------------------------
# Argument models
# ---------------------------------------------------------------------------


class _CollArgs(BaseModel):
    collection: str = Field(..., min_length=1, description="Collection id.")


class _TreeArgs(_CollArgs):
    parent: str = Field(default="", description="Parent path; '' = root.")
    depth: int = Field(default=2, ge=1, le=10)


class _ReadArgs(_CollArgs):
    path: str = Field(..., min_length=1, description="Slug path of the document.")


class _SearchArgs(_CollArgs):
    query: str = Field(..., min_length=1, description=(
        "What to find. Natural language for auto/fulltext/semantic; an "
        "exact substring for literal; a pattern only for regex."))
    mode: Literal["auto", "semantic", "fulltext", "literal", "regex"] = Field(
        default="auto")
    path_prefix: str | None = Field(default=None)
    limit: int = Field(default=20, ge=1, le=100)


class _CreateArgs(_CollArgs):
    parent: str = Field(default="", description="Parent path; '' = root.")
    slug: str = Field(..., min_length=1, description="Segment name [a-z0-9-].")
    title: str | None = Field(default=None)
    body: str = Field(..., description="Document body.")


class _UpdateArgs(_ReadArgs):
    body: str | None = Field(default=None)
    title: str | None = Field(default=None)


class _MoveArgs(_ReadArgs):
    new_parent: str = Field(default="")
    new_slug: str | None = Field(default=None)


class _DeleteArgs(_ReadArgs):
    recursive: bool = Field(default=False)


def _parse(model: type[BaseModel], arguments: dict[str, Any]):
    try:
        return model.model_validate(arguments or {}), None
    except ValidationError as exc:
        return None, _err(str(exc), error_type="validation-error")


def _map_error(exc: PrimerError) -> ToolCallResult:
    if isinstance(exc, NotFoundError):
        return _err(str(exc), error_type="not-found")
    if isinstance(exc, ConflictError):
        return _err(str(exc), error_type="conflict")
    if isinstance(exc, BadRequestError):
        return _err(str(exc), error_type="validation-error")
    return _err(str(exc), error_type="tool-error")


def build_collections_toolset(
    *,
    storage_provider,
    provider_registry=None,
    semantic_search_registry=None,
) -> InternalToolsetProvider:
    """Build the always-on collections navigation toolset."""
    # Writes through this toolset reach the vector store the same way the
    # REST document routes do. Without these hooks an agent could add or
    # edit a document in a search-enabled collection and leave it
    # unsearchable, with nothing to say so: indexing is best-effort, so
    # the write succeeded either way. The registries are optional only
    # because the tests that exercise pure navigation build without them.
    indexer = unindexer = rewriter = None
    if semantic_search_registry is not None:
        unindexer = make_document_unindexer(
            storage_provider=storage_provider,
            semantic_search_registry=semantic_search_registry,
        )
        rewriter = make_document_path_rewriter(
            storage_provider=storage_provider,
            semantic_search_registry=semantic_search_registry,
        )
        if provider_registry is not None:
            indexer = make_document_indexer(
                storage_provider=storage_provider,
                provider_registry=provider_registry,
                semantic_search_registry=semantic_search_registry,
            )
    tree = DocumentTreeService(
        storage_provider,
        indexer=indexer,
        unindexer=unindexer,
        path_rewriter=rewriter,
    )
    colls = storage_provider.get_storage(Collection)
    docs = storage_provider.get_storage(Document)
    content = storage_provider.get_content_store()

    async def _load(collection_id: str) -> tuple[Collection | None, ToolCallResult | None]:
        coll = await colls.get(collection_id)
        if coll is None:
            return None, _err(
                f"collection {collection_id!r} does not exist",
                error_type="not-found",
            )
        return coll, None

    async def _load_writable(
        collection_id: str,
    ) -> tuple[Collection | None, ToolCallResult | None]:
        coll, error = await _load(collection_id)
        if error is not None:
            return None, error
        if coll.system:
            return None, _err(
                f"collection {collection_id!r} is system-owned and read-only",
                error_type="forbidden",
            )
        return coll, None

    registry: dict[str, tuple[Tool, ToolHandler]] = {}

    # ---- reads ------------------------------------------------------------

    async def _collections_list(arguments: dict[str, Any]) -> ToolCallResult:
        rows: list[Collection] = []
        offset, page = 0, 200
        while True:
            resp = await colls.list(OffsetPage(offset=offset, length=page))
            rows.extend(resp.items)
            if len(resp.items) < page:
                break
            offset += page
        return _ok({"collections": [
            {
                "id": c.id,
                "description": c.description,
                "system": c.system,
                "search": _capability(c),
            }
            for c in sorted(rows, key=lambda c: c.id)
        ]})

    registry["collections_list"] = (
        make_tool(
            id="collections_list",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="List every collection with the best search each offers.",
            when=(
                "Use when you need to find which wiki holds the material you "
                "want, before reading or grepping it."
            ),
            args_schema={"type": "object", "properties": {}, "additionalProperties": False},
            examples=[ToolExample(
                args={},
                returns='{"collections": [{"id": "kb", "search": "fulltext", ...}]}',
                note="search names the best rung: semantic or fulltext",
            )],
            required_role="user",
        ),
        _collections_list,
    )

    async def _collection_tree(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_TreeArgs, arguments)
        if error is not None:
            return error
        _, error = await _load(args.collection)
        if error is not None:
            return error
        try:
            nodes = await tree.tree(
                collection_id=args.collection, parent=args.parent, depth=args.depth,
            )
        except PrimerError as exc:
            return _map_error(exc)
        return _ok({"nodes": [n.model_dump() for n in nodes]})

    registry["collection_tree"] = (
        make_tool(
            id="collection_tree",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="List the document tree under a parent path.",
            when="Use to discover what exists before guessing a path.",
            args_schema=_TreeArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "parent": "", "depth": 2},
                returns='{"nodes": [{"path": "guides", "has_children": true}, ...]}',
                note="depth 1 lists only the immediate children",
            )],
            required_role="user",
        ),
        _collection_tree,
    )

    async def _read_document(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_ReadArgs, arguments)
        if error is not None:
            return error
        _, error = await _load(args.collection)
        if error is not None:
            return error
        try:
            res = await tree.read(collection_id=args.collection, path=args.path)
        except PrimerError as exc:
            return _map_error(exc)
        return _ok({
            "path": res.document.path,
            "title": res.document.title or res.document.slug,
            "body": res.body,
            "children": [c.model_dump() for c in res.children],
        })

    registry["read_document"] = (
        make_tool(
            id="read_document",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="Read one document's body and list its children.",
            when="Use once you know the slug path; collection_tree finds it.",
            args_schema=_ReadArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "path": "guides/intro"},
                returns='{"body": "# Intro...", "children": []}',
                note="a miss names the siblings it did find",
            )],
            required_role="user",
        ),
        _read_document,
    )

    async def _run_semantic(coll, query: str, top_k: int):
        """Embed + query the vector store; (hits, error) like _parse."""
        from primer.model.chat import TextPart
        from primer.search.run import run_collection_search

        try:
            embedder = await provider_registry.get_embedder(
                coll.search.embedder.provider_id
            )
            response = await embedder.embed(
                model=coll.search.embedder.model,
                inputs=[TextPart(text=query)],
            )
            vector = list(response.embeddings[0].vector)
            store = await semantic_search_registry.get_store(
                coll.search.vector_store_provider_id
            )
            hits = await run_collection_search(
                collection=coll, embedder=embedder, store=store,
                query=query, top_k=top_k,
                cross_encoder_resolver=provider_registry, query_vector=vector,
            )
        except PrimerError as exc:
            return None, _map_error(exc)

        out = []
        for hit in hits:
            path = hit.record.meta.get("path")
            if not path:
                doc = await docs.get(hit.record.document_id)
                path = doc.path if doc is not None else hit.record.document_id
            out.append({
                "path": path,
                "excerpt": hit.record.text,
                "score": hit.score,
            })
        return out, None

    def _capability(coll: Collection) -> str:
        """Best rung this collection offers right now."""
        if (coll.search is not None and coll.search.state == "ready"
                and provider_registry is not None
                and semantic_search_registry is not None):
            return "semantic"
        return "fulltext"

    async def _search(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_SearchArgs, arguments)
        if error is not None:
            return error
        coll, error = await _load(args.collection)
        if error is not None:
            return error

        best = _capability(coll)
        mode = args.mode
        note = None
        if mode == "auto":
            mode = best
            if best != "semantic" and coll.search is not None:
                note = (f"semantic index is {coll.search.state}; "
                        "fell back to fulltext")
        elif mode == "semantic" and best != "semantic":
            return _err(
                f"semantic search is not enabled on {args.collection!r}; "
                "available: fulltext, literal, regex",
                error_type="conflict",
            )

        if mode == "semantic":
            hits, error = await _run_semantic(coll, args.query, args.limit)
            if error is not None:
                return error
            out: dict[str, Any] = {
                "hits": hits, "truncated": False, "mode_used": "semantic",
            }
        elif mode == "fulltext":
            try:
                result = await fulltext_collection(
                    content, collection_id=args.collection,
                    query=args.query, path_prefix=args.path_prefix,
                    max_results=args.limit,
                )
            except PrimerError as exc:
                return _map_error(exc)
            out = {"hits": [h.model_dump() for h in result.hits],
                   "truncated": result.truncated, "mode_used": "fulltext"}
        else:  # literal | regex
            pattern = args.query if mode == "regex" else re.escape(args.query)
            try:
                result = await _grep(
                    content, collection_id=args.collection, pattern=pattern,
                    path_prefix=args.path_prefix, max_results=args.limit,
                )
            except PrimerError as exc:
                return _map_error(exc)
            out = {"hits": [h.model_dump() for h in result.hits],
                   "truncated": result.truncated, "mode_used": mode}
        if note:
            out["note"] = note
        return _ok(out)

    registry["search"] = (
        make_tool(
            id="search",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose=(
                "Search a collection. One tool for every rung: mode "
                "defaults to the best the collection offers and the "
                "result states mode_used."
            ),
            when=(
                "Default to mode auto. Use literal for exact strings, "
                "regex only when you mean a pattern. A fulltext hit is "
                "keyword-relevant; a semantic hit is meaning-relevant."
            ),
            args_schema=_SearchArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "query": "how refunds work"},
                returns='{"hits": [...], "mode_used": "fulltext"}',
                note="mode_used tells you the rung auto picked",
            )],
            required_role="user",
        ),
        _search,
    )

    # ---- writes -----------------------------------------------------------

    async def _create_document(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_CreateArgs, arguments)
        if error is not None:
            return error
        _, error = await _load_writable(args.collection)
        if error is not None:
            return error
        try:
            doc = await tree.create(
                collection_id=args.collection, parent=args.parent,
                slug=args.slug, body=args.body, title=args.title,
            )
        except PrimerError as exc:
            return _map_error(exc)
        return _ok({"path": doc.path, "document_id": doc.id})

    registry["create_document"] = (
        make_tool(
            id="create_document",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="Create a document under a parent path.",
            when="Use to add a page to a wiki you own.",
            args_schema=_CreateArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "parent": "guides", "slug": "intro",
                      "body": "# Intro"},
                returns='{"path": "guides/intro", "document_id": "document-..."}',
                note="slug must be [a-z0-9-]; the parent must already exist",
            )],
            required_role="user",
        ),
        _create_document,
    )

    async def _update_document(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_UpdateArgs, arguments)
        if error is not None:
            return error
        _, error = await _load_writable(args.collection)
        if error is not None:
            return error
        try:
            doc = await tree.update(
                collection_id=args.collection, path=args.path,
                body=args.body, title=args.title,
            )
        except PrimerError as exc:
            return _map_error(exc)
        return _ok({"path": doc.path, "document_id": doc.id})

    registry["update_document"] = (
        make_tool(
            id="update_document",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="Replace a document's body and/or title.",
            when="Use to revise an existing page in place.",
            args_schema=_UpdateArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "path": "guides/intro", "body": "# New"},
                returns='{"path": "guides/intro", "document_id": "document-..."}',
                note="omit body to retitle without touching the text",
            )],
            required_role="user",
        ),
        _update_document,
    )

    async def _move_document(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_MoveArgs, arguments)
        if error is not None:
            return error
        _, error = await _load_writable(args.collection)
        if error is not None:
            return error
        try:
            doc = await tree.move(
                collection_id=args.collection, path=args.path,
                new_parent=args.new_parent, new_slug=args.new_slug,
            )
        except PrimerError as exc:
            return _map_error(exc)
        return _ok({"path": doc.path, "document_id": doc.id})

    registry["move_document"] = (
        make_tool(
            id="move_document",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="Move a document and its subtree to a new parent or slug.",
            when="Use to reorganise a wiki; document ids survive the move.",
            args_schema=_MoveArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "path": "intro", "new_parent": "guides"},
                returns='{"path": "guides/intro", "document_id": "document-..."}',
                note="moving a node into its own subtree is rejected",
            )],
            required_role="user",
        ),
        _move_document,
    )

    async def _delete_document(arguments: dict[str, Any]) -> ToolCallResult:
        args, error = _parse(_DeleteArgs, arguments)
        if error is not None:
            return error
        _, error = await _load_writable(args.collection)
        if error is not None:
            return error
        try:
            deleted = await tree.delete(
                collection_id=args.collection, path=args.path,
                recursive=args.recursive,
            )
        except PrimerError as exc:
            return _map_error(exc)
        return _ok({"deleted": deleted})

    registry["delete_document"] = (
        make_tool(
            id="delete_document",
            toolset_id=COLLECTIONS_TOOLSET_ID,
            purpose="Delete a document, optionally with its subtree.",
            when="Use to remove a page; pass recursive for a whole branch.",
            args_schema=_DeleteArgs.model_json_schema(),
            examples=[ToolExample(
                args={"collection": "kb", "path": "old", "recursive": True},
                returns='{"deleted": ["document-...", "document-..."]}',
                note="without recursive, a node with children is a conflict",
            )],
            required_role="user",
        ),
        _delete_document,
    )

    return InternalToolsetProvider(COLLECTIONS_TOOLSET_ID, registry)


__all__ = ["COLLECTIONS_TOOLSET_ID", "build_collections_toolset"]
