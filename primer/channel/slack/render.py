"""Block-kit builders for ask_user / tool_approval / reject-modal."""

from __future__ import annotations

from typing import Any

from primer.channel.adapter import PromptEnvelope, format_tool_args


REJECT_MODAL_CALLBACK_ID = "primer_reject_modal"

# Slack section text caps at 3000 chars; keep args well under it.
_ARGS_MAX = 2800


def _metadata(envelope: PromptEnvelope) -> dict[str, Any]:
    event_type = (
        "primer_ask" if envelope.kind == "ask_user" else "primer_approval"
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


def _approval_info_blocks(envelope: PromptEnvelope) -> list[dict[str, Any]]:
    """The informational blocks for an approval prompt (no buttons).

    Uses the structured ``tool_name`` / ``tool_args`` the envelope carries so
    the call renders as a tool name plus a pretty-printed JSON code block,
    instead of dumping the raw ``prompt`` string.
    """
    tool_name = envelope.tool_name or "(unknown tool)"
    args_json = format_tool_args(envelope.tool_args)
    if len(args_json) > _ARGS_MAX:
        args_json = args_json[:_ARGS_MAX] + "\n... (truncated)"
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": ":lock: *Tool approval requested*"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Tool:* `{tool_name}`"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Arguments:*\n```{args_json}```"}},
    ]


def build_tool_approval_message(
    *, channel_id: str, envelope: PromptEnvelope,
) -> dict[str, Any]:
    suffix = f"{envelope.workspace_id}:{envelope.session_id}:{envelope.tool_call_id}"
    return {
        "channel": channel_id,
        "text": envelope.prompt,
        "metadata": _metadata(envelope),
        "blocks": [
            *_approval_info_blocks(envelope),
            {"type": "actions",
             "block_id": "primer_approval",
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


def _decision_note(
    *, decision: str, slack_user_id: str | None, reason: str | None,
) -> str:
    who = f" by <@{slack_user_id}>" if slack_user_id else ""
    if decision == "approved":
        return f":white_check_mark: *Approved*{who}"
    note = f":x: *Rejected*{who}"
    if reason:
        return f"{note}\n> {reason}"
    return note


def build_decided_blocks(
    *,
    original_blocks: list[dict[str, Any]] | None,
    decision: str,
    slack_user_id: str | None,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """Rebuild an approval message after a decision: keep the informational
    blocks, drop the action buttons, and append a decision context line.
    """
    kept = [
        b for b in (original_blocks or [])
        if b.get("type") != "actions"
    ]
    if not kept:
        # No original blocks to preserve (e.g. lookup failed); show a minimal
        # header so the message still reads sensibly.
        kept = [{"type": "section",
                 "text": {"type": "mrkdwn", "text": ":lock: *Tool approval*"}}]
    kept.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": _decision_note(
                decision=decision, slack_user_id=slack_user_id, reason=reason,
            ),
        }],
    })
    return kept


def build_reject_modal(
    *,
    workspace_id: str, session_id: str, tool_call_id: str,
    channel_id: str | None = None, message_ts: str | None = None,
) -> dict[str, Any]:
    # Thread the originating channel + message ts through private_metadata so
    # the modal-submit handler can chat.update the original message (modal
    # submissions don't carry the source message).
    meta = f"reject:{workspace_id}:{session_id}:{tool_call_id}"
    meta += f":{channel_id or ''}:{message_ts or ''}"
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL_CALLBACK_ID,
        "private_metadata": meta,
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
    "build_decided_blocks",
    "build_reject_modal",
    "build_tool_approval_message",
]
