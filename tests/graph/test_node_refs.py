"""Unit tests for exception classes in primer/graph/_node_refs.py that
carry their own structured terminal-state info, independent of any
executor/graph setup.

01a08be8: `_ToolApprovalRejected` used to carry only a freeform `reason`
string - primer/graph/base.py's catch site had no way to tell a
deliberate human rejection from an approval timeout from a cancellation,
so all three (plus a genuine tool crash, in a different except block)
were stamped with the same `ended_detail="tool_execution_failed"`. This
mirrors `TurnStreamFailure.ended_detail_code` (primer/model/chat.py,
introduced by #262 for the same class of problem: a caught exception
resolving its own terminal-state code via a property, with a safe
fallback, instead of the catch site hardcoding one string for every
case) - see tests/agent/test_turn_stream_failure.py for the pattern this
is deliberately matching.
"""

from __future__ import annotations

from primer.graph._node_refs import _ToolApprovalRejected


def test_ended_detail_code_defaults_to_rejected_when_kind_is_unset() -> None:
    """Backward-compatible fallback, mirroring TurnStreamFailure's own
    "classifier left it unset" default: a caller that hasn't been
    updated to pass `kind` (or a genuine explicit human rejection, whose
    kind IS "rejected") gets the same code as before this change."""
    exc = _ToolApprovalRejected("operator said no", tool_call_id="tc-1")
    assert exc.kind is None
    assert exc.ended_detail_code == "tool_approval_rejected"


def test_ended_detail_code_for_explicit_rejection() -> None:
    exc = _ToolApprovalRejected(
        "operator said no", tool_call_id="tc-1", kind="rejected",
    )
    assert exc.ended_detail_code == "tool_approval_rejected"


def test_ended_detail_code_for_timeout() -> None:
    exc = _ToolApprovalRejected(
        "timed-out", tool_call_id="tc-1", kind="timeout",
    )
    assert exc.ended_detail_code == "tool_approval_timeout"


def test_ended_detail_code_for_cancelled() -> None:
    exc = _ToolApprovalRejected(
        "cancelled", tool_call_id="tc-1", kind="cancelled",
    )
    assert exc.ended_detail_code == "tool_approval_cancelled"


def test_reason_and_tool_call_id_still_carried_unchanged() -> None:
    """The pre-existing fields (what str(exc) reads, what the catch site's
    NodeOutput.error / _GraphErrorEvent.message use) must be untouched by
    adding `kind` - the human-readable prose was never the broken part."""
    exc = _ToolApprovalRejected(
        "operator gave a reason", tool_call_id="tc-9", kind="rejected",
    )
    assert exc.reason == "operator gave a reason"
    assert exc.tool_call_id == "tc-9"
    assert str(exc) == "operator gave a reason"


def test_reason_defaults_and_str_falls_back_when_unset() -> None:
    exc = _ToolApprovalRejected()
    assert exc.reason is None
    assert str(exc) == "tool approval rejected"
    assert exc.kind is None
    assert exc.ended_detail_code == "tool_approval_rejected"
