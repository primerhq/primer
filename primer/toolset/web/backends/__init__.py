"""Compatibility shim — re-exports from primer.web_search.

This module is preserved so existing callers keep working until
Phase 9's cleanup deletes it.
"""

from __future__ import annotations

from primer.toolset.web.backends.base import (
    SafeSearchLevel,
    SearchHit,
    WebSearchBackend,
)
from primer.toolset.web.backends.ddg import DuckDuckGoBackend


__all__ = [
    "DuckDuckGoBackend",
    "SafeSearchLevel",
    "SearchHit",
    "WebSearchBackend",
]
