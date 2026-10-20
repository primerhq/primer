"""The core kit: the five tools every seeded agent run gets (pivot P1, task 12).

``CORE_KIT_TOOLS`` is the scoped-id tuple P2 grants to the seeded agents and
Plan 05's agent form "Core kit" preset copies. Everything else an agent needs
goes through ``system__call_tool``.
"""

from __future__ import annotations

CORE_KIT_TOOLS: tuple[str, ...] = (
    "system__call_tool",
    "system__wiki_read",
    "system__wiki_search",
    "system__ask_user",
    "misc__inform_user",
)
