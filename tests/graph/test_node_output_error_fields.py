"""NodeOutput gains optional `error` and `ended_detail` fields populated only
on the on_failure='collect' path (Spec B §1.2)."""

from __future__ import annotations

from primer.model.graph import NodeOutput


def test_error_defaults_to_none() -> None:
    n = NodeOutput(text="ok", iteration=0)
    assert n.error is None
    assert n.ended_detail is None


def test_error_can_be_set() -> None:
    n = NodeOutput(
        text="",
        iteration=0,
        error="executor raised KeyError",
        ended_detail="tool_execution_failed",
    )
    assert n.error == "executor raised KeyError"
    assert n.ended_detail == "tool_execution_failed"


def test_round_trips_through_json() -> None:
    n = NodeOutput(
        text="",
        iteration=0,
        error="boom",
        ended_detail="end_output_invalid",
    )
    raw = n.model_dump_json()
    back = NodeOutput.model_validate_json(raw)
    assert back.error == "boom"
    assert back.ended_detail == "end_output_invalid"
