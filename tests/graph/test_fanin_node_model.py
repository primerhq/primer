"""_FanInNode model shape — Jinja aggregate_template + optional output_schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import _FanInNode


def test_minimal_fanin() -> None:
    n = _FanInNode(id="join")
    assert n.kind == "fan_in"
    assert n.aggregate_template == ""
    assert n.output_schema is None


def test_with_template_and_schema() -> None:
    n = _FanInNode(
        id="join",
        aggregate_template="{{ nodes.workers | map(attribute='text') | join(' ') }}",
        output_schema={"type": "object"},
    )
    assert "nodes.workers" in n.aggregate_template
    assert n.output_schema == {"type": "object"}


def test_id_required() -> None:
    with pytest.raises(ValidationError):
        _FanInNode(id="")
