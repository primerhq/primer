"""`Chat.response_format` — persistent per-chat structured-output schema.

Task A1 (docs/superpowers/plans/2026-07-05-chat-refactor.md). Mirrors the
existing `Agent.response_format` validator (`tests/graph/test_schema_field_validation.py`
covers the graph-node analogue): a malformed JSON Schema is rejected at
construction time via Pydantic ``ValidationError``; ``None`` (the default)
passes through unconstrained.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.chats import Chat


def _make_chat(**kwargs) -> Chat:
    return Chat(
        id="c1",
        agent_id="ag",
        created_at=datetime.now(timezone.utc),
        **kwargs,
    )


def test_chat_accepts_valid_response_format() -> None:
    """A well-formed JSON Schema round-trips onto the chat row."""
    schema = {"type": "object", "properties": {"foo": {"type": "string"}}}
    chat = _make_chat(response_format=schema)
    assert chat.response_format == schema


def test_chat_rejects_invalid_response_format() -> None:
    """A malformed schema (bad `type` keyword) raises `ValidationError`."""
    with pytest.raises(ValidationError) as exc:
        _make_chat(response_format={"type": "nonsense-☠"})
    assert "invalid JSON Schema" in str(exc.value)


def test_chat_rejects_response_format_with_bad_required_shape() -> None:
    """A malformed schema (`required` must be a list) raises `ValidationError`."""
    with pytest.raises(ValidationError) as exc:
        _make_chat(response_format={"required": "notalist"})
    assert "invalid JSON Schema" in str(exc.value)


def test_chat_response_format_defaults_none() -> None:
    """A chat constructed without `response_format` defaults to `None`
    (no per-chat structured-output override)."""
    chat = _make_chat()
    assert chat.response_format is None
