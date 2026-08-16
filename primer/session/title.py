"""Session title derivation, ported from primer/chat/executor.py:132.

The chat surface derived a list-friendly title from the first non-empty
text part of the opening turn. Sessions become the only conversational
surface, so the behaviour ports verbatim, including the word-boundary
trim and the ellipsis character: matching the old titles exactly is the
point, since these are the strings users learned to scan.
"""

from __future__ import annotations

TITLE_MAX_CHARS = 80


def derive_session_title(parts: list) -> str:
    """First non-empty text part, whitespace-collapsed and trimmed.

    Falls back to a generic placeholder when the turn carries only
    binary parts, so the session list stays readable rather than
    showing a blank row.
    """
    for part in parts:
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        cleaned = " ".join(text.split())
        if not cleaned:
            continue
        if len(cleaned) <= TITLE_MAX_CHARS:
            return cleaned
        # Trim on a word boundary if one exists in the back third, so
        # the title doesn't snap a word in half when it can be avoided.
        truncated = cleaned[: TITLE_MAX_CHARS - 1]
        space = truncated.rfind(" ")
        if space >= TITLE_MAX_CHARS * 2 // 3:
            truncated = truncated[:space]
        return truncated + "…"
    return "[attachment]"


__all__ = ["TITLE_MAX_CHARS", "derive_session_title"]
