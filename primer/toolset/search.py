"""``search`` internal toolset — one search tool per Describeable entity.

Activated only when the internal collections subsystem is configured
and bootstrapped. The toolset exposes:

* ``search_agents`` — semantic search over agent definitions.
* ``search_graphs`` — semantic search over graph definitions.
* ``search_collections`` — semantic search over collection
  definitions (including the internal collections themselves).
* ``search_tools`` — semantic search over tool descriptors from every
  toolset known to the application (including the search toolset's
  own tools, which are ingested during bootstrap).

Every tool wraps the same vector-store search call from
:meth:`InternalCollectionsSubsystem.search` so the search semantics
(embedder, future cross-encoder rerank + MMR) remain consistent
across HTTP and toolset access paths.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import ConfigError, PrimerError, NotFoundError
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok_json as _ok
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


if TYPE_CHECKING:
    from primer.internal_collections import InternalCollectionsSubsystem


logger = logging.getLogger(__name__)


SEARCH_TOOLSET_ID = "search"


class _SearchArgs(BaseModel):
    """Semantic search query."""

    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Free-text query. The subsystem embeds it via the configured "
            "embedding provider and searches the matching internal "
            "collection."
        ),
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of hits to return (1-100, default 10).",
    )


def _make_search_handler(
    subsystem: "InternalCollectionsSubsystem",
    entity_type: str,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err(
                "argument validation failed: "
                + json.dumps(exc.errors(), default=str),
                error_type="validation-error",
            )
        try:
            hits = await subsystem.search(
                entity_type,  # type: ignore[arg-type]
                query=args.query,
                top_k=args.top_k,
            )
        except ConfigError as exc:
            return _err(str(exc), error_type="subsystem-inactive")
        except NotFoundError as exc:
            return _err(getattr(exc, "message", str(exc)), error_type="not-found")
        except PrimerError as exc:
            return _err(
                getattr(exc, "message", str(exc)),
                error_type="storage-error",
            )
        return _ok(
            {
                "hits": [
                    {
                        "document_id": hit.record.document_id,
                        "chunk_id": hit.record.chunk_id,
                        "score": hit.score,
                        "text": hit.record.text,
                        "meta": hit.record.meta,
                    }
                    for hit in hits
                ]
            }
        )

    return _handler


def _make_ai_docs_handler(
    subsystem: "InternalCollectionsSubsystem",
) -> ToolHandler:
    """Handler for ``search_ai_docs`` — wraps subsystem.search_ai_docs().

    Distinct from :func:`_make_search_handler` because the AI docs
    collection isn't keyed off a CDC entity type — it has its own
    disk-sourced ingest path and its own subsystem method.
    """
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err(
                "argument validation failed: "
                + json.dumps(exc.errors(), default=str),
                error_type="validation-error",
            )
        try:
            hits = await subsystem.search_ai_docs(
                query=args.query, top_k=args.top_k,
            )
        except ConfigError as exc:
            return _err(str(exc), error_type="subsystem-inactive")
        except NotFoundError as exc:
            return _err(getattr(exc, "message", str(exc)), error_type="not-found")
        except PrimerError as exc:
            return _err(
                getattr(exc, "message", str(exc)),
                error_type="storage-error",
            )
        return _ok(
            {
                "hits": [
                    {
                        "document_id": hit.record.document_id,
                        "chunk_id": hit.record.chunk_id,
                        "score": hit.score,
                        "text": hit.record.text,
                        "meta": hit.record.meta,
                    }
                    for hit in hits
                ]
            }
        )

    return _handler


def _descriptor(name: str, pretty: str) -> Tool:
    return make_tool(
        id=name,
        toolset_id=SEARCH_TOOLSET_ID,
        purpose=(
            f"Semantic search over {pretty}: embed the query and return up "
            f"to ``top_k`` hits from the reserved ``_internal_{pretty}`` "
            "collection, each with ``document_id``, ``score``, ``text`` and "
            "``meta``."
        ),
        when=(
            f"Use when you need to discover {pretty} by meaning rather than "
            "by exact id; if you already know the id, look it up directly "
            "instead of searching."
        ),
        args_schema=_SearchArgs.model_json_schema(),
        examples=[
            ToolExample(
                args={"query": "code review", "top_k": 5},
                returns=f"up to 5 {pretty} ranked by relevance",
            ),
        ],
        # Internal collections + internal-search config are operator-plane
        # (§6.2): these tools search the reserved "_internal_*" collections
        # (agent/graph/collection/tool definitions), not a user's own
        # knowledge collections (see search_collection = user, system.py).
        required_role="admin",
    )


def build_search_toolset(
    subsystem: "InternalCollectionsSubsystem",
    *,
    toolset_id: str = SEARCH_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the ``_search`` toolset bound to a live subsystem."""
    registry: dict[str, tuple[Tool, ToolHandler]] = {}
    for name, pretty, entity_type in (
        ("search_agents", "agents", "agent"),
        ("search_graphs", "graphs", "graph"),
        ("search_collections", "collections", "collection"),
        ("search_tools", "tools", "tool"),
    ):
        registry[name] = (
            _descriptor(name, pretty),
            _make_search_handler(subsystem, entity_type),
        )
    # Fifth tool — the agent-facing docs collection. Lives alongside
    # the four entity-keyed searches because it's still semantic
    # search via the same vector store + embedder. Pair with
    # system::get_document_content for full-doc retrieval.
    registry["search_ai_docs"] = (
        make_tool(
            id="search_ai_docs",
            toolset_id=toolset_id,
            purpose=(
                "Semantic search over the agent-facing documentation collection; "
                "returns the most relevant doc chunks, each whose ``document_id`` "
                "is the doc slug (e.g. ``agents``, ``chats``)."
            ),
            when=(
                "Use when you need platform usage guidance or how-to docs; pair "
                "with ``get_document_content`` on a returned slug to fetch the "
                "full document. Not for user-defined collections (use "
                "``search_collections``)."
            ),
            args_schema=_SearchArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"query": "how do graphs work", "top_k": 3},
                    returns="top doc chunks",
                ),
            ],
            required_role="admin",
        ),
        _make_ai_docs_handler(subsystem),
    )
    logger.info(
        "search toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["SEARCH_TOOLSET_ID", "build_search_toolset"]
