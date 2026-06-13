"""Platform-agnostic slash-command parsing + result shapes.

Handlers (later tasks) execute the parsed command against chats/associations
and return a CommandResult; each adapter renders it natively.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.channel.correlation import CorrelationStore
from primer.int.storage_provider import StorageProvider
from primer.model.agent import Agent
from primer.model.channel import Channel
from primer.model.chats import Chat, ChatChannelBinding, ChatMessage
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage, OrderBy

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

    ``supports_threads=True`` (multi-type: Slack/Discord) offers only
    ``/agent`` + ``/help``: a new thread IS a new chat and the thread list IS
    the chat list, so ``/new`` / ``/list`` / ``/switch`` are unnecessary.
    ``supports_threads=False`` (single-type: Telegram) has no threads, so it
    keeps ``/new`` / ``/list`` / ``/switch`` for explicit chat management.
    """
    lines = ["Commands:"]
    if not supports_threads:
        lines.append("/new - start a fresh chat")
        lines.append("/list - list your chats")
        lines.append("/switch <chat-id> - switch to a previous chat")
    lines.append("/agent - switch the agent (pick from a list)")
    lines.append("/help - show this help")
    if supports_threads:
        lines.append("")
        lines.append("Tip: each thread is a chat - start a new thread for a new one.")
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
    """Executes parsed commands against chats + the room Channel config."""

    def __init__(
        self, *, storage_provider: StorageProvider,
        correlation_store: CorrelationStore | None = None,
    ) -> None:
        self._sp = storage_provider
        self._correlation = correlation_store or CorrelationStore(storage_provider)

    async def _chat_config(self, channel_id: str):
        """Return the room Channel's ChatConfig (raises if channel unknown)."""
        channel = await self._sp.get_storage(Channel).get(channel_id)
        if channel is None:
            raise NotFoundError(f"no Channel {channel_id!r}")
        return channel.config.chats

    async def agent_switch_allowed(self, channel_id: str) -> bool:
        """Whether /agent switching is enabled on this channel (operator flag).
        Returns False for an unknown channel."""
        try:
            cfg = await self._chat_config(channel_id)
        except NotFoundError:
            return False
        return bool(cfg.allow_agent_switch)

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

    async def agent_picker(
        self, *, channel_id: str | None = None,
    ) -> CommandResult:
        """List pickable agents. When *channel_id* is given and the room's
        ``allowed_agents`` is non-empty, list only those (preserving order);
        otherwise list every agent in storage."""
        allowed: list[str] = []
        if channel_id is not None:
            cfg = await self._chat_config(channel_id)
            allowed = list(cfg.allowed_agents)
        out: list[dict[str, Any]] = []
        if allowed:
            for aid in allowed:
                a = await self._sp.get_storage(Agent).get(aid)
                label = (a.description if a is not None else None) or aid
                out.append({"agent_id": aid, "label": label})
            return CommandResult(kind="agent_picker", items=out)
        agents = self._sp.get_storage(Agent)
        offset = 0
        while True:
            page = await agents.list(OffsetPage(offset=offset, length=200))
            for a in page.items:
                out.append({"agent_id": a.id, "label": a.description or a.id})
            if len(page.items) < 200:
                break
            offset += 200
        return CommandResult(kind="agent_picker", items=out)

    async def new_single_chat(self, *, channel_id: str) -> CommandResult:
        """Single-type /new: detach current chat, create a fresh active one."""
        cfg = await self._chat_config(channel_id)
        if not cfg.enabled or not cfg.default_agent:
            raise ValueError(f"chats are not enabled on channel {channel_id!r}")
        default_agent = cfg.default_agent
        agent = await self._sp.get_storage(Agent).get(default_agent)
        if agent is None:
            raise NotFoundError(
                f"default agent {default_agent!r} does not exist")
        chat = await self._sp.get_storage(Chat).create(Chat(
            id=f"chat-{uuid.uuid4().hex[:12]}",
            agent_id=default_agent,
            created_at=datetime.now(timezone.utc),
            channel_binding=ChatChannelBinding(channel_id=channel_id),
        ))
        await self._correlation.set_active_chat(channel_id, chat.id)
        return CommandResult(
            kind="notice", text="Started a fresh chat with the default agent.")

    async def switch_active_chat(
        self, *, channel_id: str, chat_id: str,
    ) -> CommandResult:
        target = await self._sp.get_storage(Chat).get(chat_id)
        if target is None or (
            target.channel_binding is None
            or target.channel_binding.channel_id != channel_id
        ):
            raise NotFoundError(
                f"chat {chat_id!r} is not a chat on channel {channel_id!r}")
        await self._correlation.set_active_chat(channel_id, chat_id)
        return CommandResult(
            kind="notice", text=f"Switched to chat {target.title or chat_id}.")

    async def set_agent(
        self, *, chat_id: str, agent_id: str, channel_id: str | None = None,
    ) -> CommandResult:
        from primer.chat.pending import abandon_pending_rows
        chats = self._sp.get_storage(Chat)
        chat = await chats.get(chat_id)
        if chat is None:
            raise NotFoundError(f"chat {chat_id!r} does not exist")
        agent = await self._sp.get_storage(Agent).get(agent_id)
        if agent is None:
            raise NotFoundError(f"Agent {agent_id!r} does not exist")
        # Gate on the room Channel config. Resolve the channel from the chat's
        # binding when not passed explicitly.
        room_id = channel_id
        if room_id is None and chat.channel_binding is not None:
            room_id = chat.channel_binding.channel_id
        if room_id is not None:
            cfg = await self._chat_config(room_id)
            # Switching must be explicitly enabled by the operator (off by
            # default), then optionally restricted to allowed_agents.
            if not cfg.allow_agent_switch:
                return CommandResult(
                    kind="notice",
                    text="Agent switching is disabled on this channel.")
            if cfg.allowed_agents and agent_id not in cfg.allowed_agents:
                return CommandResult(
                    kind="notice",
                    text=f"Agent {agent_id!r} is not allowed on this channel.")
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
