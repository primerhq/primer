"""Compatibility shim — re-exports from primer.web_search.duckduckgo.

This module is preserved so existing callers keep working until
Phase 9's cleanup deletes it. The constructor signature is preserved
here as a thin wrapper so callers passing ``region=...`` still work.
"""

from __future__ import annotations

from primer.model.web_search import DuckDuckGoConfig
from primer.web_search.duckduckgo import DuckDuckGoAdapter


class DuckDuckGoBackend(DuckDuckGoAdapter):
    """Back-compatible shim. New code should use DuckDuckGoAdapter
    with an explicit DuckDuckGoConfig."""

    def __init__(self, *, region: str = "us-en") -> None:
        super().__init__(DuckDuckGoConfig(), region=region)


__all__ = ["DuckDuckGoBackend"]
