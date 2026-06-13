"""Block Kit rendering for the Slack chat + agent pickers."""

from __future__ import annotations

from primer.channel.commands import CommandExecutor, CommandResult
from primer.int.storage_provider import StorageProvider


AGENT_SWITCH_MODAL_CALLBACK_ID = "agent_switch_modal"

# Slack static_select caps at 100 options. Channels with more chats/agents than
# this show the most recent 100 (chats are listed newest-first).
_SELECT_OPTION_CAP = 100


def build_agent_switch_modal(
    chats: list[dict], agents: list[dict], *, channel_external_id: str,
) -> dict:
    """A Slack modal (pop-up) for switching a chat's agent: a Chat select + an
    Agent select, submitted together. Replaces the old in-channel picker
    messages so nothing lingers in the conversation. ``private_metadata``
    carries the Slack channel id so the view-submission handler can resolve the
    adapter. When there are no chats (or no agents) an info-only modal is
    returned instead (no submit)."""
    if not chats:
        return _info_modal("No chats yet on this channel. Post a message to start one.")
    if not agents:
        return _info_modal("No agents are available for this channel.")
    chat_opts = [
        {"text": {"type": "plain_text",
                  "text": (f"{c['title']} ({c['agent_id']})")[:75]},
         "value": c["chat_id"]}
        for c in chats[:_SELECT_OPTION_CAP]
    ]
    agent_opts = [
        {"text": {"type": "plain_text", "text": a["label"][:75]},
         "value": a["agent_id"]}
        for a in agents[:_SELECT_OPTION_CAP]
    ]
    return {
        "type": "modal",
        "callback_id": AGENT_SWITCH_MODAL_CALLBACK_ID,
        "private_metadata": channel_external_id,
        "title": {"type": "plain_text", "text": "Switch agent"},
        "submit": {"type": "plain_text", "text": "Switch"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "chat_b",
             "label": {"type": "plain_text", "text": "Chat"},
             "element": {"type": "static_select", "action_id": "chat_s",
                         "placeholder": {"type": "plain_text", "text": "Choose a chat"},
                         "options": chat_opts}},
            {"type": "input", "block_id": "agent_b",
             "label": {"type": "plain_text", "text": "Agent"},
             "element": {"type": "static_select", "action_id": "agent_s",
                         "placeholder": {"type": "plain_text", "text": "Choose an agent"},
                         "options": agent_opts}},
        ],
    }


def _info_modal(message: str) -> dict:
    return {
        "type": "modal",
        "callback_id": AGENT_SWITCH_MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Switch agent"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section",
                    "text": {"type": "mrkdwn", "text": message}}],
    }


def read_agent_switch_submission(view: dict) -> tuple[str, str] | None:
    """Pull (chat_id, agent_id) from a submitted agent-switch modal view, or
    None when the info-only modal (no inputs) was submitted/closed."""
    try:
        state = view["state"]["values"]
        chat_id = state["chat_b"]["chat_s"]["selected_option"]["value"]
        agent_id = state["agent_b"]["agent_s"]["selected_option"]["value"]
    except (KeyError, TypeError):
        return None
    return chat_id, agent_id


def build_agent_select_blocks(
    *, result: CommandResult, chat_id: str,
) -> list[dict]:
    """A section block with a static_select of agents.

    The option value encodes '<chat_id>:<agent_id>' so the action handler
    resolves the target chat without extra state.
    """
    options = [
        {
            "text": {"type": "plain_text", "text": opt["label"][:75]},
            "value": f"{chat_id}:{opt['agent_id']}",
        }
        for opt in result.items
    ]
    return [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": "Pick an agent:"},
        "accessory": {
            "type": "static_select",
            "action_id": "pick_agent",
            "placeholder": {"type": "plain_text", "text": "Choose"},
            "options": options,
        },
    }]


async def parse_agent_selection(
    *, storage_provider: StorageProvider, selected_value: str,
) -> str:
    """Apply a 'chat_id:agent_id' selection. Returns a notice."""
    chat_id, agent_id = selected_value.split(":", 1)
    res = await CommandExecutor(storage_provider=storage_provider).set_agent(
        chat_id=chat_id, agent_id=agent_id)
    return res.text or "Agent switched."


__all__ = [
    "AGENT_SWITCH_MODAL_CALLBACK_ID",
    "build_agent_select_blocks",
    "build_agent_switch_modal",
    "parse_agent_selection",
    "read_agent_switch_submission",
]
