"""Concrete ToolsetProvider implementations.

Each implementation subclasses :class:`primer.int.ToolsetProvider` and
exposes one source of tools to the application.

The framework also ships **built-in internal toolsets** assembled from
the Python implementation here. They are immutable (no config row to
delete) and constructed by per-toolset factories:

* :func:`build_web_toolset` — ``web_search`` + ``http_request``.
"""

from primer.toolset.internal import InternalToolsetProvider
from primer.toolset.mcp import McpToolsetProvider
from primer.toolset.web import build_web_toolset

__all__ = [
    "InternalToolsetProvider",
    "McpToolsetProvider",
    "build_web_toolset",
]
