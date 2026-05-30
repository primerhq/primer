"""Per-chat last-Usage cache, shared across runner + WS layer.

Lives in its own module to avoid the import cycle that would arise
if either ``primer.chat.executor`` or ``primer.api.routers.chats``
owned it.
"""

from __future__ import annotations

import threading
from typing import TypedDict


class CachedUsage(TypedDict):
    input_tokens: int
    output_tokens: int


_LOCK = threading.Lock()
_CACHE: dict[str, CachedUsage] = {}


def set_usage(chat_id: str, input_tokens: int, output_tokens: int) -> None:
    """Stash the last Usage event for a chat. Called by ChatTurnRunner."""
    with _LOCK:
        _CACHE[chat_id] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }


def get_usage(chat_id: str) -> CachedUsage:
    """Read the last Usage; returns zeros if nothing has been recorded."""
    with _LOCK:
        return dict(_CACHE.get(chat_id, {"input_tokens": 0, "output_tokens": 0}))


def clear_usage(chat_id: str) -> None:
    """Drop a chat's cache (e.g. on chat deletion)."""
    with _LOCK:
        _CACHE.pop(chat_id, None)


def reset_cache() -> None:
    """Test seam — wipe the entire cache."""
    with _LOCK:
        _CACHE.clear()


__all__ = ["CachedUsage", "set_usage", "get_usage", "clear_usage", "reset_cache"]
