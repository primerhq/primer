"""FanOutSpec discriminator validates per kind (broadcast/tee/map):
each kind accepts its required fields and rejects the others.

Spec B §1.1."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.graph import FanOutSpec


def test_broadcast_minimal() -> None:
    s = FanOutSpec(kind="broadcast", target_node_id="worker", count=3)
    assert s.kind == "broadcast"
    assert s.target_node_id == "worker"
    assert s.count == 3
    assert s.on_failure == "fail_fast"


def test_broadcast_requires_count() -> None:
    with pytest.raises(ValidationError):
        FanOutSpec(kind="broadcast", target_node_id="worker")


def test_broadcast_forbids_tee_map_fields() -> None:
    with pytest.raises(ValidationError):
        FanOutSpec(
            kind="broadcast",
            target_node_id="worker",
            count=2,
            target_node_ids=["other"],
        )


def test_tee_minimal() -> None:
    s = FanOutSpec(kind="tee", target_node_ids=["a", "b", "c"])
    assert s.target_node_ids == ["a", "b", "c"]


def test_tee_requires_target_node_ids() -> None:
    with pytest.raises(ValidationError):
        FanOutSpec(kind="tee")


def test_map_minimal() -> None:
    s = FanOutSpec(
        kind="map",
        target_node_id="worker",
        source_node_id="planner",
        source_path="topics",
    )
    assert s.kind == "map"
    assert s.source_path == "topics"


def test_map_requires_all_three_fields() -> None:
    with pytest.raises(ValidationError):
        FanOutSpec(kind="map", target_node_id="worker")


def test_on_failure_explicit() -> None:
    s = FanOutSpec(
        kind="broadcast",
        target_node_id="w",
        count=1,
        on_failure="collect",
    )
    assert s.on_failure == "collect"


def test_on_failure_invalid_rejected() -> None:
    with pytest.raises(ValidationError):
        FanOutSpec(kind="broadcast", target_node_id="w", count=1, on_failure="ignore")


def test_count_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        FanOutSpec(kind="broadcast", target_node_id="w", count=0)
