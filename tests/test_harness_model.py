"""Pydantic round-trip tests for harness models."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pydantic import SecretStr, ValidationError

from primer.model.harness import (
    Harness, HarnessRendering, HarnessStatus, HarnessOperation, RenderedEntry,
)


class TestHarness:
    def test_defaults(self):
        h = Harness(
            id="h1", slug="acme", name="Acme",
            git_url="https://github.com/x/y",
            created_at=datetime.now(timezone.utc),
        )
        assert h.status == HarnessStatus.DRAFT
        assert h.ref == "main"
        assert h.overrides == {}
        assert h.git_token is None
        assert h.commits_ahead is False
        assert h.pending_operation is None

    def test_slug_rejects_underscore_underscore(self):
        with pytest.raises(ValidationError):
            Harness(
                id="h1", slug="bad__slug", name="x",
                git_url="https://x/y",
                created_at=datetime.now(timezone.utc),
            )

    def test_slug_rejects_uppercase(self):
        with pytest.raises(ValidationError):
            Harness(
                id="h1", slug="BadSlug", name="x",
                git_url="https://x/y",
                created_at=datetime.now(timezone.utc),
            )

    def test_token_round_trips_as_secret(self):
        h = Harness(
            id="h1", slug="acme", name="x",
            git_url="https://x/y",
            git_token=SecretStr("ghp_abc"),
            created_at=datetime.now(timezone.utc),
        )
        # Serialized form hides the value
        dumped = h.model_dump_json()
        assert "ghp_abc" not in dumped
        # Round-trip via model_dump(mode='json') preserves the value when reconstructed
        round = Harness.model_validate_json(dumped)
        assert round.git_token is not None


class TestHarnessRendering:
    def test_round_trip(self):
        r = HarnessRendering(
            id="h1", harness_id="h1",
            bundle_hash="b1", overrides_hash="o1",
            schema_hash="s1",
            entries=[
                RenderedEntry(
                    kind="agent", template_name="assistant",
                    resolved_id="acme__assistant",
                    template_source_hash="t1", rendered_hash="r1",
                    rendered_payload={"description": "x"},
                ),
            ],
            rendered_at=datetime.now(timezone.utc),
        )
        assert r.entries[0].resolved_id == "acme__assistant"


def test_agent_carries_harness_id():
    from primer.model.agent import Agent, AgentModel
    a = Agent(
        id="a1", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
        harness_id="h1",
    )
    assert a.harness_id == "h1"


def test_graph_carries_harness_id():
    # Imports inside the function to avoid circular-import issues at module level
    import primer.model.workspace_session  # noqa: F401 — ensure workspace_session is fully initialised first
    from primer.model.graph import (
        Graph,
        _AgentNodeRef,
        _BeginNode,
        _EndNode,
        _StaticEdge,
    )
    # Graph requires Begin + End (topology rules); provide a minimal valid graph
    g = Graph(
        id="g1", description="x",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id="a1"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="end"),
        ],
        harness_id="h1",
    )
    assert g.harness_id == "h1"


def test_collection_carries_harness_id():
    from primer.model.collection import Collection, CollectionEmbedder, Document
    c = Collection(
        id="c1", description="x",
        embedder=CollectionEmbedder(provider_id="p", model="m"),
        search_provider_id="s",
        harness_id="h1",
    )
    assert c.harness_id == "h1"
    d = Document(id="d1", collection_id="c1", name="n", path="d1.md", meta={}, harness_id="h1")
    assert d.harness_id == "h1"


def test_toolset_carries_harness_id():
    from primer.model.provider import Toolset, ToolsetProviderType
    t = Toolset(id="t1", provider=ToolsetProviderType.INTERNAL, harness_id="h1")
    assert t.harness_id == "h1"
