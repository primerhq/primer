"""Web search subsystem — the adapter ABC, concretes, registry, and
service that back the ``web__web_search`` MCP tool.

The public surface re-exported here is what callers outside the
package use: the ABC + result type + named exceptions for
extension authors, plus the concrete adapters that ship in-tree
for the registry's default factory.

See ``docs/superpowers/specs/2026-06-03-web-search-providers-design.md``.
"""

from __future__ import annotations

from primer.web_search.adapter import (
    SafeSearchLevel,
    SearchHit,
    WebSearchAdapter,
    WebSearchProviderError,
    WebSearchUnavailable,
)


__all__ = [
    "SafeSearchLevel",
    "SearchHit",
    "WebSearchAdapter",
    "WebSearchProviderError",
    "WebSearchUnavailable",
]
