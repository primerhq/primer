"""Block Kit rendering for the Slack chat + agent pickers."""

from __future__ import annotations

from primer.channel.commands import CommandExecutor, CommandResult
from primer.int.storage_provider import StorageProvider


CHATS_PER_PAGE = 8


def build_chat_select_blocks(chats: list[dict], *, page: int = 0) -> list[dict]:
    """Paginated chat picker: a static_select of the page's chats plus a
    Prev/Next nav row. Picking a chat (action ``pick_chat_agent``, value =
    chat_id) advances to the agent select for that chat; the nav buttons
    (``chat_page_prev`` / ``chat_page_next``, value = target page) re-render
    this picker. Used by the native Slack ``/agent`` command, which carries no
    thread context, so the operator picks the chat explicitly."""
    total = len(chats)
    pages = max(1, (total + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * CHATS_PER_PAGE
    window = chats[start:start + CHATS_PER_PAGE]
    options = [
        {
            "text": {"type": "plain_text",
                     "text": (f"{c['title']} ({c['agent_id']})")[:75]},
            "value": c["chat_id"],
        }
        for c in window
    ]
    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"Pick a chat to switch its agent (page {page + 1}/{pages}):"},
        "accessory": {
            "type": "static_select",
            "action_id": "pick_chat_agent",
            "placeholder": {"type": "plain_text", "text": "Choose a chat"},
            "options": options,
        },
    }]
    nav: list[dict] = []
    if page > 0:
        nav.append({
            "type": "button", "action_id": "chat_page_prev",
            "text": {"type": "plain_text", "text": "< Prev"},
            "value": str(page - 1),
        })
    if page < pages - 1:
        nav.append({
            "type": "button", "action_id": "chat_page_next",
            "text": {"type": "plain_text", "text": "Next >"},
            "value": str(page + 1),
        })
    if nav:
        blocks.append({"type": "actions", "elements": nav})
    return blocks


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
    "build_agent_select_blocks",
    "build_chat_select_blocks",
    "parse_agent_selection",
]
