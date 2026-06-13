"""Slack slash-command dispatch over the shared CommandExecutor."""

from __future__ import annotations

from primer.channel.commands import CommandExecutor, CommandResult, help_text
from primer.int.storage_provider import StorageProvider


async def handle_slash_command(
    *, storage_provider: StorageProvider, command: str, text: str,
    channel_id: str, thread_ts: str | None,
) -> CommandResult:
    """Execute a Slack slash command. /switch is not offered (threads ARE
    the switch UI). /agent in a thread targets that thread's chat."""
    ex = CommandExecutor(storage_provider=storage_provider)
    verb = command.lstrip("/").lower()
    # /new and /list are intentionally NOT offered on Slack: a new thread is a
    # new chat, and the channel's threads are the chat list.
    if verb == "agent":
        # Native slash command (no thread context): drive a paginated chat
        # picker, then an agent picker, so the operator targets a specific
        # chat. kind="chat_picker" carries the channel's chats.
        chats = await ex.list_chats(channel_id=channel_id)
        return CommandResult(kind="chat_picker", items=chats.items)
    if verb == "help":
        return CommandResult(
            kind="notice", text=help_text(supports_threads=True))
    return CommandResult(kind="notice", text=f"unknown command {command!r}")


__all__ = ["handle_slash_command"]
