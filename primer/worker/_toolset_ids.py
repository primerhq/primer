"""Scoped-tool-id parsing helper for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). A
tiny pure function with no dependency on ``WorkerPool``. Re-exported from
``primer.worker.pool`` so existing importers (``primer.agent.invoke``,
``primer.worker.executor_builders``, tests) keep resolving
``primer.worker.pool._toolset_ids_from_scoped``.
"""

from __future__ import annotations


# Scoped tool ids are ``<toolset_id>__<tool_name>``; the worker only
# needs to resolve each unique toolset prefix to get the providers it
# has to load. Scoped ids without the separator are skipped silently —
# they can't reference a real tool anyway, and the agent definition
# is operator-owned so we don't want to 500 on a malformed entry.
def _toolset_ids_from_scoped(scoped_tool_ids: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}  # dict preserves insertion order
    for sid in scoped_tool_ids or []:
        if "__" not in sid:
            continue
        prefix = sid.rsplit("__", 1)[0]
        if prefix:
            seen.setdefault(prefix, None)
    return list(seen)
