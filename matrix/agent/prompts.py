"""Default prompts used by the agent runtime.

The :data:`DEFAULT_COMPACTION_PROMPT` is consumed by
:class:`matrix.agent.compaction.CompactionStrategy` (sub-project F2)
when an :class:`matrix.model.agent.Agent` has no
``compaction_prompt`` of its own. The wording derives from the
cross-project research distilled in
``research/compaction.md``.
"""

from __future__ import annotations


DEFAULT_COMPACTION_PROMPT = """\
Summarise the conversation so far for handoff to a future turn.
Preserve, in this order:

1. The user's stated goal(s) verbatim.
2. Hard constraints the user set (don't do X, must use Y).
3. Decisions made and the reasoning.
4. Files / artefacts currently being worked on, by name.
5. Tool results that materially affect the next step (the values, not
   the raw output).
6. What remains to be done.

Drop intermediate exploration, raw tool outputs, and any reasoning the
agent already discarded. Output a single dense paragraph; no headers,
no bullet lists.\
"""


__all__ = ["DEFAULT_COMPACTION_PROMPT"]
