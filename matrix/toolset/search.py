"""``_search`` internal toolset — one search tool per Describeable entity.

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

from matrix.model.chat import Tool, ToolCallResult
from matrix.model.except_ import ConfigError, MatrixError, NotFoundError
from matrix.toolset.internal import InternalToolsetProvider, ToolHandler


if TYPE_CHECKING:
    from matrix.internal_collections import InternalCollectionsSubsystem


logger = logging.getLogger(__name__)


SEARCH_TOOLSET_ID = "_search"


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


def _ok(payload: Any) -> ToolCallResult:
    return ToolCallResult(output=json.dumps(payload, default=str), is_error=False)


def _err(message: str, *, error_type: str = "tool-error") -> ToolCallResult:
    return ToolCallResult(
        output=json.dumps({"type": error_type, "message": message}),
        is_error=True,
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
        except MatrixError as exc:
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
    return Tool(
        id=name,
        description=(
            f"Semantic search over {pretty} via the internal "
            f"collections subsystem. Embeds the query via the "
            "configured embedding provider, searches the reserved "
            f"``_internal_{pretty}`` collection in the vector store, "
            "and returns up to ``top_k`` hits ordered by relevance. "
            "Each hit carries ``document_id`` (the entity id), an "
            "optional similarity ``score``, and the ``text`` that was "
            "embedded plus the entity's serialized ``meta``. Returns "
            "``is_error=true`` ``type=subsystem-inactive`` when the "
            "internal collections subsystem has not been bootstrapped."
        ),
        toolset_id=SEARCH_TOOLSET_ID,
        args_schema=_SearchArgs.model_json_schema(),
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
    logger.info(
        "search toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["SEARCH_TOOLSET_ID", "build_search_toolset"]
