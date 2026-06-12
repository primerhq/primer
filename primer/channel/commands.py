"""Platform-agnostic slash-command parsing + result shapes.

Handlers (later tasks) execute the parsed command against chats/associations
and return a CommandResult; each adapter renders it natively.
"""

from __future__ import annotations

from dataclasses import dataclass

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


__all__ = ["ParsedCommand", "parse_command"]
