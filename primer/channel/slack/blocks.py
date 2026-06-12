"""Block Kit rendering for the Slack agent picker."""

from __future__ import annotations

from primer.channel.commands import CommandExecutor, CommandResult
from primer.int.storage_provider import StorageProvider


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


__all__ = ["build_agent_select_blocks", "parse_agent_selection"]
