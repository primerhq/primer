"""Public types for the semantic catalog subsystem.

The catalog indexes :class:`Describeable` entities — :class:`Agent`,
:class:`Tool`, :class:`Graph`, :class:`Collection` — into per-type
vector collections. These types are the wire surface used by callers
(future event-bus subscribers, future search APIs) to drive the
catalog and consume its results.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SemanticEntityType(str, Enum):
    """Wire identifier for a catalog-indexable entity type.

    String values are stable serialization tokens and MUST remain
    unchanged across releases (they are stored in
    :attr:`EmbeddingRecord.meta` for every catalog row).
    """

    AGENT = "agent"
    TOOL = "tool"
    GRAPH = "graph"
    COLLECTION = "collection"


class SemanticHit(BaseModel):
    """One result returned from :meth:`SemanticCatalog.search`."""

    entity_type: SemanticEntityType = Field(
        ...,
        description="Which kind of Describeable this hit refers to.",
    )
    entity_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the matched entity. For tools this is the "
            "**scoped** form ``toolset_id__bare_name`` (the same id "
            "the LLM sees and the agent's ``tools`` list references); "
            "for agents / graphs / collections this is the entity's "
            "own ``id``."
        ),
    )
    text: str = Field(
        ...,
        description=(
            "The text the catalog embedded — by convention "
            "``f\"{entity.id}\\n\\n{entity.description}\"``. Returned so "
            "callers can render results without a second round-trip "
            "to persistence."
        ),
    )
    score: float = Field(
        ...,
        description=(
            "Backend-relative similarity score, higher = more "
            "relevant. Scale matches the underlying VectorStore / "
            "CrossEncoder; do not compare across catalog instances."
        ),
    )


__all__ = ["SemanticEntityType", "SemanticHit"]
