"""Begin and End node model shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import _BeginNode, _EndNode


def test_begin_node_minimal() -> None:
    b = _BeginNode(id="begin")
    assert b.kind == "begin"
    assert b.id == "begin"
    assert b.description is None
    assert b.input_schema is None


def test_begin_node_with_schema() -> None:
    b = _BeginNode(
        id="begin",
        description="Take a research question",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    assert b.input_schema is not None
    assert b.description == "Take a research question"


def test_end_node_minimal() -> None:
    e = _EndNode(id="end")
    assert e.kind == "end"
    assert e.output_template == ""
    assert e.output_schema is None


def test_end_node_with_template_and_schema() -> None:
    e = _EndNode(
        id="end",
        output_template="{{ nodes.x.text }}",
        output_schema={"type": "object"},
    )
    assert e.output_template == "{{ nodes.x.text }}"


def test_begin_id_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        _BeginNode(id="")


def test_end_id_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        _EndNode(id="")
