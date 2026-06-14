"""Unit tests for primer.catalog.types.

Covers enum string-value stability (these values are stored in
`EmbeddingRecord.meta` and MUST remain stable across releases) and
Pydantic round-trip of :class:`SemanticHit`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.catalog import SemanticEntityType, SemanticHit


class TestSemanticEntityType:
    def test_string_values_are_stable(self) -> None:
        # These are wire identifiers; changing them is a breaking change.
        assert SemanticEntityType.AGENT.value == "agent"
        assert SemanticEntityType.TOOL.value == "tool"
        assert SemanticEntityType.GRAPH.value == "graph"
        assert SemanticEntityType.COLLECTION.value == "collection"

    def test_string_compatibility(self) -> None:
        # The enum subclasses str so plain string comparison is allowed.
        assert SemanticEntityType.AGENT == "agent"

    def test_membership(self) -> None:
        names = {t.name for t in SemanticEntityType}
        assert names == {"AGENT", "TOOL", "GRAPH", "COLLECTION"}


class TestSemanticHit:
    def test_construction(self) -> None:
        hit = SemanticHit(
            entity_type=SemanticEntityType.AGENT,
            entity_id="code-reviewer",
            text="code-reviewer\n\nReviews source code.",
            score=0.873,
        )
        assert hit.entity_type is SemanticEntityType.AGENT
        assert hit.entity_id == "code-reviewer"
        assert hit.score == 0.873

    def test_round_trip(self) -> None:
        original = SemanticHit(
            entity_type=SemanticEntityType.TOOL,
            entity_id="web__web_search",
            text="web__web_search\n\nPerform a web search.",
            score=1.5,
        )
        rehydrated = SemanticHit.model_validate(original.model_dump())
        assert rehydrated == original

    def test_entity_id_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            SemanticHit(
                entity_type=SemanticEntityType.AGENT,
                entity_id="",
                text="x",
                score=0.0,
            )

    def test_round_trip_through_json(self) -> None:
        original = SemanticHit(
            entity_type=SemanticEntityType.GRAPH,
            entity_id="research-pipeline",
            text="research-pipeline\n\nMulti-stage graph",
            score=-0.2,
        )
        as_json = original.model_dump_json()
        rehydrated = SemanticHit.model_validate_json(as_json)
        assert rehydrated == original
