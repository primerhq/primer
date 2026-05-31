"""_FanOutNode model shape — requires at least one spec, accepts mixed specs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import FanOutSpec, _FanOutNode


def _spec(kind="broadcast", target="w", count=2, **kw):
    base = dict(kind=kind, target_node_id=target, count=count)
    base.update(kw)
    return FanOutSpec(**base)


def test_minimal_fanout() -> None:
    n = _FanOutNode(id="fan", specs=[_spec()])
    assert n.kind == "fan_out"
    assert n.id == "fan"
    assert len(n.specs) == 1


def test_mixed_specs() -> None:
    n = _FanOutNode(
        id="fan",
        specs=[
            FanOutSpec(kind="broadcast", target_node_id="a", count=3),
            FanOutSpec(kind="tee", target_node_ids=["b", "c"]),
            FanOutSpec(
                kind="map",
                target_node_id="d",
                source_node_id="planner",
                source_path="items",
            ),
        ],
    )
    assert len(n.specs) == 3
    assert {s.kind for s in n.specs} == {"broadcast", "tee", "map"}


def test_empty_specs_rejected() -> None:
    with pytest.raises(ValidationError):
        _FanOutNode(id="fan", specs=[])


def test_id_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        _FanOutNode(id="", specs=[_spec()])
