"""Reject malformed JSON Schema at graph save time.

Spec §7.2 / Task 10.2. Fields that accept a user-supplied JSON Schema
(``_BeginNode.input_schema``, ``_EndNode.output_schema``,
``_AgentNodeRef.input_schema``, ``_AgentNodeRef.response_format``) MUST
validate the schema against the Draft 2020-12 meta-schema at construction
time. A malformed schema raises Pydantic ``ValidationError`` rather than
landing in storage and exploding at runtime.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import _AgentNodeRef, _BeginNode, _EndNode


# A non-string ``type`` keyword is rejected by Draft 2020-12; we use it
# as the canonical malformed-schema across all four field tests.
_BAD_SCHEMA: dict = {"type": 123}


def test_begin_input_schema_rejects_malformed_schema() -> None:
    """`_BeginNode.input_schema` runs the schema through Draft 2020-12
    meta-schema validation at construction time."""
    with pytest.raises(ValidationError) as exc:
        _BeginNode(id="begin", input_schema=_BAD_SCHEMA)
    assert "invalid JSON Schema" in str(exc.value)


def test_end_output_schema_rejects_malformed_schema() -> None:
    """`_EndNode.output_schema` is meta-schema-validated."""
    with pytest.raises(ValidationError) as exc:
        _EndNode(id="end", output_schema=_BAD_SCHEMA)
    assert "invalid JSON Schema" in str(exc.value)


def test_agent_input_schema_rejects_malformed_schema() -> None:
    """`_AgentNodeRef.input_schema` is meta-schema-validated."""
    with pytest.raises(ValidationError) as exc:
        _AgentNodeRef(id="a", agent_id="ag", input_schema=_BAD_SCHEMA)
    assert "invalid JSON Schema" in str(exc.value)


def test_agent_response_format_rejects_malformed_schema() -> None:
    """`_AgentNodeRef.response_format` is meta-schema-validated."""
    with pytest.raises(ValidationError) as exc:
        _AgentNodeRef(id="a", agent_id="ag", response_format=_BAD_SCHEMA)
    assert "invalid JSON Schema" in str(exc.value)
