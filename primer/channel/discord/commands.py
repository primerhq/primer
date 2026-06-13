"""Discord application-command dispatch + agent autocomplete."""

from __future__ import annotations

from primer.channel.chat_router import ChatChannelRouter
from primer.channel.commands import CommandExecutor, CommandResult, help_text
from primer.int.storage_provider import StorageProvider
from primer.model.except_ import NotFoundError


async def agent_autocomplete_choices(
    *, storage_provider: StorageProvider, current: str,
    channel_id: str | None = None,
) -> list[dict[str, str]]:
    """Discord autocomplete choices: [{name, value}] filtered by substring."""
    res = await CommandExecutor(storage_provider=storage_provider).agent_picker(
        channel_id=channel_id)
    needle = (current or "").lower()
    out: list[dict[str, str]] = []
    for opt in res.items:
        if needle and needle not in opt["label"].lower():
            continue
        out.append({"name": opt["label"], "value": opt["agent_id"]})
    return out[:25]  # Discord caps autocomplete at 25


async def handle_app_command(
    *, storage_provider: StorageProvider, command: str, channel_id: str,
    arg: str | None, thread_id: str | None,
) -> CommandResult:
    ex = CommandExecutor(storage_provider=storage_provider)
    # No "new"/"list" on Discord: a new thread is a new chat, and the channel's
    # threads are the chat list.
    if command == "help":
        return CommandResult(
            kind="notice", text=help_text(supports_threads=True))
    if command == "agent":
        if not arg:
            return await ex.agent_picker(channel_id=channel_id)
        if thread_id is None:
            raise NotFoundError("no thread to switch the agent on")
        router = ChatChannelRouter(storage_provider=storage_provider)
        chat, _ = await router.resolve_or_create(
            channel_id=channel_id, thread_external_id=thread_id,
            supports_threads=True)
        return await ex.set_agent(
            chat_id=chat.id, agent_id=arg, channel_id=channel_id)
    return CommandResult(kind="notice", text=f"unknown command {command!r}")


__all__ = ["agent_autocomplete_choices", "handle_app_command"]
