"""Platform-agnostic slash-command parsing + result shapes.

Handlers (later tasks) execute the parsed command against chats/associations
and return a CommandResult; each adapter renders it natively.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.int.storage_provider import StorageProvider
from primer.model.agent import Agent
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatChannelBinding, ChatMessage
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage, OrderBy
from primer.storage.q import Q

_VERBS = frozenset({"new", "list", "switch", "agent", "help"})


@dataclass(frozen=True)
class ParsedCommand:
    verb: str
    arg: str | None


def parse_command(text: str | None) -> ParsedCommand | None:
    """Parse a leading /verb. Returns None if not a known command.

    Tolerates surrounding whitespace and a trailing @botname mention on the
    verb (Telegram group syntax: '/new@mybot').
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None
    verb = parts[0].split("@", 1)[0].lower()
    if verb not in _VERBS:
        return None
    arg = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return ParsedCommand(verb=verb, arg=arg)


def help_text(*, supports_threads: bool) -> str:
    """A friendly multi-line help string, scaled to the channel type.

    ``supports_threads=True`` (multi-type: Slack/Discord) drops ``/switch``
    (threads ARE the switch UI) and notes that each new thread is a new chat.
    ``supports_threads=False`` (single-type: Telegram) includes ``/switch``.
    """
    lines = [
        "Commands:",
        "/new - start a fresh chat",
        "/list - list your chats",
    ]
    if not supports_threads:
        lines.append("/switch <chat-id> - switch to a previous chat")
    lines.append("/agent - switch the agent (pick from a list)")
    lines.append("/help - show this help")
    if supports_threads:
        lines.append("")
        lines.append("Tip: each new thread is a new chat.")
    return "\n".join(lines)


@dataclass
class CommandResult:
    """Data-only command outcome; each adapter renders it natively.

    kind:
      'list'         -> items: [{chat_id, title, agent_id, created_at}]
      'agent_picker' -> items: [{agent_id, label}]
      'notice'       -> text: a one-line message to post
    """

    kind: str
    items: list[dict[str, Any]] | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []


class CommandExecutor:
    """Executes parsed commands against chats/associations."""

    def __init__(self, *, storage_provider: StorageProvider) -> None:
        self._sp = storage_provider

    async def list_chats(self, *, channel_id: str) -> CommandResult:
        chats = self._sp.get_storage(Chat)
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await chats.find(
                None,
                OffsetPage(offset=offset, length=200),
                order_by=[OrderBy(field="created_at", direction="desc")],
            )
            for c in page.items:
                b = c.channel_binding
                if b is not None and b.channel_id == channel_id:
                    out.append({
                        "chat_id": c.id,
                        "title": c.title or c.id,
                        "agent_id": c.agent_id,
                        "created_at": c.created_at.isoformat(),
                    })
            if len(page.items) < 200:
                break
            offset += 200
        return CommandResult(kind="list", items=out)

    async def agent_picker(self) -> CommandResult:
        agents = self._sp.get_storage(Agent)
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await agents.list(OffsetPage(offset=offset, length=200))
            for a in page.items:
                out.append({"agent_id": a.id, "label": a.description or a.id})
            if len(page.items) < 200:
                break
            offset += 200
        return CommandResult(kind="agent_picker", items=out)

    async def _association(self, channel_id: str) -> ChatChannelAssociation:
        page = await self._sp.get_storage(ChatChannelAssociation).find(
            Q(ChatChannelAssociation).where("channel_id", channel_id).build(),
            OffsetPage(offset=0, length=1),
        )
        if not page.items:
            raise NotFoundError(
                f"no ChatChannelAssociation for channel {channel_id!r}")
        return page.items[0]

    async def new_single_chat(self, *, channel_id: str) -> CommandResult:
        """Single-type /new: detach current chat, create a fresh active one."""
        assoc = await self._association(channel_id)
        agent = await self._sp.get_storage(Agent).get(assoc.default_agent_id)
        if agent is None:
            raise NotFoundError(
                f"default agent {assoc.default_agent_id!r} does not exist")
        chat = await self._sp.get_storage(Chat).create(Chat(
            id=f"chat-{uuid.uuid4().hex[:12]}",
            agent_id=assoc.default_agent_id,
            created_at=datetime.now(timezone.utc),
            channel_binding=ChatChannelBinding(channel_id=channel_id),
        ))
        assoc.active_chat_id = chat.id
        await self._sp.get_storage(ChatChannelAssociation).update(assoc)
        return CommandResult(
            kind="notice", text="Started a fresh chat with the default agent.")

    async def switch_active_chat(
        self, *, channel_id: str, chat_id: str,
    ) -> CommandResult:
        assoc = await self._association(channel_id)
        target = await self._sp.get_storage(Chat).get(chat_id)
        if target is None or (
            target.channel_binding is None
            or target.channel_binding.channel_id != channel_id
        ):
            raise NotFoundError(
                f"chat {chat_id!r} is not a chat on channel {channel_id!r}")
        assoc.active_chat_id = chat_id
        await self._sp.get_storage(ChatChannelAssociation).update(assoc)
        return CommandResult(
            kind="notice", text=f"Switched to chat {target.title or chat_id}.")

    async def set_agent(self, *, chat_id: str, agent_id: str) -> CommandResult:
        from primer.chat.pending import abandon_pending_rows
        chats = self._sp.get_storage(Chat)
        chat = await chats.get(chat_id)
        if chat is None:
            raise NotFoundError(f"chat {chat_id!r} does not exist")
        agent = await self._sp.get_storage(Agent).get(agent_id)
        if agent is None:
            raise NotFoundError(f"Agent {agent_id!r} does not exist")
        if chat.agent_id == agent_id:
            return CommandResult(kind="notice", text="Already that agent.")
        if chat.pending_tool_call is not None:
            await abandon_pending_rows(
                chat, pending=chat.pending_tool_call,
                messages=self._sp.get_storage(ChatMessage), chats=chats,
                result_text="auto-rejected: agent switched",
                terminal_reason="agent_switch")
        chat.agent_id = agent_id
        await chats.update(chat)
        return CommandResult(
            kind="notice",
            text=f"Switched agent to {agent.description or agent_id}.")


__all__ = [
    "CommandExecutor", "CommandResult", "ParsedCommand",
    "help_text", "parse_command",
]
