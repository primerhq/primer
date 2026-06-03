"""Compatibility shim — re-exports from primer.web_search.adapter.

This module is preserved so existing callers (and the toolset's
internal handler) keep working until Phase 9's cleanup deletes it.
New code should import directly from ``primer.web_search`` or
``primer.web_search.adapter``.
"""

from __future__ import annotations

from primer.web_search.adapter import (
    SafeSearchLevel,
    SearchHit,
    WebSearchAdapter as WebSearchBackend,
)


__all__ = ["SafeSearchLevel", "SearchHit", "WebSearchBackend"]
