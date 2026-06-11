"""Block-kit builders for the Slack channel adapter."""

from __future__ import annotations

from primer.channel.adapter import PromptEnvelope
from primer.channel.slack.render import (
    REJECT_MODAL_CALLBACK_ID,
    build_ask_user_message,
    build_decided_blocks,
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
        tool_name="workspace__write" if kind == "tool_approval" else None,
        tool_args={"path": "hello.txt", "content": "hi"} if kind == "tool_approval" else None,
    )


def _section_texts(blocks):
    return [b["text"]["text"] for b in blocks
            if b.get("type") == "section" and "text" in b]


def test_ask_user_message_has_metadata_and_thread_hint():
    body = build_ask_user_message(channel_id="C01", envelope=_env("ask_user"))
    assert body["channel"] == "C01"
    assert body["text"] == "please answer"
    assert body["metadata"]["event_type"] == "primer_ask"
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


def test_tool_approval_message_renders_tool_name_and_pretty_args():
    body = build_tool_approval_message(
        channel_id="C01", envelope=_env("tool_approval"),
    )
    texts = "\n".join(_section_texts(body["blocks"]))
    # Tool name shown as code, not buried in a raw repr.
    assert "`workspace__write`" in texts
    # Args rendered as a pretty-printed JSON code block (multi-line).
    assert '"path": "hello.txt"' in texts
    assert "```" in texts
    # The old raw "Approve workspace__write({...})?" repr is gone.
    assert "Approve workspace__write({" not in texts


def test_build_decided_blocks_approved_drops_buttons_and_notes_user():
    original = build_tool_approval_message(
        channel_id="C01", envelope=_env("tool_approval"),
    )["blocks"]
    decided = build_decided_blocks(
        original_blocks=original, decision="approved", slack_user_id="U9",
    )
    # No action buttons remain.
    assert all(b.get("type") != "actions" for b in decided)
    # The tool/args info blocks are preserved.
    assert any("`workspace__write`" in t for t in _section_texts(decided))
    # A decision note mentions the approver.
    ctx = [b for b in decided if b["type"] == "context"]
    note = " ".join(e["text"] for c in ctx for e in c["elements"])
    assert "Approved" in note and "<@U9>" in note


def test_build_decided_blocks_rejected_includes_reason():
    decided = build_decided_blocks(
        original_blocks=None, decision="rejected",
        slack_user_id="U9", reason="not safe",
    )
    assert all(b.get("type") != "actions" for b in decided)
    ctx = [b for b in decided if b["type"] == "context"]
    note = " ".join(e["text"] for c in ctx for e in c["elements"])
    assert "Rejected" in note and "<@U9>" in note and "not safe" in note


def test_reject_modal_round_trips_ids_via_private_metadata():
    view = build_reject_modal(
        workspace_id="ws1", session_id="s1", tool_call_id="tc1",
    )
    assert view["callback_id"] == REJECT_MODAL_CALLBACK_ID
    # Channel/ts omitted -> trailing empties, but ids still in the first slots.
    assert view["private_metadata"].startswith("reject:ws1:s1:tc1")
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert inputs and inputs[0]["element"]["action_id"] == "reason_text"


def test_reject_modal_threads_channel_and_message_ts():
    view = build_reject_modal(
        workspace_id="ws1", session_id="s1", tool_call_id="tc1",
        channel_id="C01", message_ts="123.45",
    )
    assert view["private_metadata"] == "reject:ws1:s1:tc1:C01:123.45"
