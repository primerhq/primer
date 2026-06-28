"""Outbound build — Spec B §5."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.harness.outbound import OutboundBuildError, build_outbound
from primer.model.agent import Agent
from primer.model.harness import (
    Harness,
    HarnessDirection,
    OverrideMapping,
    TrackedEntity,
)


def _make_harness(*, tracked_entities: list[TrackedEntity]) -> Harness:
    return Harness(
        id="hn-acme",
        slug="acme",
        name="Acme",
        direction=HarnessDirection.OUTBOUND,
        git_url="https://github.com/x/y",
        created_at=datetime.now(timezone.utc),
        tracked_entities=tracked_entities,
    )


@pytest.fixture
def outbound_harness() -> Harness:
    return _make_harness(
        tracked_entities=[
            TrackedEntity(
                kind="agent",
                source_id="ag-bot",
                template_name="assistant",
                overrides=[
                    OverrideMapping(
                        field_path="/model/provider_id",
                        override_path="llm.provider_id",
                        widget="llm-provider-picker",
                    ),
                ],
            ),
        ],
    )


def _make_agent(*, id: str = "ag-bot", harness_id: str | None = None) -> Agent:
    return Agent(
        id=id,
        name="Bot",
        description="d",
        harness_id=harness_id,
        model={"provider_id": "openai", "model_name": "gpt-4"},
        temperature=0.2,
        tools=[],
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_build_exports_document_content_inline(fake_storage_provider) -> None:
    """A tracked Document exports its body as ``content_inline`` so the
    inbound install can restore + index it (the entity spec has no content
    field; without this the document ships as an empty shell)."""
    from primer.model.collection import Document

    docs = fake_storage_provider.get_storage(Document)
    await docs.create(
        Document(
            id="doc-1",
            collection_id="kb",
            name="python",
            path="python.md",
            title="python",
        )
    )
    await fake_storage_provider.get_content_store().upsert(
        document_id="doc-1",
        collection_id="kb",
        path="python.md",
        content="# Python\nUse EAFP and type hints.",
    )

    harness = _make_harness(
        tracked_entities=[
            TrackedEntity(
                kind="document", source_id="doc-1", template_name="python-doc"
            ),
        ]
    )
    result = await build_outbound(harness, storage_provider=fake_storage_provider)
    text = next(
        f.rendered_text
        for f in result.files
        if f.template_path == "templates/python-doc.yaml"
    )
    assert "content_inline:" in text
    assert "Use EAFP and type hints." in text


@pytest.mark.asyncio
async def test_build_renders_templates_and_schema(
    outbound_harness: Harness, fake_storage_provider
) -> None:
    agents = fake_storage_provider.get_storage(Agent)
    await agents.create(_make_agent())

    result = await build_outbound(
        outbound_harness, storage_provider=fake_storage_provider
    )

    paths = {f.template_path for f in result.files}
    assert "harness.yaml" in paths
    assert "overrides.schema.json" in paths
    assert "templates/assistant.yaml" in paths

    template_text = next(
        f.rendered_text
        for f in result.files
        if f.template_path == "templates/assistant.yaml"
    )
    assert "{{ overrides.llm.provider_id }}" in template_text

    assert result.bundle_hash and len(result.bundle_hash) == 64
    assert all(c in "0123456789abcdef" for c in result.bundle_hash)

    schema = result.overrides_schema
    leaf = schema["properties"]["llm"]["properties"]["provider_id"]
    assert leaf["default"] == "openai"
    assert leaf["type"] == "string"
    assert leaf["x-primer-widget"] == "llm-provider-picker"


@pytest.mark.asyncio
async def test_build_fails_when_entity_missing(
    outbound_harness: Harness, fake_storage_provider
) -> None:
    with pytest.raises(OutboundBuildError) as exc:
        await build_outbound(
            outbound_harness, storage_provider=fake_storage_provider
        )
    assert exc.value.code == "outbound_entity_missing"


@pytest.mark.asyncio
async def test_build_fails_when_entity_inbound_managed(
    outbound_harness: Harness, fake_storage_provider
) -> None:
    agents = fake_storage_provider.get_storage(Agent)
    await agents.create(_make_agent(harness_id="hn-other"))

    with pytest.raises(OutboundBuildError) as exc:
        await build_outbound(
            outbound_harness, storage_provider=fake_storage_provider
        )
    assert exc.value.code == "outbound_entity_managed"


@pytest.mark.asyncio
async def test_build_fails_when_template_name_collides(
    fake_storage_provider,
) -> None:
    agents = fake_storage_provider.get_storage(Agent)
    await agents.create(_make_agent(id="ag-1"))
    await agents.create(_make_agent(id="ag-2"))
    harness = _make_harness(
        tracked_entities=[
            TrackedEntity(
                kind="agent", source_id="ag-1", template_name="dup"
            ),
            TrackedEntity(
                kind="agent", source_id="ag-2", template_name="dup"
            ),
        ],
    )
    with pytest.raises(OutboundBuildError) as exc:
        await build_outbound(harness, storage_provider=fake_storage_provider)
    assert exc.value.code == "outbound_template_name_collision"


@pytest.mark.asyncio
async def test_build_fails_when_no_entities(fake_storage_provider) -> None:
    harness = _make_harness(tracked_entities=[])
    with pytest.raises(OutboundBuildError) as exc:
        await build_outbound(harness, storage_provider=fake_storage_provider)
    assert exc.value.code == "outbound_no_entities"


@pytest.mark.asyncio
async def test_build_fails_when_field_path_invalid(
    fake_storage_provider,
) -> None:
    agents = fake_storage_provider.get_storage(Agent)
    await agents.create(_make_agent())
    harness = _make_harness(
        tracked_entities=[
            TrackedEntity(
                kind="agent",
                source_id="ag-bot",
                template_name="assistant",
                overrides=[
                    OverrideMapping(
                        field_path="/nope/deep",
                        override_path="x.y",
                    ),
                ],
            ),
        ],
    )
    with pytest.raises(OutboundBuildError) as exc:
        await build_outbound(harness, storage_provider=fake_storage_provider)
    assert exc.value.code == "outbound_field_path_invalid"


@pytest.mark.asyncio
async def test_build_bundle_hash_is_stable(
    outbound_harness: Harness, fake_storage_provider
) -> None:
    agents = fake_storage_provider.get_storage(Agent)
    await agents.create(_make_agent())

    a = await build_outbound(
        outbound_harness, storage_provider=fake_storage_provider
    )
    b = await build_outbound(
        outbound_harness, storage_provider=fake_storage_provider
    )
    assert a.bundle_hash == b.bundle_hash
