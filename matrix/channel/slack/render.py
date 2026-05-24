"""Block-kit builders for ask_user / tool_approval / reject-modal."""

from __future__ import annotations

from typing import Any

from matrix.channel.adapter import PromptEnvelope


REJECT_MODAL_CALLBACK_ID = "matrix_reject_modal"


def _metadata(envelope: PromptEnvelope) -> dict[str, Any]:
    event_type = (
        "matrix_ask" if envelope.kind == "ask_user" else "matrix_approval"
    )
    return {
        "event_type": event_type,
        "event_payload": {
            "kind": envelope.kind,
            "ws":   envelope.workspace_id,
            "sid":  envelope.session_id,
            "tcid": envelope.tool_call_id,
        },
    }


def build_ask_user_message(
    *, channel_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    return {
        "channel": channel_id,
        "text": envelope.prompt,
        "metadata": _metadata(envelope),
        "blocks": [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": envelope.prompt}},
            {"type": "context",
             "elements": [
                {"type": "mrkdwn",
                 "text": "_Reply in this thread to answer._"}
             ]},
        ],
    }


def build_tool_approval_message(
    *, channel_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    suffix = f"{envelope.workspace_id}:{envelope.session_id}:{envelope.tool_call_id}"
    return {
        "channel": channel_id,
        "text": envelope.prompt,
        "metadata": _metadata(envelope),
        "blocks": [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": envelope.prompt}},
            {"type": "actions",
             "block_id": "matrix_approval",
             "elements": [
                {"type": "button",
                 "action_id": "approve",
                 "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"},
                 "value": f"approve:{suffix}"},
                {"type": "button",
                 "action_id": "reject",
                 "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"},
                 "value": f"reject:{suffix}"},
             ]},
        ],
    }


def build_reject_modal(
    *, workspace_id: str, session_id: str, tool_call_id: str,
) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL_CALLBACK_ID,
        "private_metadata": f"reject:{workspace_id}:{session_id}:{tool_call_id}",
        "title": {"type": "plain_text", "text": "Reject tool call"},
        "submit": {"type": "plain_text", "text": "Send rejection"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input",
             "block_id": "reason",
             "element": {"type": "plain_text_input",
                         "multiline": True,
                         "action_id": "reason_text"},
             "label": {"type": "plain_text", "text": "Why are you rejecting?"}},
        ],
    }


__all__ = [
    "REJECT_MODAL_CALLBACK_ID",
    "build_ask_user_message",
    "build_reject_modal",
    "build_tool_approval_message",
]
