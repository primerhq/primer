"""Backends for the ``web-search`` tool.

The default is :class:`DuckDuckGoBackend` (no API key). Future Brave /
Tavily / Serper / Exa adapters slot in alongside it as additional
:class:`WebSearchBackend` implementations.
"""

from matrix.toolset.web.backends.base import (
    SafeSearchLevel,
    SearchHit,
    WebSearchBackend,
)
from matrix.toolset.web.backends.ddg import DuckDuckGoBackend


__all__ = [
    "DuckDuckGoBackend",
    "SafeSearchLevel",
    "SearchHit",
    "WebSearchBackend",
]
