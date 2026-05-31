"""Outbound model fields — Spec B §3."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.harness import (
    Harness,
    HarnessDirection,
    HarnessOperation,
    OverrideMapping,
    RenderedEntry,
    TrackedEntity,
)


def test_direction_default_inbound():
    h = Harness(
        id="hn-x",
        slug="xy",
        name="X",
        git_url="https://x",
        created_at=datetime.now(timezone.utc),
    )
    assert h.direction == HarnessDirection.INBOUND


def test_outbound_operations_added():
    assert HarnessOperation.BUILD.value == "build"
    assert HarnessOperation.PUSH.value == "push"


def test_override_mapping_field_path_must_be_pointer():
    with pytest.raises(ValidationError):
        OverrideMapping(field_path="model.x", override_path="llm.x")
    m = OverrideMapping(field_path="/model/x", override_path="llm.x")
    assert m.widget is None


def test_tracked_entity_template_name_validated():
    with pytest.raises(ValidationError):
        TrackedEntity(kind="agent", source_id="ag-1", template_name="Assistant!")
    te = TrackedEntity(kind="agent", source_id="ag-1", template_name="assistant")
    assert te.overrides == []


def test_rendered_entry_default_source_entity_id_is_none():
    e = RenderedEntry(
        kind="agent",
        template_name="bot",
        resolved_id="x__bot",
        template_source_hash="s" * 64,
        rendered_hash="r" * 64,
        rendered_payload={},
    )
    assert e.source_entity_id is None
