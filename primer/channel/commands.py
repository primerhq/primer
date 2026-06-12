"""Platform-agnostic slash-command parsing + result shapes.

Handlers (later tasks) execute the parsed command against chats/associations
and return a CommandResult; each adapter renders it natively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from primer.int.storage_provider import StorageProvider
from primer.model.agent import Agent
from primer.model.chats import Chat
from primer.model.storage import OffsetPage, OrderBy

_VERBS = frozenset({"new", "list", "switch", "agent"})


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


__all__ = ["CommandExecutor", "CommandResult", "ParsedCommand", "parse_command"]
