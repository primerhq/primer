"""Block-kit builders for the Slack channel adapter."""

from __future__ import annotations

from matrix.channel.adapter import PromptEnvelope
from matrix.channel.slack.render import (
    REJECT_MODAL_CALLBACK_ID,
    build_ask_user_message,
    build_reject_modal,
    build_tool_approval_message,
)


def _env(kind: str = "ask_user") -> PromptEnvelope:
    return PromptEnvelope(
        kind=kind, workspace_id="ws1", session_id="s1",
        tool_call_id="tc1", prompt="please answer",
        response_schema=None,
        choices=["Approve", "Reject"] if kind == "tool_approval" else None,
        timeout_at_iso=None,
    )


def test_ask_user_message_has_metadata_and_thread_hint():
    body = build_ask_user_message(channel_id="C01", envelope=_env("ask_user"))
    assert body["channel"] == "C01"
    assert body["text"] == "please answer"
    assert body["metadata"]["event_type"] == "matrix_ask"
    assert body["metadata"]["event_payload"] == {
        "kind": "ask_user", "ws": "ws1", "sid": "s1", "tcid": "tc1",
    }
    # Must include a "reply in this thread" hint block.
    contexts = [b for b in body["blocks"] if b["type"] == "context"]
    assert any("thread" in e.get("text", "").lower()
               for ctx in contexts for e in ctx["elements"])


def test_tool_approval_message_has_buttons_with_value_encoded_ids():
    body = build_tool_approval_message(
        channel_id="C01", envelope=_env("tool_approval"),
    )
    actions = [b for b in body["blocks"] if b["type"] == "actions"]
    assert len(actions) == 1
    btns = actions[0]["elements"]
    assert [b["action_id"] for b in btns] == ["approve", "reject"]
    assert btns[0]["value"] == "approve:ws1:s1:tc1"
    assert btns[1]["value"] == "reject:ws1:s1:tc1"
    # metadata still carries IDs for thread / conversations.history fallback.
    assert body["metadata"]["event_payload"]["tcid"] == "tc1"


def test_reject_modal_round_trips_ids_via_private_metadata():
    view = build_reject_modal(
        workspace_id="ws1", session_id="s1", tool_call_id="tc1",
    )
    assert view["callback_id"] == REJECT_MODAL_CALLBACK_ID
    assert view["private_metadata"] == "reject:ws1:s1:tc1"
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert inputs and inputs[0]["element"]["action_id"] == "reason_text"
