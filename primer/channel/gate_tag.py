"""How a platform keeps a gate's id with the button it posts (console review C-033, ticket 01a11f52-9d98).

Slack's button value and Discord's custom id are ``verb:workspace:session:tool_call_id`` strings. The gate id rides after the tool_call_id as
``#<token>`` (a token is the full 32-character id, or its first 12 where the platform's limit is tight), so a button posted before this release,
which has no suffix, parses exactly as it did. A provider's tool_call_id may itself hold a ``#``: only a ``#`` followed by exactly 12 or 32
lowercase hex characters at the very end is taken for a gate token.
"""

from __future__ import annotations

import re


_SUFFIX = re.compile(r"^(.*)#([0-9a-f]{12}|[0-9a-f]{32})$", re.DOTALL)


def attach_gate_suffix(tool_call_id: str, token: str | None, *, max_len: int | None = None, base_len: int = 0) -> str:
    """``tool_call_id`` with ``#token`` after it; unchanged for no token, or when the result would pass ``max_len``.

    ``base_len`` is the length of everything the platform puts in front of the tool_call_id in the same string (the verb and the ids).
    """
    if not token:
        return tool_call_id
    if max_len is not None and base_len + len(tool_call_id) + 1 + len(token) > max_len:
        return tool_call_id
    return f"{tool_call_id}#{token}"


def split_gate_suffix(tail: str) -> tuple[str, str | None]:
    """``(tool_call_id, token)``; the token is ``None`` for a tail with no gate suffix."""
    match = _SUFFIX.match(tail)
    if match is None:
        return tail, None
    return match.group(1), match.group(2)


__all__ = ["attach_gate_suffix", "split_gate_suffix"]
