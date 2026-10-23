"""The platform wiki agent tools: wiki_read and wiki_search (pivot P1, task 12).

The system collection holds the platform wiki (task 11): the table of
contents at ``index``, the hand-written pages, the generated reference layer
and the live install map under ``this-install/``. These two tools are how an
agent run navigates it: read one page by path (its title, summary, kind,
body and children), or search it through the collections ``search`` tool.
Both are user-role and non-pausing, so they are part of the core kit
(``primer/toolset/core_kit.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import NotFoundError, PrimerError
from primer.toolset._describe import make_tool
from primer.toolset._helpers import ok as _ok
from primer.toolset._system_common import (
    SYSTEM_TOOLSET_ID,
    _err_from_primer,
    _err_from_validation,
)

if TYPE_CHECKING:
    from primer.toolset.internal import ToolHandler


class _WikiReadArgs(BaseModel):
    path: str = Field(
        default="", max_length=300,
        description="Slug path of the page; '' reads the table of contents "
        "(index). Leading or trailing slashes, a trailing .md and a #anchor "
        "are stripped.",
    )


class _WikiSearchArgs(BaseModel):
    query: str = Field(..., min_length=1, description=(
        "What to find: natural language for auto/fulltext/semantic, an exact "
        "substring for literal."))
    mode: Literal["auto", "semantic", "fulltext", "literal"] = Field(
        default="auto")
    path_prefix: str | None = Field(
        default=None,
        description="Restrict hits to paths under this prefix (e.g. "
        "start-here/).",
    )
    limit: int = Field(default=10, ge=1, le=50)


def _normalize_path(raw: str) -> str:
    """The path the tree service is asked for: spaces, slashes, .md and the
    anchor come off; the empty path reads the table of contents."""
    path = raw.strip()
    if "#" in path:
        path = path.split("#", 1)[0]
    if path.endswith(".md"):
        path = path[: -len(".md")]
    path = path.strip("/")
    return path or "index"


def _wiki_read_tool(
    storage_provider,
) -> tuple[str, tuple[Tool, ToolHandler]]:
    from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
    from primer.knowledge.tree import DocumentTreeService
    from primer.model.collection import Document

    # Lazily memoised, like the _system_crud document tree factory: building
    # the toolset over a provider with no content store (the in-memory
    # unit-test fakes) must not touch get_content_store.
    cached: dict[str, DocumentTreeService] = {}

    def _tree() -> DocumentTreeService:
        if "svc" not in cached:
            cached["svc"] = DocumentTreeService(storage_provider)
        return cached["svc"]

    docs = storage_provider.get_storage(Document)

    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WikiReadArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        tree = _tree()
        try:
            res = await tree.read(
                collection_id=SYSTEM_COLLECTION_ID,
                path=_normalize_path(args.path),
            )
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        meta = res.document.meta or {}
        if meta.get("kind") == "toc":
            # The table of contents is a root page with no tree-children;
            # its children are the collection root's entries, the Contents
            # list itself (the sections, the reference root, the live map
            # root, the legacy roots), the TOC itself aside.
            roots = await tree.tree(
                collection_id=SYSTEM_COLLECTION_ID, parent="", depth=1,
            )
            nodes = [n for n in roots if n.path != "index"]
        else:
            nodes = res.children
        children = []
        for node in nodes:
            # The node carries the title; the summary lives on the child
            # document's meta (task 11 writes it there).
            child = await docs.get(node.document_id)
            cmeta = (child.meta if child is not None else None) or {}
            children.append({
                "path": node.path,
                "title": node.title,
                "summary": cmeta.get("summary"),
            })
        return _ok({
            "path": res.document.path,
            "title": res.document.title or res.document.slug,
            "summary": meta.get("summary"),
            "kind": meta.get("kind"),
            "body": res.body,
            "children": children,
        })

    return "wiki_read", (
        make_tool(
            id="wiki_read",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Read one page of the platform wiki by path.",
            when=(
                "Use when you need a wiki page's text: the table of contents "
                "(an empty path), a guide, a section index or a tool's "
                "reference page. The path is a slug path; spaces, leading or "
                "trailing slashes, a trailing .md and a #anchor are stripped. "
                "Returns the page's path, title, summary, kind, body and its "
                "children (each path, title, summary); a missing path is "
                "``type=not-found`` and names the siblings."
            ),
            args_schema=_WikiReadArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"path": "start-here/calling-tools"},
                    returns="``{path, title, summary, kind, body, children}``",
                )
            ],
            required_role="user",
        ),
        _handler,
    )


def _wiki_search_tool(
    provider_registry,
) -> tuple[str, tuple[Tool, ToolHandler]]:
    import json

    from primer.knowledge.system_collection import LIVE_ROOT, SYSTEM_COLLECTION_ID

    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _WikiSearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            collections = await provider_registry.get_toolset("collections")
        except NotFoundError as exc:
            return _err_from_primer(exc, error_type="not-found")
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="provider-error")
        norm_prefix = (args.path_prefix or "").strip().lstrip("/")
        include_live = (
            norm_prefix == LIVE_ROOT or norm_prefix.startswith(LIVE_ROOT + "/")
        )
        fetch_limit = (
            args.limit if include_live else min(100, max(args.limit * 5, 50))
        )
        res = await collections.call(
            tool_name="search",
            arguments={
                "collection": SYSTEM_COLLECTION_ID,
                "query": args.query,
                "mode": args.mode,
                "path_prefix": args.path_prefix,
                "limit": fetch_limit,
            },
        )
        if res.is_error:
            return res.model_copy(
                update={
                    "output": res.output.replace(
                        "available: fulltext, literal, regex",
                        "available: fulltext, literal",
                    )
                }
            )
        if not include_live:
            try:
                payload = json.loads(res.output)
                hits = [
                    h
                    for h in payload.get("hits", [])
                    if h.get("path") != LIVE_ROOT
                    and not str(h.get("path", "")).startswith(LIVE_ROOT + "/")
                ]
                if len(hits) > args.limit:
                    payload["truncated"] = True
                payload["hits"] = hits[: args.limit]
                return _ok(payload)
            except Exception:  # noqa: BLE001
                return res
        return res

    return "wiki_search", (
        make_tool(
            id="wiki_search",
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Search the platform wiki.",
            when=(
                "Use when you need to find a wiki page by its content (auto, "
                "fulltext or semantic) or an exact substring (literal), "
                "optionally restricted to a path prefix. Only the system "
                "collection is searched; the collections search result passes "
                "through unchanged (hits with path, excerpt and score, the "
                "mode used, and its own errors)."
            ),
            args_schema=_WikiSearchArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"query": "call_tool", "mode": "fulltext"},
                    returns="``{hits: [{path, excerpt, score}], truncated, mode_used}``",
                )
            ],
            required_role="user",
        ),
        _handler,
    )


